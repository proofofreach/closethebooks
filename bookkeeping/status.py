"""Coverage/reconciliation status: how far the books run per mapped account.

Answers Xero's bank-rec-screen question — "am I caught up?" — from explicit
account lifecycles, transaction and balance coverage, ledger validation,
transfer clearing, feed revisions, and `!`-flagged entries.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from beancount import loader
from beancount.core.data import Balance, Transaction

from .accounts import AccountSource, coverage_state, load_account_sources
from .evidence import verify_balance_evidence
from .workspace import Chart, Workspace


def account_status(
    workspace: Workspace,
    today: dt.date | None = None,
) -> dict:
    """Load the workspace ledger and compute its close status."""
    entries, errors, _ = loader.load_file(str(workspace.ledger_main))
    sources = load_account_sources(workspace.sources_file, workspace.currency)
    revisions_path = workspace.inbox / "plaid" / "current" / "revisions.json"
    revisions = (
        json.loads(revisions_path.read_text(encoding="utf-8"))
        if revisions_path.exists()
        else []
    )
    return compute_status(
        entries,
        errors,
        sources,
        workspace.chart,
        workspace.currency,
        today=today,
        plaid_revisions=revisions,
    )


def compute_status(
    entries: list,
    errors: list,
    sources: dict[str, AccountSource],
    chart: Chart,
    currency: str,
    today: dt.date | None = None,
    plaid_revisions: list[dict] | None = None,
) -> dict:
    today = today or dt.date.today()
    mapped_accounts = {
        source.account: source for source in sources.values() if source.account
    }
    failed_balances = {
        (error.entry.meta.get("filename"), error.entry.meta.get("lineno"))
        for error in errors
        if isinstance(getattr(error, "entry", None), Balance)
    }
    last_txn: dict[str, dt.date] = {}
    last_assertion: dict[str, dt.date] = {}
    flagged: dict[str, int] = {}
    flagged_total = 0
    tracking_transfer_balance = Decimal()
    ledger_statement_ids: set[str] = set()
    statement_evidence_issues: list[dict] = []
    for entry in entries:
        if isinstance(entry, Transaction):
            if entry.meta and "stmt-id" in entry.meta:
                ledger_statement_ids.add(str(entry.meta["stmt-id"]))
            if entry.flag == "!":
                flagged_total += 1
            for posting in entry.postings:
                if posting.meta and "stmt-id" in posting.meta:
                    ledger_statement_ids.add(str(posting.meta["stmt-id"]))
                if (
                    posting.account == chart.transfer_clearing
                    and posting.units is not None
                    and posting.units.currency == currency
                ):
                    tracking_transfer_balance += posting.units.number
                if posting.account in mapped_accounts:
                    if entry.date > last_txn.get(posting.account, dt.date.min):
                        last_txn[posting.account] = entry.date
                    if entry.flag == "!":
                        flagged[posting.account] = flagged.get(posting.account, 0) + 1
        elif isinstance(entry, Balance) and entry.account in mapped_accounts:
            key = (entry.meta.get("filename"), entry.meta.get("lineno"))
            evidence_valid, evidence_error = verify_balance_evidence(
                entry,
                mapped_accounts[entry.account].statement_evidence_required_after,
            )
            if not evidence_valid:
                statement_evidence_issues.append(
                    {
                        "account": entry.account,
                        "date": entry.date.isoformat(),
                        "error": evidence_error,
                    }
                )
                continue
            if (
                key not in failed_balances
                and entry.date > last_assertion.get(entry.account, dt.date.min)
            ):
                last_assertion[entry.account] = entry.date

    accounts = []
    obligations = []
    for account, spec in sorted(mapped_accounts.items()):
        txn_date = last_txn.get(account)
        assertion_date = last_assertion.get(account)
        days_stale = (today - txn_date).days if txn_date else None
        state = coverage_state(spec, days_stale)
        next_statement_due = None
        if spec.lifecycle == "active" and spec.reconciliation == "statement_balance":
            next_statement_due = (
                assertion_date + dt.timedelta(days=spec.expected_cadence_days)
                if assertion_date
                else dt.date.fromisoformat(spec.cutover_date)
            )
        accounts.append(
            {
                "key": spec.key,
                "name": spec.name,
                "account": account,
                "owner": spec.owner,
                "lifecycle": spec.lifecycle,
                "ingestion": spec.ingestion,
                "feed_account_id": spec.feed_account_id,
                "reconciliation": spec.reconciliation,
                "statement_balance_sign": spec.statement_balance_sign,
                "statement_evidence_required_after": (
                    spec.statement_evidence_required_after
                ),
                "statement_extensions": list(spec.statement_extensions),
                "expected_cadence_days": spec.expected_cadence_days,
                "overdue_after_days": spec.overdue_after_days,
                "last_transaction": txn_date.isoformat() if txn_date else None,
                "last_assertion": (
                    assertion_date.isoformat() if assertion_date else None
                ),
                "next_statement_due": (
                    next_statement_due.isoformat() if next_statement_due else None
                ),
                "days_stale": days_stale,
                "coverage_state": state,
                "flagged": flagged.get(account, 0),
            }
        )
        if spec.lifecycle == "active" and state != "current":
            # A passing balance assertion dated D proves the ledger complete
            # before D, so evidence is only needed from the later date.
            starts = [dt.date.fromisoformat(spec.cutover_date)]
            if txn_date:
                starts.append(txn_date + dt.timedelta(days=1))
            if assertion_date:
                starts.append(assertion_date)
            coverage_start = max(starts).isoformat()
            obligations.append(
                {
                    "account": account,
                    "account_key": spec.key,
                    "kind": state,
                    "coverage_start": coverage_start,
                    "coverage_end": today.isoformat(),
                }
            )
        if next_statement_due and today > next_statement_due:
            obligations.append(
                {
                    "account": account,
                    "account_key": spec.key,
                    "kind": "balance_verification",
                    "coverage_start": (
                        (assertion_date + dt.timedelta(days=1)).isoformat()
                        if assertion_date
                        else spec.cutover_date
                    ),
                    "coverage_end": today.isoformat(),
                }
            )
    if tracking_transfer_balance:
        obligations.append(
            {
                "account": chart.transfer_clearing,
                "account_key": None,
                "kind": "unpaired_transfers",
                "coverage_start": today.isoformat(),
                "coverage_end": today.isoformat(),
            }
        )
    if plaid_revisions:
        for revision in plaid_revisions:
            if f"plaid:{revision['transaction_id']}" not in ledger_statement_ids:
                continue
            obligations.append(
                {
                    "account": "Plaid feed",
                    "account_key": None,
                    "kind": "feed_revision",
                    "transaction_id": revision["transaction_id"],
                    "coverage_start": today.isoformat(),
                    "coverage_end": today.isoformat(),
                }
            )
    if errors:
        obligations.append(
            {
                "account": "Ledger",
                "account_key": None,
                "kind": "ledger_validation",
                "error_count": len(errors),
                "coverage_start": today.isoformat(),
                "coverage_end": today.isoformat(),
            }
        )
    for issue in statement_evidence_issues:
        obligations.append(
            {
                "account": issue["account"],
                "account_key": mapped_accounts[issue["account"]].key,
                "kind": "statement_evidence",
                "error": issue["error"],
                "coverage_start": issue["date"],
                "coverage_end": issue["date"],
            }
        )
    close_state = (
        "blocked"
        if errors or statement_evidence_issues
        else "needs_attention"
        if obligations or flagged_total
        else "current"
    )
    return {
        "as_of": today.isoformat(),
        "close_state": close_state,
        "ledger_errors": len(errors),
        "flagged_total": flagged_total,
        "currency": currency,
        "tracking_transfer_balance": f"{tracking_transfer_balance:.2f} {currency}",
        "statement_evidence_errors": len(statement_evidence_issues),
        "obligations": obligations,
        "accounts": accounts,
    }


def render(status: dict) -> str:
    visible = [row for row in status["accounts"] if row["lifecycle"] == "active"]
    hidden_count = len(status["accounts"]) - len(visible)
    width = max(len(row["account"]) for row in visible) if visible else 8
    lines = [
        f"As of {status['as_of']} — close state: {status['close_state']}, "
        f"ledger errors: {status['ledger_errors']}, "
        f"flagged (!) entries: {status['flagged_total']}, "
        f"close obligations: {len(status['obligations'])}",
        f"{'account':<{width}}  {'state':<16}  {'last txn':<10}  "
        f"{'assertion':<10}  {'stale':>6}  flags",
    ]
    for row in visible:
        stale = f"{row['days_stale']}d" if row["days_stale"] is not None else "-"
        lines.append(
            f"{row['account']:<{width}}  {row['coverage_state']:<16}  "
            f"{row['last_transaction'] or '-':<10}  "
            f"{row['last_assertion'] or '-':<10}  {stale:>6}  {row['flagged'] or ''}"
        )
    if hidden_count:
        account_word = "account" if hidden_count == 1 else "accounts"
        lines.append(
            f"{hidden_count} dormant or closed {account_word} hidden; use --json for all."
        )
    if status["obligations"]:
        names = {row["account"]: row["name"] for row in status["accounts"]}
        lines.append("Obligations:")
        for obligation in status["obligations"]:
            lines.append(
                f"- {names.get(obligation['account'], obligation['account'])}: "
                f"{obligation['kind']} · "
                f"{obligation['coverage_start']} through {obligation['coverage_end']}"
            )
    return "\n".join(lines)
