"""Customer invoices: a self-contained HTML document plus its ledger entry.

Generating an invoice writes only the document. The receivable entry is
returned for review and posted through the normal reviewed workflow.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from string import Template

from .workspace import Workspace

INVOICE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.invoice-(.+)\.html$")


@dataclass(frozen=True)
class LineItem:
    description: str
    amount: Decimal


@dataclass(frozen=True)
class Invoice:
    number: str
    client: str
    client_address: tuple[str, ...]
    items: tuple[LineItem, ...]
    issue_date: dt.date
    due_date: dt.date
    notes: str

    @property
    def total(self) -> Decimal:
        return sum((item.amount for item in self.items), Decimal())


@dataclass(frozen=True)
class InvoiceResult:
    invoice: Invoice
    document: Path
    entry: str


PAGE = Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Invoice $number · $business_name</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light; }
  body { font: 14px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #1d1f21; max-width: 760px; margin: 48px auto; padding: 0 24px; }
  header { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; }
  h1 { font-size: 13px; letter-spacing: .14em; text-transform: uppercase; margin: 0 0 4px; color: #5b6168; }
  .business { font-size: 20px; font-weight: 600; margin: 0; }
  .label { color: #5b6168; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; margin-bottom: 2px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 20px; margin: 36px 0; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th { text-align: left; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: #5b6168;
       border-bottom: 1px solid #1d1f21; padding: 8px 0; }
  td { border-bottom: 1px solid #e3e5e8; padding: 10px 0; vertical-align: top; }
  .num { text-align: right; white-space: nowrap; padding-left: 16px; }
  tfoot td { border: 0; font-weight: 600; font-size: 16px; padding-top: 16px; }
  .notes { margin-top: 40px; color: #3c4146; white-space: pre-line; }
  @media print { body { margin: 0 auto; } }
</style></head><body>
<header>
  <div><h1>Invoice</h1><p class="business">$business_name</p>$business_lines</div>
  <div style="text-align:right"><div class="label">Invoice number</div><strong>$number</strong></div>
</header>
<section class="grid">
  <div><div class="label">Bill to</div>$client</div>
  <div><div class="label">Issued</div>$date</div>
  <div><div class="label">Due</div>$due</div>
  <div><div class="label">Amount due</div><strong>$total $currency</strong></div>
</section>
<table>
  <thead><tr><th>Description</th><th class="num">Amount ($currency)</th></tr></thead>
  <tbody>
  $rows
  </tbody>
  <tfoot><tr><td>Total due</td><td class="num">$total $currency</td></tr></tfoot>
</table>
<div class="notes">$notes</div>
</body></html>
""")


def parse_amount(value: str) -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"Amount {value!r} is not a number.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Each line amount must be greater than zero.")
    try:
        rounded = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("The line amount is too large.") from exc
    if rounded <= 0:
        raise ValueError("Each line amount must be at least 0.01.")
    return rounded


def existing_invoices(workspace: Workspace) -> list[dict]:
    """Invoice documents already filed in the workspace, newest first."""
    folder = workspace.invoice_dir
    if not folder.is_dir():
        return []
    invoices = []
    for path in folder.iterdir():
        match = INVOICE_FILE.match(path.name)
        if match:
            invoices.append(
                {"date": match.group(1), "number": match.group(2), "path": path}
            )
    return sorted(invoices, key=lambda item: (item["date"], item["number"]), reverse=True)


def ledger_invoice_numbers(workspace: Workspace) -> set[str]:
    """Invoice numbers already recorded in the ledger (`invoice:` metadata)."""
    from beancount import loader
    from beancount.core.data import Transaction

    if not workspace.ledger_main.exists():
        return set()
    entries, _errors, _ = loader.load_file(str(workspace.ledger_main))
    return {
        str(entry.meta["invoice"])
        for entry in entries
        if isinstance(entry, Transaction) and entry.meta and entry.meta.get("invoice")
    }


def used_invoice_numbers(workspace: Workspace) -> set[str]:
    return {invoice["number"] for invoice in existing_invoices(workspace)} | ledger_invoice_numbers(
        workspace
    )


def next_invoice_number(
    workspace: Workspace, issue_date: dt.date, used: set[str] | None = None
) -> str:
    stem = f"{workspace.invoicing.prefix}-{issue_date:%Y%m%d}"
    used = used_invoice_numbers(workspace) if used is None else used
    sequence = 1
    while f"{stem}-{sequence}" in used:
        sequence += 1
    return f"{stem}-{sequence}"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_entry(workspace: Workspace, invoice: Invoice, document: Path) -> str:
    currency = workspace.currency
    # Year files live in <ledger>/years/, so documents are one level up.
    try:
        reference = Path("..") / document.relative_to(workspace.ledger_root)
    except ValueError:
        reference = document
    return (
        f"{invoice.issue_date.isoformat()} * {_quote(invoice.client)} "
        f"{_quote('Invoice ' + invoice.number)}\n"
        f"  invoice: {_quote(invoice.number)}\n"
        f"  due: {_quote(invoice.due_date.isoformat())}\n"
        f"  document: {_quote(reference.as_posix())}\n"
        f"  {workspace.chart.receivable}  {invoice.total:.2f} {currency}\n"
        f"  {workspace.chart.sales_income}  {-invoice.total:.2f} {currency}\n"
    )


def create_invoice(
    workspace: Workspace,
    *,
    client: str,
    items: list[tuple[str, str | Decimal]],
    client_address: list[str] | tuple[str, ...] = (),
    issue_date: dt.date | None = None,
    due_days: int | None = None,
    number: str | None = None,
    notes: str | None = None,
) -> InvoiceResult:
    client = client.strip()
    if not client:
        raise ValueError("Enter the client's name.")
    line_items = tuple(
        LineItem(description.strip(), parse_amount(str(amount)))
        for description, amount in items
        if description.strip() or str(amount).strip()
    )
    if not line_items:
        raise ValueError("Add at least one line item.")
    if any(not item.description for item in line_items):
        raise ValueError("Each line item needs a description.")
    issue_date = issue_date or dt.date.today()
    due_days = workspace.invoicing.due_days if due_days is None else due_days
    if not 0 <= due_days <= 3650:
        raise ValueError("Payment terms must be between 0 and 3650 days.")
    try:
        due_date = issue_date + dt.timedelta(days=due_days)
    except OverflowError as exc:
        raise ValueError("Choose an earlier invoice date or shorter payment terms.") from exc
    used = used_invoice_numbers(workspace)
    number = (number or "").strip() or next_invoice_number(workspace, issue_date, used)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", number):
        raise ValueError("Invoice numbers may use letters, digits, dots, dashes, and underscores.")
    if number in used:
        raise ValueError(f"Invoice {number} already exists.")
    if notes is None:
        notes = "\n\n".join(
            part
            for part in (
                workspace.invoicing.payment_instructions,
                workspace.invoicing.notes,
            )
            if part
        )
    invoice = Invoice(
        number=number,
        client=client,
        client_address=tuple(line.strip() for line in client_address if line.strip()),
        items=line_items,
        issue_date=issue_date,
        due_date=due_date,
        notes=notes,
    )

    company = workspace.company
    business_lines = "".join(
        f"<div>{escape(line)}</div>"
        for line in (*company.address, company.email)
        if line
    )
    html = PAGE.substitute(
        number=escape(invoice.number),
        business_name=escape(company.display_name),
        business_lines=business_lines,
        client=escape(invoice.client)
        + "".join(f"<br>{escape(line)}" for line in invoice.client_address),
        date=invoice.issue_date.isoformat(),
        due=invoice.due_date.isoformat(),
        currency=escape(workspace.currency),
        rows="\n  ".join(
            f'<tr><td>{escape(item.description)}</td><td class="num">{item.amount:,.2f}</td></tr>'
            for item in invoice.items
        ),
        total=f"{invoice.total:,.2f}",
        notes=escape(invoice.notes),
    )
    folder = workspace.invoice_dir
    folder.mkdir(parents=True, exist_ok=True)
    document = folder / f"{invoice.issue_date.isoformat()}.invoice-{invoice.number}.html"
    with document.open("x", encoding="utf-8") as handle:
        handle.write(html)
    return InvoiceResult(invoice, document, render_entry(workspace, invoice, document))
