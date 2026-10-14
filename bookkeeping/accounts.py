"""Account source registry and coverage policy.

The registry is the explicit contract for which financial sources are active
and how often evidence is expected. Transaction age never changes lifecycle.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

LIFECYCLES = {"active", "dormant", "closed"}
INGESTION_PROVIDERS = {"statement", "plaid", "teller"}
STATEMENT_BALANCE_SIGNS = {"ledger", "invert"}


@dataclass(frozen=True)
class AccountSource:
    key: str
    name: str
    account: str
    cutover_date: str
    invert: bool = False
    statement_preset: str | None = None
    lifecycle: str = "active"
    expected_cadence_days: int = 35
    overdue_after_days: int = 90
    ingestion: str = "statement"
    reconciliation: str = "statement_balance"
    feed_account_id: str | None = None
    owner: str = "owner-operator"
    statement_extensions: tuple[str, ...] = ()
    statement_balance_sign: str = "ledger"
    statement_evidence_required_after: str | None = None
    currency: str = "USD"


def load_account_sources(
    path: Path, default_currency: str = "USD"
) -> dict[str, AccountSource]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing — add an account with `books add-account` or the "
            "Accounts page."
        )
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    sources = {}
    ledger_accounts: dict[str, str] = {}
    feed_accounts: dict[str, str] = {}
    for key, entry in payload.get("accounts", {}).items():
        account = entry["account"]
        if account in ledger_accounts:
            raise ValueError(
                f"{path}: duplicate ledger account {account!r} in {ledger_accounts[account]!r} "
                f"and {key!r}."
            )
        ledger_accounts[account] = key
        lifecycle = entry.get("lifecycle", "active")
        if lifecycle not in LIFECYCLES:
            raise ValueError(
                f"{path}: account {key!r} has invalid lifecycle {lifecycle!r}."
            )
        cadence = int(entry.get("expected_cadence_days", 35))
        overdue = int(entry.get("overdue_after_days", 90))
        if cadence < 1 or overdue < cadence:
            raise ValueError(
                f"{path}: account {key!r} must have a positive cadence and an "
                "overdue threshold no shorter than its cadence."
            )
        ingestion = entry.get("ingestion", "statement")
        if ingestion not in INGESTION_PROVIDERS:
            raise ValueError(
                f"{path}: account {key!r} has invalid ingestion provider "
                f"{ingestion!r}."
            )
        feed_account_id = entry.get("feed_account_id")
        if feed_account_id and feed_account_id in feed_accounts:
            raise ValueError(
                f"{path}: duplicate feed account id {feed_account_id!r} in "
                f"{feed_accounts[feed_account_id]!r} and {key!r}."
            )
        if feed_account_id:
            feed_accounts[feed_account_id] = key
        statement_balance_sign = entry.get("statement_balance_sign", "ledger")
        if statement_balance_sign not in STATEMENT_BALANCE_SIGNS:
            raise ValueError(
                f"{path}: account {key!r} has invalid statement_balance_sign "
                f"{statement_balance_sign!r}."
            )
        evidence_required_after = entry.get("statement_evidence_required_after")
        if evidence_required_after:
            try:
                date.fromisoformat(evidence_required_after)
            except ValueError as exc:
                raise ValueError(
                    f"{path}: account {key!r} has invalid "
                    "statement_evidence_required_after."
                ) from exc
        sources[key] = AccountSource(
            key=key,
            name=entry.get("name", key),
            account=account,
            cutover_date=entry["cutover_date"],
            invert=bool(entry.get("invert", False)),
            statement_preset=entry.get("statement_preset"),
            lifecycle=lifecycle,
            expected_cadence_days=cadence,
            overdue_after_days=overdue,
            ingestion=ingestion,
            reconciliation=entry.get("reconciliation", "statement_balance"),
            feed_account_id=feed_account_id,
            owner=entry.get("owner", "owner-operator"),
            statement_extensions=tuple(entry.get("statement_extensions", ())),
            statement_balance_sign=statement_balance_sign,
            statement_evidence_required_after=evidence_required_after,
            currency=str(entry.get("currency") or default_currency),
        )
    return sources


def coverage_state(source: AccountSource, days_stale: int | None) -> str:
    if source.lifecycle != "active":
        return source.lifecycle
    if days_stale is None:
        return "missing_activity"
    if days_stale <= source.expected_cadence_days:
        return "current"
    if days_stale <= source.overdue_after_days:
        return "update_due"
    return "overdue"


def configured_feed_providers(sources: dict[str, AccountSource]) -> set[str]:
    providers = set()
    for source in sources.values():
        if source.lifecycle != "active" or source.ingestion == "statement":
            continue
        if not source.feed_account_id:
            raise ValueError(
                f"Active {source.ingestion} source {source.key!r} has no "
                "feed_account_id."
            )
        providers.add(source.ingestion)
    return providers
