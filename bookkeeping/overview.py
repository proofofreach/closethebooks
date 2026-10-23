"""Book health, next actions, financial snapshot, and coverage for the app.

Built on `status.compute_status` so the CLI and the app always agree on
whether the books are current.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlencode

from beancount.core.data import Transaction

from .accounts import AccountSource
from .dates import long_date
from .status import compute_status
from .workspace import Workspace

STATE_LABELS = {
    "current": ("current", "Current"),
    "update_due": ("due", "Update due"),
    "missing_activity": ("overdue", "No activity yet"),
    "overdue": ("overdue", "Overdue"),
    "dormant": ("muted", "Dormant"),
    "closed": ("muted", "Closed"),
}


@dataclass
class Action:
    kind: str
    title: str
    detail: str = ""
    account_key: str | None = None
    accept: str = ""
    formats: str = ""
    href: str | None = None
    link_label: str = ""
    agent_prompt: str = ""
    account_name: str = ""


@dataclass
class AccountRow:
    key: str
    name: str
    account: str
    lifecycle: str
    ingestion: str
    last_transaction: dt.date | None
    last_verified: dt.date | None
    days_stale: int | None
    state: str
    tone: str
    label: str
    formats: list[str] = field(default_factory=list)
    flagged: int = 0
    months: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class Period:
    key: str
    label: str
    start: dt.date
    end: dt.date


def period_for(key: str, today: dt.date) -> Period:
    if key == "last-year":
        return Period(key, f"{today.year - 1}", dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31))
    if key == "month":
        start = today.replace(day=1)
        return Period(key, today.strftime("%B %Y"), start, today)
    if key == "last-12":
        start = (today.replace(day=1) - dt.timedelta(days=335)).replace(day=1)
        return Period(key, "Last 12 months", start, today)
    return Period("ytd", f"{today.year} to date", dt.date(today.year, 1, 1), today)


PERIOD_CHOICES = (("month", "This month"), ("ytd", "Year to date"), ("last-12", "Last 12 months"), ("last-year", "Last year"))


def month_coverage(
    today: dt.date,
    start: dt.date | None,
    imported_through: dt.date | None,
    verified_through: dt.date | None,
    count: int = 12,
) -> list[tuple[str, str, str]]:
    """(short label, long label, state) for the trailing months.

    States: verified (a statement-backed balance closes the month), imported
    (transactions reach into the month), missing, or before (pre-tracking).
    A balance assertion dated D verifies the balance at the start of D.
    """
    first = today.replace(day=1)
    months = []
    for offset in range(count - 1, -1, -1):
        year, month = divmod(first.year * 12 + first.month - 1 - offset, 12)
        month_start = dt.date(year, month + 1, 1)
        next_month = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        month_end = min(next_month - dt.timedelta(days=1), today)
        if start and month_end < start:
            state = "before"
        elif verified_through and verified_through > month_end:
            state = "verified"
        elif imported_through and imported_through >= month_start:
            state = "imported"
        else:
            state = "missing"
        months.append((month_start.strftime("%b")[0], month_start.strftime("%B %Y"), state))
    return months


def review_query(chart) -> str:
    return (
        "SELECT date, payee, narration, account, position "
        f"WHERE flag = '!' OR account = '{chart.uncategorized_expense}' "
        f"OR account = '{chart.uncategorized_income}' ORDER BY date DESC"
    )


def _formats(source: AccountSource) -> list[str]:
    return sorted(source.statement_extensions) or ["csv", "ofx", "qbo", "qfx"]


def build_overview(
    workspace: Workspace,
    entries: list,
    errors: list,
    sources: dict[str, AccountSource],
    *,
    today: dt.date | None = None,
    period_key: str = "ytd",
    plaid_revisions: list[dict] | None = None,
) -> dict:
    today = today or dt.date.today()
    chart = workspace.chart
    currency = workspace.currency
    status = compute_status(
        entries,
        errors,
        sources,
        chart,
        currency,
        today=today,
        plaid_revisions=plaid_revisions,
    )
    period = period_for(period_key, today)

    cash = Decimal()
    receivable = Decimal()
    revenue = Decimal()
    expenses = Decimal()
    needs_review = 0
    monthly_cash: dict[dt.date, Decimal] = defaultdict(Decimal)
    first_transaction: dict[str, dt.date] = {}
    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        for posting in entry.postings:
            first_transaction.setdefault(posting.account, entry.date)
        in_period = period.start <= entry.date <= period.end
        review = entry.flag == "!"
        for posting in entry.postings:
            if posting.units is None or posting.units.currency != currency:
                continue
            number = posting.units.number
            account = posting.account
            if chart.is_uncategorized(account):
                review = True
            if entry.date > today:
                continue
            if chart.is_cash(account):
                cash += number
                monthly_cash[entry.date.replace(day=1)] += number
            if account == chart.receivable:
                receivable += number
            if in_period:
                if account.startswith("Income:"):
                    revenue -= number
                elif account.startswith("Expenses:"):
                    expenses += number
        if review:
            needs_review += 1

    by_account = {row["account"]: row for row in status["accounts"]}
    active: list[AccountRow] = []
    inactive: list[AccountRow] = []
    for source in sorted(sources.values(), key=lambda item: item.name.lower()):
        row = by_account.get(source.account)
        if row is None:
            continue
        tone, label = STATE_LABELS.get(row["coverage_state"], ("muted", "Unknown"))
        account_row = AccountRow(
            key=source.key,
            name=source.name if source.name != source.account else source.account.rsplit(":", 1)[-1].replace("-", " "),
            account=source.account,
            lifecycle=source.lifecycle,
            ingestion=source.ingestion,
            last_transaction=dt.date.fromisoformat(row["last_transaction"]) if row["last_transaction"] else None,
            last_verified=dt.date.fromisoformat(row["last_assertion"]) if row["last_assertion"] else None,
            days_stale=row["days_stale"],
            state=row["coverage_state"],
            tone=tone,
            label=label,
            formats=_formats(source),
            flagged=row["flagged"],
        )
        account_row.months = month_coverage(
            today,
            first_transaction.get(source.account)
            or (dt.date.fromisoformat(source.cutover_date) if source.cutover_date else None),
            account_row.last_transaction,
            account_row.last_verified,
        )
        (active if source.lifecycle == "active" else inactive).append(account_row)
    rows_by_key = {row.key: row for row in active}

    actions: list[Action] = []
    blocking = [o for o in status["obligations"] if o["kind"] in {"ledger_validation", "statement_evidence"}]
    for obligation in blocking:
        if obligation["kind"] == "ledger_validation":
            actions.append(
                Action(
                    "ledger_validation",
                    f"Fix {obligation['error_count']} ledger error{'s' if obligation['error_count'] != 1 else ''}",
                    "Reports and balances are unreliable until the ledger validates. "
                    "Run `books check` to see each error.",
                    agent_prompt="Run books check and fix the ledger errors.",
                )
            )
        else:
            actions.append(
                Action(
                    "statement_evidence",
                    f"Repair statement evidence for {obligation['account']}",
                    f"The balance on {obligation['coverage_start']} cannot be treated as verified: {obligation['error']}.",
                    agent_prompt="Investigate the statement evidence errors in books status.",
                )
            )

    statement_needed: dict[str, dict] = {}
    for obligation in status["obligations"]:
        key = obligation.get("account_key")
        if obligation["kind"] in {"update_due", "overdue", "missing_activity", "balance_verification"} and key:
            current = statement_needed.get(key)
            if current is None or obligation["coverage_start"] < current["coverage_start"]:
                statement_needed[key] = obligation
    def stale_order(item: tuple[str, dict]) -> tuple[int, str]:
        row = rows_by_key.get(item[0])
        return (-(row.days_stale or 10_000) if row else 0, item[0])

    for key, obligation in sorted(statement_needed.items(), key=stale_order):
        row = rows_by_key.get(key)
        source = sources.get(key)
        if row is None or source is None:
            continue
        start = dt.date.fromisoformat(obligation["coverage_start"])
        formats = row.formats
        if source.ingestion != "statement":
            title = f"Refresh the {row.name} feed"
            detail = (
                f"No transactions since {long_date(row.last_transaction)}. Run `books fetch`, then upload a statement "
                "to verify the balance."
                if row.last_transaction
                else "The feed has not delivered transactions yet. Run `books fetch`."
            )
        else:
            title = f"Upload the {row.name} statement"
            detail = f"Cover {long_date(start)} through {long_date(today)}."
            if obligation["kind"] == "balance_verification":
                detail += " The latest balance is not yet verified against a statement."
        actions.append(
            Action(
                "statement",
                title,
                detail,
                account_key=key,
                account_name=row.name,
                accept=",".join(f".{ext}" for ext in formats),
                formats=", ".join(ext.upper() for ext in formats),
            )
        )

    inbox_files = 0
    if workspace.inbox.is_dir():
        inbox_files = sum(
            1 for path in workspace.inbox.iterdir() if path.is_file() and not path.name.startswith(".")
        )
    if inbox_files:
        actions.append(
            Action(
                "inbox",
                f"Import {inbox_files} file{'s' if inbox_files != 1 else ''} waiting in the inbox",
                "Prepare proposals, then ask your coding agent to do the books.",
                href="/import",
                link_label="Open Import",
                agent_prompt="do the books",
            )
        )
    if needs_review:
        actions.append(
            Action(
                "review",
                f"Review {needs_review} flagged or uncategorized transaction{'s' if needs_review != 1 else ''}",
                "Each needs a category or an owner decision before the period can close.",
                href="/r/query/?" + urlencode({"query_string": review_query(chart)}),
                link_label="Show them",
                agent_prompt="Review the flagged and uncategorized transactions with me.",
            )
        )
    for obligation in status["obligations"]:
        if obligation["kind"] == "unpaired_transfers":
            actions.append(
                Action(
                    "transfers",
                    f"Resolve {status['tracking_transfer_balance']} of unpaired transfers",
                    f"{chart.transfer_clearing} should net to zero once both sides of each transfer are imported.",
                    agent_prompt="Find the unpaired transfers in the transfer clearing account.",
                )
            )
        elif obligation["kind"] == "feed_revision":
            actions.append(
                Action(
                    "feed_revision",
                    "Review a bank revision to a posted transaction",
                    f"Plaid changed or removed transaction {obligation['transaction_id']} after it was posted.",
                    agent_prompt="Review the pending Plaid revisions with books plaid-revisions.",
                )
            )

    has_transactions = any(isinstance(entry, Transaction) for entry in entries)
    if not sources:
        state, title, summary = (
            "incomplete",
            "Set up your accounts",
            "Add the bank and card accounts the business uses, then upload a statement for each.",
        )
        actions.insert(
            0,
            Action(
                "setup",
                "Add your first bank or card account",
                "Each account gets a statement inbox, a coverage status, and balance verification.",
                href="/accounts/new",
                link_label="Add account",
            ),
        )
        if not has_transactions:
            actions.insert(
                1,
                Action(
                    "migrate",
                    "Bring existing accounting history",
                    "Move ledger history from Xero, QuickBooks Online, or Wave. "
                    "Review source coverage and totals before accepting a migration. Do this before registering accounts.",
                    href="/migrations",
                    link_label="Move accounting history",
                ),
            )
    elif not active:
        state, title, summary = (
            "incomplete",
            "No active accounts",
            "Every registered account is dormant or closed. Activate an account to track coverage.",
        )
    elif status["close_state"] == "blocked":
        state, title, summary = (
            "blocked",
            "Blocked",
            "The ledger has errors or broken evidence. Fix them before trusting any figure below.",
        )
    elif actions:
        count = len(actions)
        state, title, summary = (
            "attention",
            "Needs attention",
            f"{count} item{'s' if count != 1 else ''} keep the books from being current.",
        )
    else:
        state, title, summary = (
            "current",
            "Current",
            f"All {len(active)} active account{'s are' if len(active) != 1 else ' is'} current and verified.",
        )

    def oldest(values: list[dt.date | None]) -> dt.date | None:
        if not values or any(value is None for value in values):
            return None
        return min(value for value in values if value is not None)

    chart_points: list[tuple[dt.date, Decimal]] = []
    if monthly_cash:
        running = Decimal()
        month = min(monthly_cash)
        last = today.replace(day=1)
        while month <= last:
            running += monthly_cash.get(month, Decimal())
            chart_points.append((month, running))
            month = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    chart_points = chart_points[-24:]

    return {
        "today": today,
        "currency": currency,
        "state": state,
        "title": title,
        "summary": summary,
        "coverage_through": oldest([row.last_transaction for row in active]),
        "verified_through": oldest([row.last_verified for row in active]),
        "actions": actions,
        "period": period,
        "periods": PERIOD_CHOICES,
        "metrics": {
            "cash": cash,
            "revenue": revenue,
            "expenses": expenses,
            "net_income": revenue - expenses,
            "receivable": receivable,
            "needs_review": needs_review,
        },
        "active": active,
        "inactive": inactive,
        "cash_series": chart_points,
        "ledger_errors": len(errors),
        "has_transactions": has_transactions,
        "status": status,
    }
