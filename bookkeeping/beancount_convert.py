"""Convert the verified Xero GL export into a beancount ledger.

Re-runnable: regenerates ledger/accounts.beancount and ledger/years/*.beancount
from backup_output CSVs. Run verify_conversion afterwards.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .gl import AccountResolver, group_ledger_rows, parse_amount, read_gl_rows

ADJUSTMENT_ACCOUNT = "Equity:Xero-Source-Adjustments"

CLASS_ROOTS = {
    "ASSET": "Assets",
    "LIABILITY": "Liabilities",
    "EQUITY": "Equity",
    "REVENUE": "Income",
    "EXPENSE": "Expenses",
}

TYPE_SEGMENTS = {
    ("ASSET", "BANK"): "Bank",
    ("ASSET", "FIXED"): "Fixed",
    ("ASSET", "CURRENT"): "Current",
    ("LIABILITY", "CURRLIAB"): "Current",
    ("LIABILITY", "TERMLIAB"): "Term",
    ("LIABILITY", "LIABILITY"): "Current",
}


def component(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned:
        cleaned = "Account"
    if not cleaned[0].isupper() and not cleaned[0].isdigit():
        cleaned = cleaned[0].upper() + cleaned[1:]
    if cleaned[0].isdigit():
        cleaned = "X" + cleaned
    return cleaned


def beancount_account_name(account: dict[str, str]) -> str:
    klass = account.get("Class", "").upper()
    acc_type = account.get("Type", "").upper()
    bank_type = account.get("BankAccountType", "").upper()
    if acc_type == "BANK" and bank_type == "CREDITCARD":
        root = "Liabilities:Cards"
    else:
        root = CLASS_ROOTS.get(klass, "Assets")
        segment = TYPE_SEGMENTS.get((klass, acc_type))
        if segment:
            root = f"{root}:{segment}"
    name = account.get("Name", "")
    return f"{root}:{component(name)}"


def build_chart(accounts: list[dict[str, str]]) -> dict[str, str]:
    """Map Xero AccountID -> beancount account name, ensuring uniqueness."""
    used: dict[str, str] = {}
    chart: dict[str, str] = {}
    for account in accounts:
        name = beancount_account_name(account)
        base = name
        suffix = 2
        while name in used and used[name] != account["AccountID"]:
            name = f"{base}-{suffix}"
            suffix += 1
        used[name] = account["AccountID"]
        chart[account["AccountID"]] = name
    return chart


def format_amount(value: Decimal) -> str:
    return f"{value:.2f}"


def quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Converter:
    def __init__(
        self,
        backup_root: Path,
        ledger_root: Path,
        title: str = "Books",
        currency: str = "USD",
        uncategorized: tuple[str, str] = ("Expenses:Uncategorized", "Income:Uncategorized"),
        state_db: Path | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self.state_db = state_db
        self.base_dir = base_dir or Path.cwd()
        self.backup_root = backup_root
        self.ledger_root = ledger_root
        self.title = title
        self.currency = currency
        self.uncategorized = uncategorized
        self.accounts = self._read_csv(backup_root / "csv" / "accounts.csv")
        self.resolver = AccountResolver(self.accounts)
        self.chart = build_chart(self.accounts)
        self.gl_rows = read_gl_rows(backup_root / "csv" / "general_ledger_merged.csv")
        self.stats: dict[str, object] = {}

    @staticmethod
    def _read_csv(path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    def account_for_label(self, label: str) -> str:
        resolved = self.resolver.resolve(label)
        if resolved is None:
            raise ValueError(f"GL account label not found in chart: {label!r}")
        return self.chart[resolved["AccountID"]]

    def convert(self) -> None:
        groups = group_ledger_rows(self.gl_rows)
        balanced = [g for g in groups if g["balanced"]]
        exceptions = [g for g in groups if not g["balanced"]]

        self._prepare_documents()

        entries_by_year: dict[int, list[str]] = defaultdict(list)
        used_labels: set[str] = set()
        for group in groups:
            text, year, labels = self._render_group(group)
            entries_by_year[year].append(text)
            used_labels.update(labels)

        min_date = min(r["Date"] for r in self.gl_rows)
        max_date = max(r["Date"] for r in self.gl_rows)
        assertions = self._balance_assertions(max_date)
        for year, text in assertions:
            entries_by_year[year].append(text)

        documents = self._document_directives()
        for year, text in documents:
            entries_by_year[year].append(text)
        if documents:
            earliest_doc = min(text[:10] for _year, text in documents)
            min_date = min(min_date, earliest_doc)

        self._write_accounts_file(min_date, used_labels)
        self._write_year_files(entries_by_year)
        self._write_main(sorted(entries_by_year))

        self.stats = {
            "gl_rows": len(self.gl_rows),
            "groups": len(groups),
            "balanced_groups": len(balanced),
            "exception_groups": len(exceptions),
            "exception_details": [
                {
                    "number": g["number"],
                    "date": g["rows"][0].get("Date"),
                    "source": g["rows"][0].get("Source"),
                    "debit": str(g["debit"]),
                    "credit": str(g["credit"]),
                }
                for g in exceptions
            ],
            "gl_debit_total": str(sum(parse_amount(r.get("Debit")) for r in self.gl_rows)),
            "gl_credit_total": str(sum(parse_amount(r.get("Credit")) for r in self.gl_rows)),
            "date_range": [min_date, max_date],
            "documents_total": len(self._doc_manifest),
            "documents_txn_linked": self._docs_txn_linked,
            "documents_account_level": self._docs_account_level,
        }
        manifest_out = self.ledger_root / "documents" / "migration_manifest.json"
        if self._doc_manifest:
            manifest_out.parent.mkdir(parents=True, exist_ok=True)
            manifest_out.write_text(
                json.dumps(self._doc_manifest, indent=1), encoding="utf-8"
            )

    def _render_group(self, group: dict) -> tuple[str, int, set[str]]:
        rows = group["rows"]
        txn_date = rows[0].get("Date", "")
        year = int(txn_date[:4])
        narration = Counter(
            (row.get("Description") or "").strip() for row in rows
        ).most_common(1)[0][0] or rows[0].get("Source", "")
        reference = next((row.get("Reference", "").strip() for row in rows if row.get("Reference", "").strip()), "")
        flag = "*" if group["balanced"] else "!"

        lines = [f"{txn_date} {flag} {quote(narration)}"]
        lines.append(f"  xero-je: {quote(group['number'])}")
        lines.append(f"  xero-source: {quote(rows[0].get('Source', ''))}")
        if reference:
            lines.append(f"  xero-reference: {quote(reference)}")

        labels: set[str] = set()
        posting_lines: list[str] = []
        doc_paths: list[str] = []
        for row in rows:
            label = row.get("Account", "")
            account = self.account_for_label(label)
            labels.add(label)
            amount = parse_amount(row.get("Debit")) - parse_amount(row.get("Credit"))
            posting = f"  {account}  {format_amount(amount)} {self.currency}"
            description = (row.get("Description") or "").strip()
            posting_lines.append(posting)
            if description and description != narration:
                posting_lines.append(f"    desc: {quote(description)}")
            doc_paths += self._pop_txn_documents(txn_date, account, amount, reference)

        for index, path in enumerate(doc_paths):
            key = "document" if index == 0 else f"document-{index + 1}"
            lines.append(f"  {key}: {quote(path)}")
        self._docs_txn_linked += len(doc_paths)
        lines += posting_lines

        if not group["balanced"]:
            gap = group["credit"] - group["debit"]
            lines.append(f"  {ADJUSTMENT_ACCOUNT}  {format_amount(gap)} {self.currency}")
            lines.append(
                '    note: "Balancing entry: Xero GL source export is unbalanced for this '
                'payment (known source-data defect, see CONVERSION.md)"'
            )
        return "\n".join(lines) + "\n", year, labels

    def _balance_assertions(self, max_date: str) -> list[tuple[int, str]]:
        """Year-boundary + post-cutover assertions for bank and card accounts."""
        bank_labels: dict[str, str] = {}
        for row in self.gl_rows:
            label = row.get("Account", "")
            if label in bank_labels:
                continue
            resolved = self.resolver.resolve(label)
            if resolved and resolved.get("Type", "").upper() == "BANK":
                bank_labels[label] = self.chart[resolved["AccountID"]]

        running: dict[str, Decimal] = defaultdict(Decimal)
        per_year_end: dict[int, dict[str, Decimal]] = {}
        rows = sorted(self.gl_rows, key=lambda r: r.get("Date", ""))
        current_year = int(rows[0]["Date"][:4])
        for row in rows:
            year = int(row["Date"][:4])
            while year > current_year:
                per_year_end[current_year] = dict(running)
                current_year += 1
            label = row.get("Account", "")
            if label in bank_labels:
                running[label] += parse_amount(row.get("Debit")) - parse_amount(row.get("Credit"))
        per_year_end[current_year] = dict(running)

        assertions: list[tuple[int, str]] = []
        for year, balances in sorted(per_year_end.items()):
            assert_date = date(year + 1, 1, 1)
            if year == current_year:
                last = date.fromisoformat(max_date)
                assert_date = last + timedelta(days=1)
            for label, balance in sorted(balances.items()):
                account = bank_labels[label]
                text = (
                    f"{assert_date.isoformat()} balance {account}  "
                    f"{format_amount(balance)} {self.currency}\n"
                )
                assertions.append((assert_date.year if year == current_year else year, text))
        return assertions

    def _load_attachment_rows(self) -> list[dict]:
        manifest_path = self.backup_root / "csv" / "attachments_manifest.csv"
        if not manifest_path.exists():
            return []
        manifest = self._read_csv(manifest_path)
        # The CSV manifest's downloaded/local_path columns are stale for 371 of 635
        # rows; the sync state DB is authoritative. Overlay its final paths.
        state_db = self.state_db
        if state_db is not None and state_db.exists():
            import sqlite3

            connection = sqlite3.connect(state_db)
            connection.row_factory = sqlite3.Row
            final = {
                (row["entity_id"], row["file_name"]): row["local_path"]
                for row in connection.execute(
                    "SELECT entity_id, file_name, local_path FROM attachments_queue WHERE downloaded = 1"
                )
            }
            connection.close()
            for row in manifest:
                key = (row.get("entity_id", ""), row.get("file_name", ""))
                if key in final and final[key]:
                    row["local_path"] = final[key]
                    row["downloaded"] = "1"

        bank_txn_info: dict[str, dict] = {}
        bank_txn_csv = self.backup_root / "csv" / "bank_transactions.csv"
        if bank_txn_csv.exists():
            for row in self._read_csv(bank_txn_csv):
                name = row.get("BankAccount.Name", "")
                resolved = self.resolver.by_name.get(name)
                if resolved:
                    bank_txn_info[row.get("BankTransactionID", "")] = {
                        "account": self.chart[resolved["AccountID"]],
                        "date": (row.get("DateString") or "").split("T")[0],
                        "total": row.get("Total", ""),
                        "reference": row.get("Reference", ""),
                    }

        sales = next(
            (a for a in self.accounts if a.get("Name", "").lower() == "sales"), None
        )
        sales_account = self.chart[sales["AccountID"]] if sales else None

        records: list[dict] = []
        for row in manifest:
            local_path = row.get("local_path", "")
            if not local_path or row.get("downloaded") != "1":
                continue
            path = Path(local_path)
            if not path.is_absolute():
                path = self.base_dir / path
            if not path.exists():
                continue
            entity_type = row.get("entity_type", "")
            entity_id = row.get("entity_id", "")
            txn = bank_txn_info.get(entity_id) if entity_type == "bank_transactions" else None
            if txn:
                account = txn["account"]
                raw_date = txn["date"]
            elif entity_type == "invoices":
                account = sales_account
                raw_date = (row.get("date") or "").split("T")[0]
            else:
                account = None
                raw_date = ""
            if account is None or not raw_date:
                continue
            records.append(
                {
                    "path": path,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "account": account,
                    "date": raw_date,
                    "total": txn["total"] if txn else "",
                    "reference": txn["reference"] if txn else "",
                }
            )
        return records

    def _relative(self, path: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.base_dir.resolve()))
        except ValueError:
            return str(path)

    def _migrate_document(self, record: dict) -> Path:
        """Copy an attachment into ledger/documents/<Account/Path>/<date>.<name>.

        Idempotent; on a name collision with a different file, the entity GUID
        prefix disambiguates. Returns the destination path.
        """
        import shutil

        source: Path = record["path"]
        account_dir = self.ledger_root / "documents" / record["account"].replace(":", "/")
        dest = account_dir / f"{record['date']}.{source.name}"
        if dest.exists() and dest.stat().st_size != source.stat().st_size:
            dest = account_dir / f"{record['date']}.{record['entity_id'][:8]}.{source.name}"
        if not dest.exists():
            account_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        return dest

    def _prepare_documents(self) -> None:
        """Migrate attachments into ledger/documents and index them for
        transaction-level linking during rendering.

        Bank-transaction attachments are keyed by (date, account, |amount|) so
        _render_group can attach them as `document:` metadata on the matching
        transaction; whatever is left over (plus invoice attachments) becomes an
        account-level `document` directive.
        """
        self._txn_docs: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        self._account_docs: list[dict] = []
        self._doc_manifest: list[dict] = []
        self._docs_txn_linked = 0
        self._docs_account_level = 0
        for record in self._load_attachment_rows():
            dest = self._migrate_document(record)
            # Paths are written relative to ledger/years/<year>.beancount.
            record["relpath"] = str(Path("..") / dest.relative_to(self.ledger_root))
            self._doc_manifest.append(
                {
                    "source": self._relative(record["path"]),
                    "dest": self._relative(dest),
                    "entity_type": record["entity_type"],
                    "entity_id": record["entity_id"],
                }
            )
            total = parse_amount(record["total"]) if record["total"] else None
            if record["entity_type"] == "bank_transactions" and total is not None:
                key = (record["date"], record["account"], format_amount(abs(total)))
                self._txn_docs[key].append(record)
            else:
                self._account_docs.append(record)

    def _pop_txn_documents(
        self, date: str, account: str, amount, reference: str
    ) -> list[str]:
        """Documents matching a rendered posting, preferring a reference match."""
        key = (date, account, format_amount(abs(amount)))
        candidates = self._txn_docs.get(key)
        if not candidates:
            return []
        candidates.sort(
            key=lambda r: 0 if reference and r["reference"] == reference else 1
        )
        paths = [r["relpath"] for r in candidates]
        del self._txn_docs[key]
        return paths

    def _document_directives(self) -> list[tuple[int, str]]:
        """Account-level directives for invoice attachments and any
        bank-transaction attachments that found no matching transaction."""
        leftovers = [r for rows in self._txn_docs.values() for r in rows]
        directives: list[tuple[int, str]] = []
        for record in self._account_docs + leftovers:
            year = int(record["date"][:4])
            directives.append(
                (
                    year,
                    f"{record['date']} document {record['account']} "
                    f"{quote(record['relpath'])}\n",
                )
            )
        self._docs_account_level = len(directives)
        return directives

    def _write_accounts_file(self, min_date: str, used_labels: set[str]) -> None:
        open_date = min_date
        lines = ["; Chart of accounts converted from Xero (see CONVERSION.md)\n"]
        for account in sorted(self.accounts, key=lambda a: self.chart[a["AccountID"]]):
            name = self.chart[account["AccountID"]]
            lines.append(f"{open_date} open {name}  {self.currency}")
            if account.get("Code"):
                lines.append(f"  xero-code: {quote(account['Code'])}")
            lines.append(f"  xero-account-id: {quote(account['AccountID'])}")
            lines.append(f"  xero-name: {quote(account.get('Name', ''))}")
            lines.append("")
        lines.append(f"{open_date} open {ADJUSTMENT_ACCOUNT}  {self.currency}")
        lines.append(
            '  note: "Absorbs documented imbalances present in the Xero source GL export"'
        )
        lines.append("")
        # Landing accounts for uncategorized feed transactions (books propose).
        for account in self.uncategorized:
            lines.append(f"{open_date} open {account}  {self.currency}")
        lines.append("")
        path = self.ledger_root / "accounts.beancount"
        path.write_text("\n".join(lines), encoding="utf-8")

    def _write_year_files(self, entries_by_year: dict[int, list[str]]) -> None:
        years_dir = self.ledger_root / "years"
        years_dir.mkdir(parents=True, exist_ok=True)
        for stale in years_dir.glob("*.beancount"):
            if int(stale.stem) not in entries_by_year:
                stale.unlink()
        for year, entries in sorted(entries_by_year.items()):
            def sort_key(entry: str) -> tuple[str, int]:
                first_line = entry.split("\n", 1)[0]
                # documents/balances after transactions on the same date
                kind = 1 if (" balance " in first_line or " document " in first_line) else 0
                return (first_line[:10], kind)

            body = "\n".join(sorted(entries, key=sort_key))
            (years_dir / f"{year}.beancount").write_text(
                f"; Converted from Xero GL — regenerate with `books convert`\n\n{body}",
                encoding="utf-8",
            )

    def _write_main(self, years: list[int]) -> None:
        lines = [
            f"option \"title\" {quote(self.title)}",
            f"option \"operating_currency\" \"{self.currency}\"",
            "",
            *(
                ['include "fava.beancount"']
                if (self.ledger_root / "fava.beancount").exists()
                else []
            ),
            'include "accounts.beancount"',
        ]
        for year in years:
            lines.append(f'include "years/{year}.beancount"')
        lines.append("")
        (self.ledger_root / "main.beancount").write_text("\n".join(lines), encoding="utf-8")


def find_state_db(workspace, backup_root: Path) -> Path | None:
    """The backup's sync-state database: authoritative attachment paths."""
    for candidate in (
        backup_root.parent / "state.db",
        workspace.root / "xero_sync_state.db",
        backup_root.parent.parent / "xero_sync_state.db",
    ):
        if candidate.exists():
            return candidate
    return None


def convert(workspace, backup_root: Path | None = None) -> dict[str, object]:
    ledger_root = workspace.ledger_root
    ledger_root.mkdir(parents=True, exist_ok=True)
    backup_root = backup_root or workspace.xero_backup
    converter = Converter(
        backup_root,
        ledger_root,
        title=workspace.company.name,
        currency=workspace.currency,
        uncategorized=(
            workspace.chart.uncategorized_expense,
            workspace.chart.uncategorized_income,
        ),
        state_db=find_state_db(workspace, backup_root),
        base_dir=workspace.root,
    )
    converter.convert()
    return converter.stats
