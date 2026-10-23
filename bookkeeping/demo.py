"""A fictional demo workspace for trying the product without real data."""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal
from pathlib import Path

from . import tomlwrite
from .setup import SOURCES_HEADER, add_account_source, init_workspace, read_sources

CHECKING = "Assets:Bank:Operating-Checking"
CARD = "Liabilities:Cards:Business-Card"
CLIENTS = ("Harbor & Pine", "Lumen Labs", "Oakline Dental", "Fieldnote Press")
CARD_SPEND = (
    ("Figma", "Expenses:Software", 45),
    ("GitHub", "Expenses:Software", 21),
    ("Google Workspace", "Expenses:Software", 14),
    ("Delta Air Lines", "Expenses:Travel", 380),
    ("Blue Bottle Coffee", "Expenses:Meals", 18),
    ("Staples", "Expenses:Office", 64),
    ("LinkedIn Ads", "Expenses:Advertising", 150),
)


def _q(text: str) -> str:
    return '"' + text.replace('"', '\\"') + '"'


def _month_starts(start: dt.date, end: dt.date):
    month = start.replace(day=1)
    while month <= end:
        yield month
        month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def create_demo(root: Path, today: dt.date | None = None) -> Path:
    today = today or dt.date.today()
    start = dt.date(today.year - 1, 1, 1)
    workspace = init_workspace(
        root,
        name="Northwind Studio",
        currency="USD",
        entity="Single-member LLC",
        email="billing@northwind.example",
        invoice_prefix="NW",
        start_date=start,
        git=False,
    )
    add_account_source(workspace, key="operating-checking", name="Operating checking", account=CHECKING, opened=start)
    add_account_source(workspace, key="business-card", name="Business card", account=CARD, opened=start)
    # Demo balances are illustrative, so they do not require retained statements.
    payload = read_sources(workspace)
    for entry in payload["accounts"].values():
        entry.pop("statement_evidence_required_after", None)
    tomlwrite.write(workspace.sources_file, payload, SOURCES_HEADER)

    rng = random.Random(7)
    entries: dict[int, list[str]] = {}

    movements: list[tuple[dt.date, str, Decimal]] = []

    def add(date: dt.date, text: str) -> None:
        entries.setdefault(date.year, []).append(text)
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] in (CHECKING, CARD):
                movements.append((date, parts[0], Decimal(parts[1])))

    def balance_before(account: str, date: dt.date) -> Decimal:
        return sum((amount for day, name, amount in movements if name == account and day < date), Decimal())

    add(start, f'{start} * "Opening balance"\n  {CHECKING}  18500.00 USD\n  Equity:Opening-Balances  -18500.00 USD\n')
    card_balance = Decimal()
    # The card statement lags: its data stops two months ago so the demo has work to do.
    card_cutoff = (today.replace(day=1) - dt.timedelta(days=40)).replace(day=1)
    checking_verified = today.replace(day=1)
    for month in _month_starts(start, today):
        for index, client in enumerate(rng.sample(CLIENTS, 2)):
            day = month + dt.timedelta(days=4 + index * 9)
            if day > today:
                continue
            amount = Decimal(rng.choice((2400, 3150, 4800, 5200)))
            number = f"NW-{day:%Y%m%d}-1"
            due = day + dt.timedelta(days=14)
            add(day, f'{day} * {_q(client)} "Invoice {number}"\n  invoice: "{number}"\n  due: "{due}"\n'
                f"  Assets:Current:Accounts-Receivable  {amount:.2f} USD\n  Income:Sales  -{amount:.2f} USD\n")
            if due + dt.timedelta(days=3) <= today:
                paid = due + dt.timedelta(days=3)
                add(paid, f'{paid} * {_q(client)} "Payment for {number}"\n  invoice: "{number}"\n'
                    f"  {CHECKING}  {amount:.2f} USD\n  Assets:Current:Accounts-Receivable  -{amount:.2f} USD\n")
        for payee, account, typical, day_offset in (("Studio Lease Co", "Expenses:Rent", 1650, 1), ("Northline Insurance", "Expenses:Insurance", 140, 12)):
            day = month + dt.timedelta(days=day_offset)
            if day <= today:
                add(day, f'{day} * {_q(payee)}\n  {CHECKING}  -{typical:.2f} USD\n  {account}  {typical:.2f} USD\n')
        if month < card_cutoff:
            for _ in range(rng.randint(4, 7)):
                payee, account, typical = rng.choice(CARD_SPEND)
                day = month + dt.timedelta(days=rng.randint(0, 26))
                amount = Decimal(typical) * Decimal(rng.choice(("0.9", "1", "1.1"))).quantize(Decimal("0.01"))
                add(day, f'{day} * {_q(payee.upper())}\n  category-review: "accepted"\n  {CARD}  -{amount:.2f} USD\n  {account}  {amount:.2f} USD\n')
                card_balance -= amount
            pay_day = month + dt.timedelta(days=27)
            add(pay_day, f'{pay_day} * "Card payment"\n  {CHECKING}  {card_balance:.2f} USD\n  {CARD}  {-card_balance:.2f} USD\n')
            card_balance = Decimal()
    for month in _month_starts(start, today):
        next_month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        if next_month <= checking_verified:
            add(next_month, f"{next_month} balance {CHECKING}  {balance_before(CHECKING, next_month):.2f} USD\n")
        if next_month <= card_cutoff:
            add(next_month, f"{next_month} balance {CARD}  {balance_before(CARD, next_month):.2f} USD\n")
    # One uncategorized purchase awaiting review.
    review_day = today - dt.timedelta(days=5)
    add(review_day, f'{review_day} ! "SQ *CORNER PRINT SHOP"\n  {CHECKING}  -86.40 USD\n  Expenses:Uncategorized  86.40 USD\n')

    main_text = workspace.ledger_main.read_text(encoding="utf-8")
    for year, items in sorted(entries.items()):
        path = workspace.ledger_root / "years" / f"{year}.beancount"
        path.write_text(f"; Transactions dated {year}\n\n" + "\n".join(sorted(items, key=lambda t: (t[:10], " balance " in t))), encoding="utf-8")
        include = f'include "years/{year}.beancount"'
        if include not in main_text:
            main_text += include + "\n"
    workspace.ledger_main.write_text(main_text, encoding="utf-8")

    statement = workspace.inbox / "business-card--recent-activity.csv"
    lines = ["Date,Description,Amount"]
    for offset in range(6):
        day = card_cutoff + dt.timedelta(days=3 + offset * 6)
        if day >= today:
            break
        payee, _account, typical = CARD_SPEND[offset % len(CARD_SPEND)]
        lines.append(f"{day.isoformat()},{payee.upper()},-{typical:.2f}")
    statement.write_text("\n".join(lines) + "\n", encoding="utf-8")
    from .csv_mapping import save_mapping
    save_mapping(statement, dict(date='Date', description='Description', amount='Amount',
                                date_format='iso', delimiter=',', number_format='dot',
                                amount_sign='inflow', expected_currency='USD'))
    return workspace.root
