"""Durable evidence and deterministic balance assertions from bank statements."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from beancount.core.data import Balance

from .accounts import AccountSource
from .statements import ParsedStatement


@dataclass(frozen=True)
class StatementArtifact:
    evidence_path: Path
    source_evidence_path: Path
    balance_assertion: str | None
    balance_date: str | None
    balance_amount: Decimal | None


def _amount(value: Decimal, currency: str) -> str:
    return f"{value:.2f} {currency}"


def create_statement_artifact(
    statement: ParsedStatement,
    source: AccountSource,
    evidence_dir: Path,
) -> StatementArtifact:
    if source.statement_balance_sign not in {"ledger", "invert"}:
        raise ValueError(
            f"Unsupported statement balance sign: {source.statement_balance_sign!r}."
        )
    content_hash = hashlib.sha256(statement.path.read_bytes()).hexdigest()
    dates = sorted(transaction.date for transaction in statement.transactions)
    transaction_ids = "\n".join(
        sorted(transaction.txn_id for transaction in statement.transactions)
    ).encode("utf-8")
    assertion_date = (
        date.fromisoformat(statement.ending_balance_date) + timedelta(days=1)
        if statement.ending_balance_date
        else None
    )
    balance = statement.ending_balance
    if balance is not None and source.statement_balance_sign == "invert":
        balance = -balance

    evidence = {
        "schema_version": 1,
        "sha256": content_hash,
        "source_filename": statement.path.name,
        "source_evidence": f"{content_hash}{statement.path.suffix.lower() or '.bin'}",
        "format": statement.format,
        "account_key": source.key,
        "ledger_account": source.account,
        "transaction_count": len(statement.transactions),
        "transaction_ids_sha256": hashlib.sha256(transaction_ids).hexdigest(),
        "transaction_coverage_start": dates[0] if dates else None,
        "transaction_coverage_end": dates[-1] if dates else None,
        "ending_balance": _amount(balance, source.currency) if balance is not None else None,
        "ending_balance_date": statement.ending_balance_date,
        "balance_assertion_date": assertion_date.isoformat() if assertion_date else None,
        "reconciliation": (
            "parser_balance_reconciled"
            if statement.format == "pdf"
            else "ending_balance_reported"
            if balance is not None
            else "transactions_only"
        ),
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / f"{content_hash}.json"
    source_evidence_path = evidence_dir / evidence["source_evidence"]
    payload = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if evidence_path.exists() and evidence_path.read_text(encoding="utf-8") != payload:
        raise ValueError(f"Evidence conflict for statement hash {content_hash}.")
    if not evidence_path.exists():
        temporary = evidence_path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(evidence_path)
    if source_evidence_path.exists():
        if hashlib.sha256(source_evidence_path.read_bytes()).hexdigest() != content_hash:
            raise ValueError(f"Source evidence conflict for statement hash {content_hash}.")
    else:
        temporary_source = source_evidence_path.with_suffix(
            source_evidence_path.suffix + ".tmp"
        )
        shutil.copyfile(statement.path, temporary_source)
        temporary_source.replace(source_evidence_path)

    assertion = None
    if balance is not None:
        if assertion_date is None:
            raise ValueError(
                f"{statement.path.name}: ending balance has no assertion date."
            )
        assertion = (
            f"{assertion_date.isoformat()} balance {source.account}  "
            f"{_amount(balance, source.currency)}\n"
            f'  evidence: "../evidence/statements/{content_hash}.json"\n'
        )
    return StatementArtifact(
        evidence_path,
        source_evidence_path,
        assertion,
        assertion_date.isoformat() if assertion_date and balance is not None else None,
        balance,
    )


def select_balance_assertion(
    artifact: StatementArtifact,
    existing_amount: Decimal | None,
) -> str | None:
    if artifact.balance_assertion is None:
        return None
    if existing_amount is None:
        return artifact.balance_assertion
    if existing_amount == artifact.balance_amount:
        return None
    raise ValueError(
        f"Statement balance {artifact.balance_amount} conflicts with existing "
        f"assertion {existing_amount} on {artifact.balance_date}."
    )


def verify_balance_evidence(
    entry: Balance,
    required_after: str | None,
) -> tuple[bool, str | None]:
    if not required_after or entry.date < date.fromisoformat(required_after):
        return True, None
    source_file = Path(entry.meta["filename"]).resolve()
    ledger_root = (
        source_file.parent.parent
        if source_file.parent.name == "years"
        else source_file.parent
    )
    expected_dir = (ledger_root / "evidence" / "statements").resolve()
    reference = entry.meta.get("evidence") if entry.meta else None
    if not reference:
        return False, "balance assertion has no statement evidence link"
    evidence_path = (source_file.parent / str(reference)).resolve()
    if evidence_path.parent != expected_dir:
        return False, "statement evidence path is outside ledger/evidence/statements"
    if not evidence_path.is_file():
        return False, "statement evidence file is missing"
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "statement evidence file is unreadable"
    expected_balance = f"{entry.amount.number:.2f} {entry.amount.currency}"
    checks = {
        "sha256": evidence_path.stem,
        "ledger_account": entry.account,
        "balance_assertion_date": entry.date.isoformat(),
        "ending_balance": expected_balance,
    }
    for field, expected in checks.items():
        if evidence.get(field) != expected:
            return False, f"statement evidence {field} does not match"
    source_evidence = evidence.get("source_evidence")
    if not source_evidence:
        return False, "statement evidence has no retained source"
    source_evidence_path = (expected_dir / str(source_evidence)).resolve()
    if source_evidence_path.parent != expected_dir or not source_evidence_path.is_file():
        return False, "retained statement source is missing"
    if hashlib.sha256(source_evidence_path.read_bytes()).hexdigest() != evidence["sha256"]:
        return False, "retained statement source hash does not match"
    return True, None
