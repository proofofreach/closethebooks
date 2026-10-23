"""Explicit, content-bound bank CSV interpretation.

Column values are exact header names. Required options are date_format
(iso/mdy/dmy), delimiter (,/;/tab), number_format (dot/comma), and amount_sign
(inflow/outflow: positive values mean that direction). Select amount OR both
debit and credit. Split amounts are unsigned and already have bank direction;
amount_sign only affects a single signed amount column, never a balance.
currency selects a column; expected_currency optionally constrains its values.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .statements import FeedTransaction, ParsedStatement

_COLUMNS = ("date", "description", "amount", "debit", "credit", "reference", "balance", "currency")
_OPTIONS = {
    "date_format": {"iso", "mdy", "dmy"},
    "number_format": {"dot", "comma"},
    "amount_sign": {"inflow", "outflow"},
}
_DELIMITERS = {",": ",", ";": ";", "\t": "\t", "comma": ",", "semicolon": ";", "tab": "\t"}
_VERSION = 1


class CsvMappingError(ValueError):
    """All recoverable validation errors, with physical CSV line numbers."""

    def __init__(self, path: Path, errors: list[str]):
        self.errors = errors
        super().__init__(f"{path.name}: " + "\n".join(errors))


def _delimiter(value: object) -> str:
    if not isinstance(value, str) or value not in _DELIMITERS:
        raise ValueError("delimiter must be comma (,), semicolon (;), or tab.")
    return _DELIMITERS[value]


def _read_csv(path: Path, content: bytes, delimiter: str) -> tuple[list[str], list[tuple[int, list[str]]], list[str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        line = content[:exc.start].count(b"\n") + 1
        raise CsvMappingError(path, [f"line {line}: CSV must use UTF-8 encoding."]) from exc
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    errors: list[str] = []
    headers: list[str] = []
    rows: list[tuple[int, list[str]]] = []
    try:
        headers = next(reader, [])
        if not headers:
            raise CsvMappingError(path, ["line 1: empty CSV; headers and transactions are required."])
        seen = set()
        for header in headers:
            normalized = header.strip().casefold()
            if not normalized:
                errors.append("line 1: blank column header.")
            elif normalized in seen:
                errors.append(f"line 1: duplicate column header {header!r}.")
            seen.add(normalized)
        while True:
            line = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                break
            if len(row) != len(headers):
                errors.append(f"line {line}: expected {len(headers)} columns, found {len(row)}.")
            else:
                rows.append((line, row))
    except csv.Error as exc:
        errors.append(f"line {reader.line_num}: malformed CSV: {exc}.")
    if not rows and not errors:
        errors.append("line 2: empty CSV; at least one transaction is required.")
    return headers, rows, errors


def inspect_csv(path: Path, delimiter: str = ",") -> dict:
    """Return headers, up to five sample row dictionaries, and data row_count.

    Read the entire CSV to reject structural errors, including rows beyond the
    sample. Semantic validation requires parse_mapped_csv or save_mapping.
    """
    path = Path(path)
    headers, rows, errors = _read_csv(path, path.read_bytes(), _delimiter(delimiter))
    if errors:
        raise CsvMappingError(path, errors)
    return {"headers": headers, "sample": [dict(zip(headers, row)) for _, row in rows[:5]], "row_count": len(rows)}


def _normalize_mapping(mapping: dict) -> dict:
    if not isinstance(mapping, dict):
        raise ValueError("CSV mapping must be a dictionary.")
    unknown = set(mapping) - set(_COLUMNS) - set(_OPTIONS) - {"delimiter", "expected_currency", "balance_order"}
    if unknown:
        raise ValueError(f"Unknown CSV mapping fields: {sorted(map(str, unknown))}.")
    result = {}
    for field in _COLUMNS:
        value = mapping.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"{field} column must be a header name.")
        result[field] = value
    for field in ("date", "description"):
        if field not in result:
            raise ValueError(f"{field} column is required.")
    if "amount" in result:
        if "debit" in result or "credit" in result:
            raise ValueError("Select amount OR debit/credit columns, not both.")
    elif "debit" not in result or "credit" not in result:
        raise ValueError("Select an amount column or both debit and credit columns.")
    for field, allowed in _OPTIONS.items():
        value = mapping.get(field)
        if field == 'amount_sign' and 'amount' not in result and value in (None, ''):
            value = 'inflow'  # Split columns already define direction.
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"{field} must be explicitly selected from {', '.join(sorted(allowed))}.")
        result[field] = value
    if "balance" in result:
        if mapping.get("balance_order") not in {"oldest_first", "newest_first"}:
            raise ValueError("Choose balance row order when a balance column is mapped.")
        result["balance_order"] = mapping["balance_order"]
    result["delimiter"] = _delimiter(mapping.get("delimiter"))
    if mapping.get("expected_currency") not in (None, ""):
        currency = mapping["expected_currency"]
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("expected_currency must be a three-letter uppercase currency code.")
        result["expected_currency"] = currency
    return result


def _date(value: str, date_format: str) -> str:
    if not value:
        raise ValueError("missing date")
    if date_format == "iso":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("date must use YYYY-MM-DD (iso)")
        pattern = "%Y-%m-%d"
    else:
        if not re.fullmatch(r"\d{1,2}([/-])\d{1,2}\1\d{4}", value):
            raise ValueError(f"date must use {'MM/DD/YYYY' if date_format == 'mdy' else 'DD/MM/YYYY'}")
        separator = "/" if "/" in value else "-"
        pattern = separator.join(("%m", "%d", "%Y") if date_format == "mdy" else ("%d", "%m", "%Y"))
    try:
        return datetime.strptime(value, pattern).date().isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid date {value!r} for {date_format}") from exc


def _number(value: str, number_format: str) -> Decimal:
    if not value:
        raise ValueError("missing amount")
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1]
        if value.startswith(("+", "-")):
            raise ValueError("invalid amount sign")
    point, group = (".", ",") if number_format == "dot" else (",", ".")
    integer = rf"(?:[0-9]+|[0-9]{{1,3}}(?:{re.escape(group)}[0-9]{{3}})+)"
    if not re.fullmatch(rf"[+-]?{integer}(?:{re.escape(point)}[0-9]+)?", value):
        raise ValueError(f"invalid finite amount {value!r} for {number_format} number_format")
    value = value.replace(group, "").replace(point, ".")
    # Inspect decimal digits directly; no context-dependent arithmetic or rounding.
    if "." in value and any(digit != "0" for digit in value.split(".")[1][2:]):
        raise ValueError("amount precision exceeds two decimal places; rounding is not allowed")
    amount = Decimal(value)
    return amount.copy_negate() if negative else amount


def _parse(path: Path, content: bytes, mapping: dict) -> ParsedStatement:
    mapping = _normalize_mapping(mapping)
    headers, rows, errors = _read_csv(path, content, mapping["delimiter"])
    selected = [mapping[field] for field in _COLUMNS if field in mapping]
    for column in selected:
        if column not in headers:
            errors.append(f"line 1: selected column {column!r} does not exist.")
    if len(selected) != len(set(selected)):
        errors.append("line 1: each selected column must have only one mapping role.")
    # Header errors make interpretation unsafe; row errors remain recoverable.
    if any(error.startswith("line 1:") for error in errors):
        raise CsvMappingError(path, errors)
    digest = hashlib.sha256(content).hexdigest()
    transactions = []
    ending_balance = None
    ending_balance_date = None
    references: dict[str, int] = {}
    last_transaction_date = None
    for index, (line, values) in enumerate(rows, start=1):
        row = dict(zip(headers, values))
        before = len(errors)

        def cell(field):
            return row[mapping[field]].strip() if field in mapping else ""

        def validate(field, operation):
            try:
                return operation()
            except ValueError as exc:
                errors.append(f"line {line}: {field}: {exc}.")
                return None

        date = validate("date", lambda: _date(cell("date"), mapping["date_format"]))
        if "amount" in mapping:
            amount = validate("amount", lambda: _number(cell("amount"), mapping["number_format"]))
            if amount is not None and mapping["amount_sign"] == "outflow":
                amount = amount.copy_negate()
        else:
            debit = validate("debit amount", lambda: _number(cell("debit"), mapping["number_format"])) if cell("debit") else Decimal(0)
            credit = validate("credit amount", lambda: _number(cell("credit"), mapping["number_format"])) if cell("credit") else Decimal(0)
            amount = None
            if not cell("debit") and not cell("credit"):
                errors.append(f"line {line}: missing debit/credit amount.")
            elif debit is not None and credit is not None:
                if debit < 0 or credit < 0:
                    errors.append(f"line {line}: debit/credit amounts must be unsigned.")
                elif debit and credit:
                    errors.append(f"line {line}: debit and credit amounts cannot both be nonzero.")
                else:
                    amount = debit.copy_negate() if debit else credit
        balance = validate("balance", lambda: _number(cell("balance"), mapping["number_format"])) if cell("balance") else None
        reference = cell("reference")
        if reference:
            if reference in references:
                errors.append(f"line {line}: duplicate reference {reference!r} (first on line {references[reference]}).")
            else:
                references[reference] = line
        if "currency" in mapping:
            currency = cell("currency")
            if not re.fullmatch(r"[A-Z]{3}", currency):
                errors.append(f"line {line}: currency must be a three-letter uppercase code.")
            elif mapping.get("expected_currency") and currency != mapping["expected_currency"]:
                errors.append(f"line {line}: currency {currency} does not match expected currency {mapping['expected_currency']}.")
        if "balance" in mapping and date is not None:
            if last_transaction_date is not None and (
                    (mapping['balance_order'] == 'oldest_first' and date < last_transaction_date)
                    or (mapping['balance_order'] == 'newest_first' and date > last_transaction_date)):
                errors.append(f"line {line}: dates conflict with the selected balance row order.")
            last_transaction_date = date
        if len(errors) != before:
            continue
        transactions.append(FeedTransaction(
            txn_id=reference or f"csv-{digest}-{index}", date=date, amount=amount,
            description=cell("description") or reference or f"Statement row {index}",
            source="statement", raw=row,
        ))
        if balance is not None and (ending_balance_date is None or date > ending_balance_date
                                   or (date == ending_balance_date and mapping["balance_order"] == "oldest_first")):
            ending_balance, ending_balance_date = balance, date
    if errors:
        raise CsvMappingError(path, errors)
    semantics = {"version": _VERSION, **mapping}
    fingerprint = hashlib.sha256(json.dumps(semantics, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ParsedStatement(path, "csv", transactions, ending_balance, ending_balance_date,
                           csv_mapping=semantics, csv_mapping_sha256=fingerprint,
                           source_sha256=digest)


def parse_mapped_csv(path: Path, mapping: dict) -> ParsedStatement:
    """Validate all rows before returning transactions; never round amounts.

    Raises ValueError (CsvMappingError for line errors). No partial result or
    write occurs on failure. Anonymous IDs use raw SHA-256 and data row index.
    """
    path = Path(path)
    return _parse(path, path.read_bytes(), mapping)


def load_mapping(path: Path) -> dict | None:
    """Load a saved mapping for these exact bytes, or None if none is saved.

    Invalid or tampered sidecars raise ValueError; do not fall back to presets.
    A rename within the same directory keeps the binding; edits do not.
    """
    path = Path(path)
    return _load_mapping(path, path.read_bytes())


def _load_mapping(path: Path, content: bytes) -> dict | None:
    digest = hashlib.sha256(content).hexdigest()
    mapping_path = path.parent / ".csv-mappings" / f"{digest}.json"
    try:
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{path.name}: saved CSV mapping is unreadable.") from exc
    if not isinstance(payload, dict) or payload.get("sha256") != digest:
        raise ValueError(f"{path.name}: saved CSV mapping content hash does not match.")
    if payload.get("version") != _VERSION:
        raise ValueError(f"{path.name}: unsupported saved CSV mapping version.")
    return _normalize_mapping(payload.get("mapping"))


def _parse_saved_csv(path: Path, expected_currency: str | None) -> ParsedStatement | None:
    # Loading the sidecar and parsing must use the same source snapshot.
    content = path.read_bytes()
    mapping = _load_mapping(path, content)
    if mapping is None:
        return None
    if expected_currency:
        if mapping.get("expected_currency", expected_currency) != expected_currency:
            raise ValueError(f"{path.name}: CSV mapping expected currency does not match account currency {expected_currency}.")
        mapping = {**mapping, "expected_currency": expected_currency}
    return _parse(path, content, mapping)


def save_mapping(path: Path, mapping: dict) -> None:
    """Validate the complete source, then atomically save its normalized mapping.

    Sidecar: path.parent/.csv-mappings/<raw-content-sha256>.json. A failed
    validation does not create a directory or change a previous mapping.
    """
    path = Path(path)
    content = path.read_bytes()
    normalized = _normalize_mapping(mapping)
    _parse(path, content, normalized)
    digest = hashlib.sha256(content).hexdigest()
    if path.read_bytes() != content:
        raise ValueError(f"{path.name}: CSV changed during mapping validation; inspect it again.")
    destination = path.parent / ".csv-mappings" / f"{digest}.json"
    payload = json.dumps({"version": _VERSION, "sha256": digest, "mapping": normalized}, indent=2, sort_keys=True) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
