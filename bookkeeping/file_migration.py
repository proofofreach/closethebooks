"""Explicit, staged ledger-history migration from accounting export files.

No vendor schema is inferred. All input columns and source accounts are mapped
by the operator. Preview writes only under imports/file-migrations; apply
preserves normal account/year includes and commits an included history file.
See docs/FILE_MIGRATIONS.md for the input and safety contract.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import fcntl
import glob
import hashlib
import io
import json
import os
import re
import secrets
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from decimal import Decimal, localcontext
from pathlib import Path, PurePosixPath

from beancount import loader
from beancount.core.data import Custom, Open, Query, Transaction
from beancount.parser import parser, printer

from .setup import read_sources
from .workspace import ACCOUNT_PATTERN, Workspace

PROVIDERS = {"quickbooks": "QuickBooks Online", "wave": "Wave"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 128
FIELDS = ("journal", "date", "account", "debit", "credit", "currency", "description")
REQUIRED_FIELDS = FIELDS[:5]
DATE_FORMATS = {"iso": "%Y-%m-%d", "mdy": "%m/%d/%Y", "dmy": "%d/%m/%Y"}
LIMITATIONS = (
    "Ledger history only. Open invoices, bills, payment allocations, contacts, attachments, "
    "inventory, payroll, sales-tax settings, bank feeds and reconciliation status are not migrated. "
    "Source totals check the supplied export; they do not prove the export contains every source transaction. "
    "Only the workspace currency is supported. No exchange rates or foreign-currency conversion are inferred."
)
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class MigrationError(ValueError):
    """Actionable failure that must not write the ledger."""


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _write(path: Path, data: bytes) -> None:
    """Private, durable atomic file write; temporary stays on the same device."""
    fd, name = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise MigrationError(f"{label} must be an object with explicit field values.")
    return value


def _text(value, label: str, limit=1000) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise MigrationError(f"{label} must be plain text, at most {limit} characters.")
    return value.strip()


def _amount(value, label: str, *, blank=False) -> Decimal:
    value = _text(value, label, 40)
    if not value and blank:
        return Decimal(0)
    if value.startswith("-"):
        raise MigrationError(f"{label}: negative amounts are not supported; use nonnegative debit and credit columns.")
    if not re.fullmatch(r"\d{1,20}(?:\.\d{1,8})?", value):
        raise MigrationError(f"{label}: invalid amount. Use decimal-point numbers without separators (up to 8 decimal places).")
    return Decimal(value)


def _number(value: Decimal) -> str:
    return format(value, "f")


def _archive(data: bytes, *, wave=False) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
        if not entries or len(entries) > 256:
            raise MigrationError("Archive must contain between 1 and 256 members.")
        names = set()
        expanded = 0
        for entry in entries:
            name = entry.filename
            parts = PurePosixPath(name).parts
            mode = entry.external_attr >> 16
            if (not name or '\\' in name or ':' in name or name.startswith('/') or '..' in parts
                    or name in names or '\x00' in name or stat.S_ISLNK(mode)):
                raise MigrationError("Unsafe archive member path, duplicate name, or symbolic link.")
            names.add(name)
            if entry.flag_bits & 1:
                raise MigrationError("Encrypted archives are not supported.")
            if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise MigrationError("Unsupported archive compression; use standard ZIP compression.")
            if wave and Path(name).suffix.lower() == '.zip':
                raise MigrationError("Nested ZIP archives are not supported.")
            expanded += entry.file_size
            if (expanded > MAX_EXPANDED_BYTES or entry.file_size > MAX_UPLOAD_BYTES
                    or entry.file_size > max(entry.compress_size, 1) * 200):
                raise MigrationError("Archive exceeds the expansion or compression-ratio safety limit.")
        return archive
    except (zipfile.BadZipFile, NotImplementedError, OSError) as exc:
        raise MigrationError("This is not a readable ZIP/XLSX file. Export it again.") from exc


def _read_member(archive, name: str) -> bytes:
    try:
        with archive.open(name) as handle:
            data = handle.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise MigrationError("Archive member is larger than 20 MB.")
        return data
    except (KeyError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise MigrationError("An archive member is missing or damaged. Export it again.") from exc


def _xml(archive, name):
    raw = _read_member(archive, name)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise MigrationError("XLSX XML parts must use UTF-8 encoding.") from exc
    if '\x00' in text or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise MigrationError("XML document types and entities are not accepted.")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise MigrationError("The XLSX file contains malformed XML.") from exc


def _sheets(archive) -> dict[str, str]:
    book = _xml(archive, "xl/workbook.xml")
    relationships = _xml(archive, "xl/_rels/workbook.xml.rels")
    paths = {}
    for rel in relationships:
        if rel.get('TargetMode') == 'External':
            raise MigrationError("External workbook relationships are not supported.")
        if rel.get('Type', '').endswith('/worksheet'):
            target = rel.get('Target', '')
            target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            if '..' in PurePosixPath(target).parts or not target.startswith('xl/'):
                raise MigrationError("Unsafe XLSX worksheet path.")
            paths[rel.get('Id')] = target
    sheets = {}
    for sheet in book.findall('s:sheets/s:sheet', NS):
        rid = sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        if rid not in paths or not sheet.get('name') or sheet.get('name') in sheets:
            raise MigrationError("The XLSX worksheet list is invalid.")
        sheets[sheet.get('name')] = paths[rid]
    if not sheets:
        raise MigrationError("The XLSX file has no readable worksheets.")
    return sheets


def _xlsx_rows(data: bytes, sheet: str) -> list[list[str]]:
    with _archive(data) as archive:
        sheets = _sheets(archive)
        if sheet not in sheets:
            raise MigrationError("Choose an exact XLSX worksheet name before mapping columns.")
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            for item in _xml(archive, 'xl/sharedStrings.xml').findall('s:si', NS):
                shared.append(''.join(node.text or '' for node in item.findall('.//s:t', NS)))
        rows = []
        last_row = 0
        for row in _xml(archive, sheets[sheet]).findall('s:sheetData/s:row', NS):
            try:
                row_num = int(row.get('r', '0'))
            except ValueError as exc:
                raise MigrationError("Invalid XLSX row number.") from exc
            if row_num <= last_row or row_num > MAX_ROWS + 100:
                raise MigrationError("XLSX rows are duplicated, out of order, or exceed the row limit.")
            rows.extend([[] for _ in range(row_num - last_row - 1)])
            values = {}
            for cell in row.findall('s:c', NS):
                ref = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', cell.get('r', ''))
                if not ref or int(ref[2]) != row_num:
                    raise MigrationError("Invalid XLSX cell reference.")
                col = 0
                for char in ref[1]: col = col * 26 + ord(char) - 64
                if col > MAX_COLUMNS or col in values:
                    raise MigrationError("XLSX has too many columns or duplicate cells.")
                if cell.find('s:f', NS) is not None:
                    raise MigrationError("XLSX formulas are not accepted. Export a values-only copy and retain the original report.")
                kind = cell.get('t')
                value = cell.findtext('s:v', default='', namespaces=NS)
                if kind == 's':
                    if not value.isdigit() or int(value) >= len(shared):
                        raise MigrationError("Invalid XLSX shared string reference.")
                    value = shared[int(value)]
                elif kind == 'inlineStr':
                    value = ''.join(item.text or '' for item in cell.findall('.//s:t', NS))
                elif kind not in (None, 'n', 'd', 'str'):
                    raise MigrationError("XLSX error or boolean cells are not supported in the selected worksheet.")
                values[col] = value
            rows.append([values.get(i, '') for i in range(1, max(values, default=0) + 1)])
            last_row = row_num
        return rows


def _date(value: str, date_format: str) -> dt.date:
    try:
        if date_format in DATE_FORMATS:
            # strptime accepts one-digit month/day; no ambiguity remains after explicit format selection.
            return dt.datetime.strptime(value, DATE_FORMATS[date_format]).date()
        if date_format in ('excel1900', 'excel1904') and re.fullmatch(r'\d{1,7}(?:\.0+)?', value):
            serial = int(Decimal(value))
            if date_format == 'excel1900':
                if serial == 60: raise ValueError('Excel fictitious leap day')
                origin = dt.date(1899, 12, 31) if serial < 60 else dt.date(1899, 12, 30)
            else:
                origin = dt.date(1904, 1, 1)
            return origin + dt.timedelta(days=serial)
    except (ValueError, OverflowError):
        pass
    raise MigrationError(f"Invalid date {value!r} for the selected date format.")


class FileMigration:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.root = workspace.root / 'imports' / 'file-migrations'
        for path in (workspace.root / 'imports', self.root):
            if path.is_symlink():
                raise MigrationError("Migration evidence directories must not be symbolic links.")

    @contextlib.contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.root / '.lock'
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def stage(self, stage_id: str, provider: str | None = None) -> tuple[Path, dict]:
        if not re.fullmatch(r'[a-f0-9]{32}', stage_id):
            raise FileNotFoundError("Migration stage not found.")
        path = self.root / stage_id
        if path.is_symlink() or not path.is_dir():
            raise FileNotFoundError("Migration stage not found.")
        metadata = self._read_json(path / 'upload.json')
        if provider and metadata.get('provider') != provider:
            raise FileNotFoundError("Migration stage not found.")
        return path, metadata

    @staticmethod
    def _read_json(path: Path):
        if path.is_symlink():
            raise MigrationError("Migration evidence files must not be symbolic links.")
        try:
            return _object(json.loads(path.read_text(encoding='utf-8')), 'Stored migration data')
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise MigrationError("Stored migration evidence is damaged. Upload a new export.") from exc

    def upload(self, provider: str, filename: str, data: bytes) -> str:
        if provider not in PROVIDERS:
            raise MigrationError("Choose QuickBooks Online or Wave.")
        suffix = Path(filename).suffix.lower()
        allowed = {'.csv', '.xlsx', '.zip'} if provider == 'wave' else {'.csv', '.xlsx'}
        if suffix not in allowed:
            raise MigrationError("Use CSV or XLSX, or a Wave accounting ZIP. Legacy XLS must first be saved as CSV or XLSX.")
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise MigrationError("Export files must be nonempty and 20 MB or smaller.")
        if suffix in ('.zip', '.xlsx'):
            with _archive(data, wave=suffix == '.zip') as archive:
                if suffix == '.xlsx': _sheets(archive)
        with self._lock():
            stage_id = secrets.token_hex(16)
            stage = self.root / stage_id
            stage.mkdir(mode=0o700)
            _write(stage / ('source' + suffix), data)
            _write(stage / 'upload.json', _json_bytes({
                'provider': provider, 'filename': Path(filename.replace('\\', '/')).name[:200],
                'source': 'source' + suffix, 'sha256': _hash(data), 'bytes': len(data),
                'created': dt.datetime.now(dt.timezone.utc).isoformat(),
            }))
        return stage_id

    def source(self, stage_id: str) -> tuple[bytes, dict]:
        stage, meta = self.stage(stage_id)
        if meta.get('source') not in ('source.csv', 'source.xlsx', 'source.zip'):
            raise MigrationError("Invalid source evidence name.")
        path = stage / meta['source']
        if path.is_symlink() or path.stat().st_size > MAX_UPLOAD_BYTES:
            raise MigrationError("Source evidence is unsafe or too large.")
        data = path.read_bytes()
        if _hash(data) != meta['sha256']:
            raise MigrationError("Source evidence changed after upload. Upload a new export.")
        return data, meta

    def _selected_data(self, stage_id: str, selection: dict | None = None):
        selection = _object({} if selection is None else selection, 'Table selection')
        for key in ('member', 'sheet', 'delimiter'):
            if key in selection and not isinstance(selection[key], str):
                raise MigrationError(f"Table selection {key} must be text.")
        data, meta = self.source(stage_id)
        filename = meta['source']
        result = {'members': [], 'sheets': [], 'headers': [], 'rows': [], 'selection': selection}
        if filename.endswith('.zip'):
            with _archive(data, wave=True) as archive:
                result['members'] = [e.filename for e in archive.infolist() if not e.is_dir()]
                member = selection.get('member', '')
                if not member: return result, None, filename
                if member not in result['members'] or Path(member).suffix.lower() not in ('.csv', '.xlsx'):
                    raise MigrationError("Choose a CSV or XLSX accounting member from the ZIP; other files are retained only.")
                data = _read_member(archive, member)
                filename = member
        if filename.lower().endswith('.xlsx'):
            with _archive(data) as archive:
                result['sheets'] = list(_sheets(archive))
                properties = _xml(archive, 'xl/workbook.xml').find('s:workbookPr', NS)
                flag = properties.get('date1904', '0') if properties is not None else '0'
                if flag not in ('0', '1', 'false', 'true'):
                    raise MigrationError("Invalid XLSX workbook date system.")
                result['date_system'] = 'excel1904' if flag in ('1', 'true') else 'excel1900'
        return result, data, filename

    def choices(self, stage_id: str, selection: dict | None = None) -> dict:
        """Table selectors stay available even when the current header is invalid."""
        return self._selected_data(stage_id, selection)[0]

    def inspect(self, stage_id: str, selection: dict | None = None) -> dict:
        selection = _object({} if selection is None else selection, 'Table selection')
        result, data, filename = self._selected_data(stage_id, selection)
        if data is None:
            return result
        if filename.lower().endswith('.xlsx'):
            sheet = selection.get('sheet', '')
            if not sheet: return result
            rows = _xlsx_rows(data, sheet)
        else:
            delimiter = selection.get('delimiter', ',')
            if delimiter not in (',', ';', '\t'):
                raise MigrationError("Choose comma, semicolon, or tab as the CSV delimiter.")
            try:
                content = data.decode('utf-8-sig')
                if '\x00' in content:
                    raise MigrationError("CSV must be UTF-8 text without NUL bytes.")
                rows = []
                for row in csv.reader(io.StringIO(content, newline=''), delimiter=delimiter, strict=True):
                    if len(rows) > MAX_ROWS + 100 or len(row) > MAX_COLUMNS:
                        raise MigrationError("CSV exceeds 100,000 data rows or 128 columns.")
                    rows.append(row)
            except (UnicodeError, csv.Error) as exc:
                raise MigrationError("CSV must be valid UTF-8 with consistent quoting. Save a UTF-8 copy and try again.") from exc
        try:
            header_row = int(selection.get('header_row', 1))
        except (ValueError, TypeError) as exc:
            raise MigrationError("Header row must be a whole number.") from exc
        if not 1 <= header_row <= min(100, len(rows)):
            raise MigrationError("Choose a header row between 1 and 100 within the export.")
        headers = [v.strip() for v in rows[header_row - 1]]
        while headers and not headers[-1]: headers.pop()
        if not headers or any(not v for v in headers) or len(headers) != len(set(headers)):
            raise MigrationError("Header names must be nonempty and unique. Normalize this export before mapping it.")
        result['headers'] = headers
        result['skipped_preamble_rows'] = header_row - 1
        for row_num, row in enumerate(rows[header_row:], header_row + 1):
            if not any(v.strip() for v in row): continue
            if any(v.strip() for v in row[len(headers):]):
                raise MigrationError(f"Row {row_num} has values beyond the mapped header columns.")
            result['rows'].append({'row': row_num, 'values': dict(zip(headers, row + [''] * (len(headers) - len(row))))})
            if len(result['rows']) > MAX_ROWS:
                raise MigrationError("The table exceeds 100,000 nonblank rows. Split the migration outside this tool.")
        return result

    @staticmethod
    def validate_fields(table: dict, fields: dict) -> dict:
        fields = _object(fields, 'Column mapping')
        if not table['headers']:
            raise MigrationError("Select the archive member and worksheet before mapping columns.")
        for key in REQUIRED_FIELDS:
            if not fields.get(key):
                raise MigrationError(f"Map the {key} column explicitly.")
        selected = []
        for key, value in fields.items():
            if key not in FIELDS or not isinstance(value, str):
                raise MigrationError("Column mapping contains an unknown field or invalid column name.")
            if value:
                if value not in table['headers']:
                    raise MigrationError(f"Mapped column {value!r} does not exist in the selected header.")
                selected.append(value)
        if len(selected) != len(set(selected)):
            raise MigrationError("Each mapped field must use a different column.")
        return fields

    def configure(self, stage_id: str, spec: dict) -> dict:
        """Save the explicit field mapping for the HTML account-mapping step."""
        with self._lock():
            stage, _ = self.stage(stage_id)
            if self.report(stage_id).get('status') in ('applied', 'applying'):
                raise MigrationError("This migration is applied or interrupted during apply. Its evidence cannot be replaced.")
            previous = self.draft(stage_id)
            spec = {**spec, 'accounts': previous.get('spec', {}).get('accounts', {})}
            self._invalidate(stage)
            _write(stage / 'draft.json', _json_bytes({'spec': spec, 'source_accounts': []}))
            table = self.inspect(stage_id, spec.get('selection'))
            fields = self.validate_fields(table, spec.get('fields'))
            accounts = sorted({row['values'][fields['account']].strip() for row in table['rows']})
            if not accounts or '' in accounts:
                raise MigrationError("Every data row must name its source account. Remove summary rows explicitly.")
            if len(accounts) > 2000:
                raise MigrationError("The migration exceeds 2,000 source accounts.")
            draft = {'spec': spec, 'source_accounts': accounts}
            _write(stage / 'draft.json', _json_bytes(draft))
            return draft

    def draft(self, stage_id: str) -> dict:
        stage, _ = self.stage(stage_id)
        if not (stage / 'draft.json').exists():
            return {}
        draft = self._read_json(stage / 'draft.json')
        spec = _object(draft.get('spec'), 'Saved mapping')
        for key in ('fields', 'selection', 'accounts'):
            _object(spec.get(key, {}), f'Saved {key}')
        accounts = draft.get('source_accounts')
        if not isinstance(accounts, list) or not all(isinstance(account, str) for account in accounts):
            raise MigrationError("Saved source accounts are invalid. Upload the export again.")
        return draft

    def _ledger_state(self) -> tuple[list, dict, dict]:
        ws = self.workspace
        root = ws.root.resolve()
        if ws.ledger_main.is_symlink() or not ws.ledger_main.is_file():
            raise MigrationError("The main ledger must be an existing regular file.")
        paths = sorted(set(ws.ledger_root.rglob('*.beancount')) | {ws.ledger_main, ws.sources_file, ws.config_path})
        signatures = {}
        total = 0
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise MigrationError("Migration requires ledger and source files inside the workspace, without symbolic links.")
            if not path.exists():
                signatures[str(path.relative_to(ws.root))] = None
                continue
            total += path.stat().st_size
            if total > MAX_EXPANDED_BYTES or len(paths) > 1000:
                raise MigrationError("The empty workspace exceeds migration safety limits.")
            raw = path.read_bytes()
            signatures[str(path.relative_to(ws.root))] = _hash(raw)
            if path.suffix == '.beancount':
                try: content = raw.decode('utf-8')
                except UnicodeError as exc: raise MigrationError("The existing ledger is not UTF-8.") from exc
                if re.search(r'^\s*plugin\s', content, re.M):
                    raise MigrationError("Migration requires an empty ledger without executable plugins.")
                # Never let the loader read files outside the inspected tree.
                for include in re.findall(r'^\s*include\s+"([^"\n]+)"', content, re.M):
                    target = path.parent / include
                    if not target.resolve().is_relative_to(ws.ledger_root.resolve()) or target.is_symlink():
                        raise MigrationError("Ledger includes must stay within this workspace's ledger directory.")
                    included = [Path(name) for name in glob.glob(str(target))]
                    if any(item not in paths or item.is_symlink() for item in included):
                        raise MigrationError("Ledger includes must name inspected .beancount files within the ledger directory.")
                if any(option not in ('title', 'operating_currency', 'documents') for option in
                       re.findall(r'^\s*option\s+"([^"\n]+)"', content, re.M)):
                    raise MigrationError("This ledger has custom accounting options. Use a fresh workspace so migration cannot discard them.")
                entries, _, _ = parser.parse_string(content)
                if any(isinstance(e, Transaction) for e in entries):
                    raise MigrationError("This workspace already contains ledger transactions. Use a new workspace.")
        try:
            if read_sources(ws)['accounts']:
                raise MigrationError("This workspace already has registered sources. Use a new workspace before registering accounts.")
        except (ValueError, TypeError) as exc:
            if isinstance(exc, MigrationError): raise
            raise MigrationError("The source registry is invalid. Repair it before migration.") from exc
        entries, errors, options = loader.load_file(str(ws.ledger_main))
        if errors:
            raise MigrationError("The existing ledger has errors. Repair it before migration.")
        if any(not isinstance(entry, (Open, Custom, Query)) for entry in entries):
            raise MigrationError("Migration accepts only an empty chart and app settings; existing balances or other ledger directives require review.")
        return entries, options, signatures

    def blocker(self) -> str | None:
        try: self._ledger_state()
        except (MigrationError, OSError) as exc: return str(exc)
        return None

    def _build(self, stage_id: str, spec: dict) -> tuple[dict, bytes, dict]:
        # 100k rows × 20 integer and 8 fractional digits must stay exact.
        with localcontext() as context:
            context.prec = 50
            return self._build_exact(stage_id, spec)

    def _build_exact(self, stage_id: str, spec: dict) -> tuple[dict, bytes, dict]:
        spec = _object(spec, 'Migration mapping')
        _object(spec.get('selection', {}), 'Table selection')
        existing, options, signature = self._ledger_state()
        table = self.inspect(stage_id, spec.get('selection'))
        fields = self.validate_fields(table, spec.get('fields'))
        mapping = _object(spec.get('accounts'), 'Account mapping')
        if not table['rows']:
            raise MigrationError("The export contains no journal rows.")
        currency = spec.get('currency')
        if currency != self.workspace.currency or not re.fullmatch('[A-Z]{3}', currency or ''):
            raise MigrationError(f"Select workspace currency {self.workspace.currency}; foreign-currency migration is not supported.")
        reference = _text(spec.get('totals_reference', ''), 'Source totals reference')
        if not reference:
            raise MigrationError("Record the source report, date range, accounting basis, and currency for the source totals.")
        expected_debit = _amount(spec.get('source_debit', ''), 'Source debit total')
        expected_credit = _amount(spec.get('source_credit', ''), 'Source credit total')
        used = {row['values'][fields['account']].strip() for row in table['rows']}
        if '' in used:
            raise MigrationError("Every data row must name a source account. Remove summary rows explicitly.")
        if set(mapping) != used:
            raise MigrationError("Map every source account exactly once; remove extra account mappings.")
        for source, target in mapping.items():
            if not source or not isinstance(target, str) or not ACCOUNT_PATTERN.fullmatch(target):
                raise MigrationError(f"Invalid account mapping for {source!r}. Use a full Beancount account name.")
        if len(set(mapping.values())) != len(mapping):
            raise MigrationError("Map each source account to a distinct ledger account; account merging is not supported.")
        journals = {}
        by_account = {source: {'account': target, 'debit': Decimal(0), 'credit': Decimal(0)} for source, target in mapping.items()}
        debit_total = credit_total = Decimal(0)
        date_format = spec.get('date_format', 'iso')
        if not isinstance(date_format, str) or date_format not in (*DATE_FORMATS, 'excel1900', 'excel1904'):
            raise MigrationError("Choose an explicit supported date format.")
        if date_format.startswith('excel') and table.get('date_system') and date_format != table['date_system']:
            raise MigrationError(f"Selected date system does not match this XLSX workbook's {table['date_system']} date system.")
        for item in table['rows']:
            row = item['values']
            label = f"Row {item['row']}"
            value = lambda key: row.get(fields.get(key, ''), '').strip()
            journal = _text(value('journal'), label + ' journal ID', 200)
            if not journal:
                raise MigrationError(f"{label}: journal ID is required on every row; fill down explicitly in a reviewed copy.")
            date = _date(value('date'), date_format)
            account = value('account')
            row_currency = value('currency') if fields.get('currency') else currency
            if row_currency != currency:
                raise MigrationError(f"{label}: currency {row_currency!r} differs from workspace currency {currency}.")
            debit = _amount(value('debit'), label + ' debit amount', blank=True)
            credit = _amount(value('credit'), label + ' credit amount', blank=True)
            if (debit and credit) or not (debit or credit):
                raise MigrationError(f"{label}: exactly one of debit or credit must be positive.")
            description = _text(value('description'), label + ' description')
            entry = journals.setdefault(journal, {'date': date, 'lines': []})
            if entry['date'] != date:
                raise MigrationError(f"{label}: journal {journal!r} has inconsistent dates; IDs must identify one complete journal.")
            entry['lines'].append({'account': mapping[account], 'amount': debit - credit, 'description': description, 'row': item['row']})
            by_account[account]['debit'] += debit
            by_account[account]['credit'] += credit
            debit_total += debit
            credit_total += credit
        for journal, entry in journals.items():
            if len(entry['lines']) < 2 or sum((line['amount'] for line in entry['lines']), Decimal(0)) != 0:
                raise MigrationError(f"Journal {journal!r} does not balance exactly. No suspense or rounding postings are added.")
        if debit_total != expected_debit or credit_total != expected_credit:
            raise MigrationError(f"Source totals mismatch: rows total {debit_total} debit / {credit_total} credit; supplied source totals are {expected_debit} / {expected_credit} {currency}.")
        first = min(entry['date'] for entry in journals.values())
        last = max(entry['date'] for entry in journals.values())
        source_data, meta = self.source(stage_id)
        # The self-contained candidate is a review artifact. The durable ledger
        # keeps its normal account/year includes; _ledger_plan builds and checks
        # that layout separately before any live file can change.
        q = lambda text: json.dumps(text, ensure_ascii=False)
        lines = [f'; Ledger-history migration {stage_id}', f'; Evidence: imports/file-migrations/{stage_id}/',
                 f'option "title" {q(options.get("title") or self.workspace.company.name)}',
                 f'option "operating_currency" {q(currency)}', '']
        for documents in options.get('documents', []):
            lines.append(f'option "documents" {q(documents)}')
        opened = set()
        for entry in existing:
            if isinstance(entry, Open):
                if entry.account in mapping.values() and entry.currencies and currency not in entry.currencies:
                    raise MigrationError(f"Existing account {entry.account} does not allow currency {currency}.")
                entry = entry._replace(date=min(entry.date, first))
                opened.add(entry.account)
            lines.append(printer.format_entry(entry).rstrip())
        for account in sorted(set(mapping.values()) - opened):
            lines.append(f'{first.isoformat()} open {account}  {currency}')
        history_start = len(lines)
        for journal, entry in sorted(journals.items(), key=lambda pair: (pair[1]['date'], pair[0])):
            narration = next((line['description'] for line in entry['lines'] if line['description']), journal)
            lines.extend(['', f'{entry["date"].isoformat()} * {q(narration)}',
                          f'  migration-id: {q(stage_id)}', f'  source-system: {q(meta["provider"])}',
                          f'  source-journal: {q(journal)}', f'  source-sha256: {q(meta["sha256"])}'])
            for line in entry['lines']:
                lines.append(f'  {line["account"]}  {_number(line["amount"])} {currency}')
                lines.append(f'    source-row: {line["row"]}')
                if line['description']: lines.append(f'    source-description: {q(line["description"])}')
        candidate = ('\n'.join(lines) + '\n').encode('utf-8')
        converted, errors, _ = loader.load_string(candidate.decode('utf-8'))
        if errors:
            raise MigrationError("Candidate ledger failed Beancount validation: " + '; '.join(str(e.message) for e in errors[:4]))
        # Verify the actual parsed postings independently of the source counters.
        parsed_debit = parsed_credit = Decimal(0)
        actual = defaultdict(lambda: [Decimal(0), Decimal(0)])
        for entry in converted:
            if isinstance(entry, Transaction):
                for posting in entry.postings:
                    number = posting.units.number
                    if posting.units.currency != currency: raise MigrationError("Candidate currency verification failed.")
                    actual[posting.account][0 if number > 0 else 1] += abs(number)
                    if number > 0: parsed_debit += number
                    else: parsed_credit -= number
        if parsed_debit != debit_total or parsed_credit != credit_total:
            raise MigrationError("Candidate debit/credit verification failed.")
        account_report = []
        for source, totals in sorted(by_account.items()):
            if actual[totals['account']] != [totals['debit'], totals['credit']]:
                raise MigrationError(f"Candidate account-total verification failed for {source!r}.")
            account_report.append({'source': source, 'account': totals['account'], 'debit': _number(totals['debit']), 'credit': _number(totals['credit'])})
        history = '\n'.join([f'; Imported ledger history {stage_id}', *lines[history_start:]]) + '\n'
        plan = self._ledger_plan(stage_id, first, currency, set(mapping.values()) - opened,
                                 history, options, signature, converted)
        report = {'status': 'ready', 'provider': meta['provider'], 'stage_id': stage_id,
                  'rows': len(table['rows']), 'journals': len(journals), 'accounts': account_report,
                  'first_date': str(first), 'last_date': str(last), 'currency': currency,
                  'debit': _number(debit_total), 'credit': _number(credit_total),
                  'source_debit': _number(expected_debit), 'source_credit': _number(expected_credit),
                  'totals_reference': reference, 'source_sha256': _hash(source_data),
                  'candidate_sha256': _hash(candidate), 'workspace_signature': signature,
                  'ledger_files': {relative: _hash(content.encode('utf-8')) for relative, content in plan.items()},
                  'selection': spec.get('selection', {}), 'skipped_preamble_rows': table.get('skipped_preamble_rows', 0),
                  'unimported_members': [m for m in table['members'] if m != spec.get('selection', {}).get('member')],
                  'unimported_sheets': [s for s in table['sheets'] if s != spec.get('selection', {}).get('sheet')],
                  'limitations': LIMITATIONS, 'mapping': spec}
        report['digest'] = _hash(_json_bytes(report))
        return report, candidate, plan

    def _ledger_plan(self, stage_id, first, currency, missing_accounts, history, options, signature, converted):
        """Keep normal account and year writes connected; verify the exact tree."""
        ws = self.workspace
        accounts = ws.ledger_root / 'accounts.beancount'
        included = {Path(path).resolve() for path in options.get('include', [])}
        if not accounts.is_file() or accounts.resolve() not in included:
            raise MigrationError("Migration requires the normal accounts.beancount include. Create a fresh workspace.")
        originals = {}
        plan = {}
        for relative, sha in signature.items():
            path = ws.root / relative
            if sha is None or path.suffix != '.beancount': continue
            raw = path.read_text(encoding='utf-8')
            originals[relative] = raw
            if path.resolve() not in included and path != ws.ledger_main: continue
            adjusted = re.sub(
                r'(?m)^(\d{4}-\d{2}-\d{2})([ \t]+open[ \t]+)',
                lambda match: min(match[1], str(first)) + match[2], raw,
            )
            if adjusted != raw: plan[relative] = adjusted
        accounts_relative = str(accounts.relative_to(ws.root))
        if missing_accounts:
            plan[accounts_relative] = plan.get(accounts_relative, originals[accounts_relative]) + (
                '\n; Accounts mapped from imported ledger history\n' +
                '\n'.join(f'{first} open {account}  {currency}' for account in sorted(missing_accounts)) + '\n'
            )
        main_relative = str(ws.ledger_main.relative_to(ws.root))
        main = plan.get(main_relative, originals[main_relative])
        # New year files must remain visible to the agent's normal workflow.
        main = re.sub(r'(?m)^\s*include\s+"years/[^"\n]+\.beancount"[^\n]*\n?', '', main)
        history_relative = str((ws.ledger_root / 'migrations' / f'{stage_id}.beancount').relative_to(ws.root))
        history_path = ws.root / history_relative
        if history_path.exists() or history_path.is_symlink() or history_path.parent.is_symlink():
            raise MigrationError("The migration history destination already exists or is unsafe.")
        # Refuse an existing broad include that could expose a newly written
        # history file before the main-file commit point.
        for raw in originals.values():
            if re.search(r'^\s*include\s+"[^"\n]*(?:\*\*|migrations/)', raw, re.M):
                raise MigrationError("Existing recursive or migration includes require a fresh workspace.")
        plan[history_relative] = history
        plan[main_relative] = main.rstrip() + f'\n\ninclude "years/*.beancount"\ninclude "migrations/{stage_id}.beancount"\n'
        stage, _ = self.stage(stage_id)
        with tempfile.TemporaryDirectory(prefix='.verify-', dir=stage) as directory:
            tree = Path(directory)
            for relative, content in {**originals, **plan}.items():
                destination = tree / Path(relative).relative_to(ws.ledger_root.relative_to(ws.root))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding='utf-8')
            entries, errors, _ = loader.load_file(str(tree / ws.ledger_main.name))
            if errors:
                raise MigrationError("Proposed ledger includes failed validation: " + '; '.join(e.message for e in errors[:4]))
            def ledger_values(entries):
                return [(entry.date, entry.meta.get('source-journal'),
                         [(p.account, p.units.number, p.units.currency) for p in entry.postings])
                        for entry in entries if isinstance(entry, Transaction)]
            if ledger_values(entries) != ledger_values(converted):
                raise MigrationError("Proposed ledger include coverage does not match the reviewed journals.")
        return plan

    @staticmethod
    def _invalidate(stage):
        for name in ('report.json', 'candidate.beancount', 'mapping.json', 'ledger-plan.json'):
            (stage / name).unlink(missing_ok=True)

    def preview(self, stage_id: str, spec: dict) -> dict:
        with self._lock():
            stage, _ = self.stage(stage_id)
            if self.report(stage_id).get('status') in ('applied', 'applying'):
                raise MigrationError("This migration is applied or interrupted during apply. Its evidence cannot be replaced.")
            self._invalidate(stage)
            try:
                if isinstance(spec, dict) and (stage / 'draft.json').exists():
                    draft = self.draft(stage_id)
                    _write(stage / 'draft.json', _json_bytes({**draft, 'spec': spec}))
                report, candidate, plan = self._build(stage_id, spec)
            except MigrationError as exc:
                _write(stage / 'report.json', _json_bytes({'status': 'blocked', 'error': str(exc), 'limitations': LIMITATIONS}))
                raise
            _write(stage / 'candidate.beancount', candidate)
            _write(stage / 'mapping.json', _json_bytes(spec))
            _write(stage / 'ledger-plan.json', _json_bytes(plan))
            _write(stage / 'report.json', _json_bytes(report))
            return report

    def report(self, stage_id: str) -> dict:
        stage, _ = self.stage(stage_id)
        path = stage / 'report.json'
        if not path.exists(): return {}
        report = self._read_json(path)
        # Recover a process stop after the single commit point, before recording
        # completion. The candidate's stage ID prevents confusing another import.
        if report.get('status') == 'applying' and report.get('ledger_files'):
            if all(self._plan_target(relative).is_file() and
                   _hash(self._plan_target(relative).read_bytes()) == sha
                   for relative, sha in report['ledger_files'].items()):
                report['status'] = 'applied'
        return report

    def _plan_target(self, relative: str) -> Path:
        if not isinstance(relative, str):
            raise MigrationError("Invalid migration ledger path.")
        target = self.workspace.root / relative
        if (Path(relative).is_absolute() or '..' in Path(relative).parts or target.is_symlink()
                or not target.resolve().is_relative_to(self.workspace.ledger_root.resolve())):
            raise MigrationError("Migration ledger paths must remain inside the workspace ledger.")
        return target

    def apply(self, stage_id: str, digest: str, *, confirm: str) -> dict:
        if confirm != 'apply':
            raise MigrationError("Explicitly confirm Apply ledger history after reviewing the preview.")
        with self._lock():
            stage, _ = self.stage(stage_id)
            report = self.report(stage_id)
            if report.get('status') != 'ready':
                raise MigrationError("A ready preview is required; this migration is blocked, already applied, or interrupted.")
            if not isinstance(digest, str) or not secrets.compare_digest(report['digest'], digest):
                raise MigrationError("The preview changed. Reload and review it before applying.")
            spec = self._read_json(stage / 'mapping.json')
            checked, candidate, plan = self._build(stage_id, spec)
            path = stage / 'candidate.beancount'
            if (checked['digest'] != digest or path.is_symlink() or not path.exists()
                    or _hash(path.read_bytes()) != checked['candidate_sha256']
                    or self._read_json(stage / 'ledger-plan.json') != plan):
                raise MigrationError("The workspace, evidence, mapping, or candidate changed after preview. Create a new preview.")
            # Retain all input ledger/config files before the one-file commit.
            originals = stage / 'before'
            originals.mkdir(exist_ok=True, mode=0o700)
            for relative, sha in checked['workspace_signature'].items():
                if sha is not None:
                    target = originals / relative
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    _write(target, (self.workspace.root / relative).read_bytes())
            # Recheck immediately before writing; no ledger files have changed yet.
            _, _, current = self._ledger_state()
            if current != checked['workspace_signature']:
                _write(stage / 'report.json', _json_bytes({**checked, 'status': 'blocked', 'error': 'Workspace changed during apply.'}))
                raise MigrationError("Workspace changed during apply. Create a new preview.")
            report = {**checked, 'status': 'applying'}
            _write(stage / 'report.json', _json_bytes(report))
            main_relative = str(self.workspace.ledger_main.relative_to(self.workspace.root))
            changed = []
            try:
                # History is not included yet. Open dates are extended before
                # the main include is atomically switched on as the commit point.
                for relative in [key for key in plan if key != main_relative] + [main_relative]:
                    target = self._plan_target(relative)
                    previous = target.read_bytes() if target.exists() else None
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _write(target, plan[relative].encode('utf-8'))
                    changed.append((target, previous))
            except (OSError, MigrationError) as exc:
                rollback_errors = []
                for target, previous in reversed(changed):
                    try:
                        if previous is None: target.unlink(missing_ok=True)
                        else: _write(target, previous)
                    except OSError as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                if rollback_errors:
                    raise MigrationError("Apply was interrupted and file restoration failed. Keep this evidence and restore ledger files from its before/ directory before continuing.") from exc
                _write(stage / 'report.json', _json_bytes({**checked, 'status': 'blocked', 'error': 'Apply failed; prior ledger files restored.'}))
                raise MigrationError("Apply failed; prior ledger files were restored. Check disk space and permissions, then create a new preview.") from exc
            report['status'] = 'applied'
            report['applied_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            try:
                _write(stage / 'report.json', _json_bytes(report))
            except OSError:
                # The durable precommit report plus exact ledger hash makes this
                # success recoverable without posting a second time.
                report['report_notice'] = 'Ledger applied; reload the report to recover its completion state.'
            return report
