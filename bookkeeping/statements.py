"""Bank statement parsing (CSV/OFX/QBO/QFX/MT940/PDF) for the beancount ledger.

Output is normalized FeedTransaction records consumed by `books propose`.

Sign convention: amount is bank-normal — money INTO the account is positive.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from .pdf_extraction import PdfTextCandidate, extract_pdf_text_candidates

MONEY_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class FeedTransaction:
    txn_id: str
    date: str  # YYYY-MM-DD
    amount: Decimal  # signed, money-in positive
    description: str
    source: str  # "statement" | "plaid" | "teller"
    account_key: str = ""  # mapping key; filled by caller when known
    pending: bool = False
    raw: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class ParsedStatement:
    path: Path
    format: str
    transactions: list[FeedTransaction]
    ending_balance: Decimal | None
    ending_balance_date: str | None


# Column presets for known exports. Keys are lowercase normalized headers.
CSV_PRESETS: dict[str, dict[str, list[str]]] = {
    "paypal": {
        "date": ["date"],
        "description": ["name", "description"],
        "amount": ["gross", "amount"],
        "fee": ["fee"],
        "balance": ["balance"],
        "reference": ["transaction id"],
        "status": ["status"],
    },
}

GENERIC_ALIASES = {
    "date": ["date", "posted date", "transaction date", "processed date", "post date"],
    "amount": ["amount", "transaction amount", "net amount"],
    "debit": ["debit", "withdrawal", "paid out"],
    "credit": ["credit", "deposit", "paid in"],
    "description": ["description", "memo", "name", "payee", "details", "merchant"],
    "reference": ["reference", "transaction id", "id", "fitid", "reference number"],
    "balance": ["balance", "running balance", "running bal.", "balance after transaction"],
}


def detect_format(path: Path, requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"ofx", "qbo", "qfx"}:
        return "ofx"
    if suffix in {"sta", "swift", "mt940"}:
        return "mt940"
    if suffix == "pdf":
        return "pdf"
    return "csv"


def parse_statement(path: Path, input_format: str = "auto", preset: str | None = None) -> ParsedStatement:
    detected = detect_format(path, input_format)
    if detected == "csv":
        return parse_csv_statement(path, preset)
    if detected == "ofx":
        return parse_ofx_statement(path)
    if detected == "mt940":
        return parse_mt940_statement(path)
    if detected == "pdf":
        if preset != "quickbooks-checking":
            raise ValueError(
                f"{path.name}: PDF statements require a configured bank preset."
            )
        return parse_quickbooks_checking_pdf(path)
    raise ValueError(f"Unsupported statement format: {detected}")


def parse_csv_statement(path: Path, preset: str | None = None) -> ParsedStatement:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return ParsedStatement(path, "csv", [], None, None)
    headers = list(rows[0].keys())

    if preset == "paypal":
        return _parse_paypal(path, rows)

    date_column = pick_column(headers, GENERIC_ALIASES["date"])
    amount_column = pick_column(headers, GENERIC_ALIASES["amount"])
    debit_column = pick_column(headers, GENERIC_ALIASES["debit"])
    credit_column = pick_column(headers, GENERIC_ALIASES["credit"])
    description_column = pick_column(headers, GENERIC_ALIASES["description"])
    reference_column = pick_column(headers, GENERIC_ALIASES["reference"])
    balance_column = pick_column(headers, GENERIC_ALIASES["balance"])
    if not date_column or (not amount_column and not (debit_column or credit_column)):
        raise ValueError(
            f"{path.name}: could not locate date/amount columns (headers: {headers})"
        )

    transactions = []
    ending_balance = None
    ending_balance_date = None
    for index, row in enumerate(rows, start=1):
        amount = statement_amount(row, amount_column, debit_column, credit_column)
        date = date_string(row.get(date_column, ""))
        description = (
            row.get(description_column or "", "")
            or row.get(reference_column or "", "")
            or f"Statement row {index}"
        ).strip()
        reference = (row.get(reference_column or "", "") or "").strip()
        balance = row.get(balance_column or "", "").strip() if balance_column else ""
        txn_id = reference or synth_id(path.stem, date, amount, description, balance)
        transactions.append(
            FeedTransaction(
                txn_id=txn_id,
                date=date,
                amount=amount,
                description=description,
                source="statement",
                raw=dict(row),
            )
        )
        if balance and date:
            if ending_balance_date is None or date >= ending_balance_date:
                ending_balance_date = date
                ending_balance = money(balance)
    return ParsedStatement(path, "csv", transactions, ending_balance, ending_balance_date)


def _parse_paypal(path: Path, rows: list[dict[str, str]]) -> ParsedStatement:
    headers = list(rows[0].keys())
    cols = {
        key: pick_column(headers, aliases)
        for key, aliases in CSV_PRESETS["paypal"].items()
    }
    transactions: list[FeedTransaction] = []
    ending_balance = None
    ending_balance_date = None
    for row in rows:
        status = (row.get(cols["status"] or "", "") or "").strip().lower()
        if cols["status"] and status and status != "completed":
            continue
        date = date_string(row.get(cols["date"] or "", ""))
        name = (row.get(cols["description"] or "", "") or "").strip()
        gross = money(row.get(cols["amount"] or "", ""))
        fee = money(row.get(cols["fee"] or "", "")) if cols["fee"] else Decimal("0")
        reference = (row.get(cols["reference"] or "", "") or "").strip()
        balance = (row.get(cols["balance"] or "", "") or "").strip()
        base_id = reference or synth_id(path.stem, date, gross, name, balance)
        transactions.append(
            FeedTransaction(
                txn_id=base_id,
                date=date,
                amount=gross,
                description=name or f"PayPal {reference}",
                source="statement",
                raw=dict(row),
            )
        )
        if fee:
            # Fees hit the balance separately; keep them as their own transaction
            # so they can be categorized to an expense account.
            transactions.append(
                FeedTransaction(
                    txn_id=f"{base_id}-FEE",
                    date=date,
                    amount=fee,
                    description=f"PayPal fee: {name}".strip(),
                    source="statement",
                    raw=dict(row),
                )
            )
        if balance and date:
            if ending_balance_date is None or date >= ending_balance_date:
                ending_balance_date = date
                ending_balance = money(balance)
    return ParsedStatement(path, "csv", transactions, ending_balance, ending_balance_date)


def parse_ofx_statement(path: Path) -> ParsedStatement:
    text = path.read_text(encoding="utf-8", errors="replace")
    transactions = []
    for block in re.findall(
        r"<STMTTRN>(.*?)(?=<STMTTRN>|</BANKTRANLIST>|</CCSTMTRS>|$)", text, flags=re.I | re.S
    ):
        amount = money(ofx_tag(block, "TRNAMT"))
        posted_at = date_string(ofx_tag(block, "DTPOSTED")[:8])
        reference = ofx_tag(block, "FITID")
        description = ofx_tag(block, "MEMO") or ofx_tag(block, "NAME") or reference
        transactions.append(
            FeedTransaction(
                txn_id=reference or synth_id(path.stem, posted_at, amount, description, ""),
                date=posted_at,
                amount=amount,
                description=description.strip(),
                source="statement",
            )
        )
    ending_balance = None
    ending_balance_date = None
    ledger_match = re.search(
        r"<LEDGERBAL>.*?<BALAMT>([^<\r\n]+).*?<DTASOF>([^<\r\n]+)", text, flags=re.I | re.S
    )
    if ledger_match:
        ending_balance = money(ledger_match.group(1))
        ending_balance_date = date_string(ledger_match.group(2).strip()[:8])
    return ParsedStatement(path, "ofx", transactions, ending_balance, ending_balance_date)


def parse_mt940_statement(path: Path) -> ParsedStatement:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    transactions: list[FeedTransaction] = []
    pending_row: dict | None = None

    def flush() -> None:
        nonlocal pending_row
        if pending_row is None:
            return
        txn_id = pending_row["reference"] or synth_id(
            path.stem,
            pending_row["date"],
            pending_row["amount"],
            pending_row["description"],
            "",
        )
        transactions.append(
            FeedTransaction(
                txn_id=txn_id,
                date=pending_row["date"],
                amount=pending_row["amount"],
                description=pending_row["description"],
                source="statement",
            )
        )
        pending_row = None

    for line in lines:
        if line.startswith(":61:"):
            flush()
            match = re.match(
                r":61:(?P<date>\d{6})(?:\d{4})?(?P<sign>[DC])(?P<amount>[0-9,\.]+)", line
            )
            if not match:
                continue
            amount = money(match.group("amount").replace(",", "."))
            if match.group("sign") == "D":
                amount = -abs(amount)
            pending_row = {
                "date": mt940_date(match.group("date")),
                "amount": amount,
                "description": "",
                "reference": line[4:].strip(),
            }
        elif line.startswith(":86:") and pending_row is not None:
            pending_row["description"] = line[4:].strip()
    flush()
    return ParsedStatement(path, "mt940", transactions, None, None)


@dataclass(frozen=True)
class _PdfColumns:
    starts: tuple[tuple[str, int], ...]

    def fields(self, line: str) -> dict[str, str]:
        values = {}
        for index, (name, start) in enumerate(self.starts):
            end = self.starts[index + 1][1] if index + 1 < len(self.starts) else None
            values[name] = line[start:end].strip()
        return values


def parse_quickbooks_checking_pdf(path: Path) -> ParsedStatement:
    """Parse a QuickBooks Checking PDF only when its balances reconcile."""
    extraction = extract_pdf_text_candidates(path)
    parsed: list[tuple[ParsedStatement, str]] = []
    failures = []
    for candidate in extraction.candidates:
        try:
            parsed.append((_parse_quickbooks_candidate(path, candidate), candidate.mode))
        except ValueError as exc:
            failures.append(f"{candidate.mode}: {exc}")

    if parsed:
        parsed.sort(
            key=lambda item: (
                len(item[0].transactions),
                item[1] in {"poppler-layout", "pypdf-layout"},
            ),
            reverse=True,
        )
        return parsed[0][0]

    max_chars = max(
        (len(item.text) for item in extraction.candidates),
        default=0,
    )
    if extraction.page_count and max_chars < extraction.page_count * 80:
        raise ValueError(
            f"{path.name}: PDF appears scanned or image-only; OCR is required."
        )
    details = "; ".join(failures or extraction.errors) or "no text was extracted"
    raise ValueError(f"{path.name}: PDF statement did not reconcile: {details}")


def _parse_quickbooks_candidate(
    path: Path, candidate: PdfTextCandidate
) -> ParsedStatement:
    opening = _labeled_pdf_amount(candidate.text, ("beginning balance", "opening balance"))
    ending = _labeled_pdf_amount(candidate.text, ("ending balance", "closing balance"))
    if opening is None or ending is None:
        raise ValueError("beginning or ending balance was not found")

    columns = None
    rows: list[dict[str, object]] = []
    for line in candidate.text.splitlines():
        detected = _detect_pdf_columns(line)
        if detected:
            columns = detected
            continue
        date_match = re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", line)
        if columns is None or date_match is None:
            continue
        fields = columns.fields(line)
        date = date_string(fields.get("date", ""))
        if not date:
            continue
        description = " ".join(fields.get("description", "").split())
        if not description:
            description = "Statement transaction"
        positive = _optional_pdf_money(fields.get("positive", ""))
        negative = _optional_pdf_money(fields.get("negative", ""))
        amount_value = _optional_pdf_money(fields.get("amount", ""))
        row_balance = _optional_pdf_money(fields.get("balance", ""))
        if positive is not None and negative is not None:
            raise ValueError(f"{date}: both money-in and money-out columns are set")
        if positive is not None:
            amount = abs(positive)
        elif negative is not None:
            amount = -abs(negative)
        elif amount_value is not None:
            amount = amount_value
        else:
            continue
        rows.append(
            {
                "date": date,
                "description": description,
                "amount": amount,
                "balance": row_balance,
            }
        )

    if not rows:
        raise ValueError("transaction table was not found")

    running = opening
    for row in rows:
        amount = row["amount"]
        row_balance = row["balance"]
        if row_balance is not None and amount >= 0:
            delta = row_balance - running
            if abs(delta) == abs(amount):
                row["amount"] = delta
                amount = delta
        running += amount
        if row_balance is not None and running != row_balance:
            raise ValueError(
                f"{row['date']}: running balance {row_balance} does not match {running}"
            )

    if running != ending:
        raise ValueError(
            f"opening {opening} plus transactions is {running}, not ending {ending}"
        )

    transactions = []
    for row in rows:
        transaction_id = synth_id(
            path.stem,
            str(row["date"]),
            row["amount"],
            str(row["description"]),
            str(row["balance"] or ""),
        )
        transactions.append(
            FeedTransaction(
                txn_id=transaction_id,
                date=str(row["date"]),
                amount=row["amount"],
                description=str(row["description"]),
                source="statement",
                raw={
                    "pdf_extractor": candidate.mode,
                    "running_balance": (
                        str(row["balance"]) if row["balance"] is not None else ""
                    ),
                },
            )
        )
    ending_date = _pdf_ending_date(candidate.text) or max(
        transaction.date for transaction in transactions
    )
    return ParsedStatement(path, "pdf", transactions, ending, ending_date)


def _detect_pdf_columns(line: str) -> _PdfColumns | None:
    lowered = line.lower()
    date_at = lowered.find("date")
    description_at = lowered.find("description")
    balance_at = lowered.rfind("balance")
    if date_at < 0 or description_at < 0:
        return None

    columns = [("date", date_at), ("description", description_at)]
    aliases = {
        "positive": ("money in", "deposits", "credits", "additions"),
        "negative": ("money out", "withdrawals", "debits", "subtractions"),
        "amount": ("amount",),
    }
    for name, choices in aliases.items():
        positions = [lowered.find(choice) for choice in choices]
        positions = [position for position in positions if position >= 0]
        if positions:
            columns.append((name, min(positions)))
    if balance_at >= 0:
        columns.append(("balance", balance_at))
    columns.sort(key=lambda item: item[1])
    if not any(name in {"positive", "negative", "amount"} for name, _ in columns):
        return None
    return _PdfColumns(tuple(columns))


_PDF_MONEY = re.compile(r"\(?[+-]?\s*\$?\d[\d,]*\.\d{2}\)?")


def _optional_pdf_money(value: str) -> Decimal | None:
    match = _PDF_MONEY.search(value or "")
    if not match:
        return None
    raw = match.group(0).replace("$", "").replace(",", "")
    negative = raw.startswith("-") or (raw.startswith("(") and raw.endswith(")"))
    raw = raw.strip("()+-")
    value = money(raw)
    return -value if negative else value


def _labeled_pdf_amount(text: str, labels: tuple[str, ...]) -> Decimal | None:
    for line in text.splitlines():
        if any(label in line.lower() for label in labels):
            values = _PDF_MONEY.findall(line)
            if values:
                return _optional_pdf_money(values[-1])
    return None


def _pdf_ending_date(text: str) -> str | None:
    ending_label = re.search(
        r"ending\s+balance\s+on\s+"
        r"([A-Za-z]{3,9}\.?\s+\d{1,2},\s+\d{4})",
        text,
        re.IGNORECASE,
    )
    if ending_label:
        value = ending_label.group(1).replace(".", "")
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    period = re.search(
        r"statement\s+period[^\d]*(\d{1,2}/\d{1,2}/\d{2,4})\s*"
        r"(?:-|through|to)\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        text,
        re.IGNORECASE,
    )
    return date_string(period.group(2)) if period else None


def pick_column(headers: list[str], aliases: list[str]) -> str | None:
    normalized = {normalize_header(header): header for header in headers}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def statement_amount(
    row: dict[str, str],
    amount_column: str | None,
    debit_column: str | None,
    credit_column: str | None,
) -> Decimal:
    if amount_column:
        return money(row.get(amount_column))
    debit = money(row.get(debit_column or ""))
    credit = money(row.get(credit_column or ""))
    if debit:
        return -abs(debit)
    return abs(credit)


def ofx_tag(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}>([^<\r\n]+)", block, flags=re.I)
    return match.group(1).strip() if match else ""


def date_string(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in ["%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y"]:
        try:
            return datetime.strptime(value[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def mt940_date(value: str) -> str:
    return datetime.strptime(value, "%y%m%d").strftime("%Y-%m-%d")


def normalize_header(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").split())


def money(value: object) -> Decimal:
    raw = str(value or "0").strip().replace("$", "")
    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return Decimal(raw).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def synth_id(stem: str, date: str, amount: Decimal, description: str, balance: str) -> str:
    normalized_desc = " ".join(description.lower().split())
    digest = hashlib.sha1(
        f"{date}|{amount}|{normalized_desc}|{balance}".encode("utf-8")
    ).hexdigest()
    return digest[:20]
