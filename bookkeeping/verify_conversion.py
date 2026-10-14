"""Verify the beancount ledger against the source Xero GL export."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from beancount import loader
from beancount.core import data as bc_data

from .beancount_convert import ADJUSTMENT_ACCOUNT, Converter
from .gl import parse_amount


def verify(backup_root: Path, ledger_root: Path) -> dict[str, object]:
    converter = Converter(backup_root, ledger_root)

    entries, errors, _options = loader.load_file(str(ledger_root / "main.beancount"))
    error_summaries = [f"{e.source.get('filename')}:{e.source.get('lineno')}: {e.message}" for e in errors]

    # Per-account net from the ledger (transactions only).
    ledger_net: dict[str, Decimal] = defaultdict(Decimal)
    for entry in entries:
        if isinstance(entry, bc_data.Transaction):
            for posting in entry.postings:
                ledger_net[posting.account] += posting.units.number

    # Per-account net from the GL CSV.
    gl_net: dict[str, Decimal] = defaultdict(Decimal)
    for row in converter.gl_rows:
        account = converter.account_for_label(row.get("Account", ""))
        gl_net[account] += parse_amount(row.get("Debit")) - parse_amount(row.get("Credit"))

    mismatches = []
    for account in sorted(set(gl_net) | set(ledger_net)):
        if account == ADJUSTMENT_ACCOUNT:
            continue
        gl_value = gl_net.get(account, Decimal("0"))
        ledger_value = ledger_net.get(account, Decimal("0"))
        if gl_value != ledger_value:
            mismatches.append(
                {"account": account, "gl": str(gl_value), "ledger": str(ledger_value)}
            )

    adjustment_total = ledger_net.get(ADJUSTMENT_ACCOUNT, Decimal("0"))
    txn_count = sum(1 for e in entries if isinstance(e, bc_data.Transaction))

    report = {
        "status": "verified" if not errors and not mismatches else "failed",
        "bean_check_errors": error_summaries,
        "transactions": txn_count,
        "accounts_compared": len(set(gl_net)),
        "mismatches": mismatches,
        "adjustment_account_net": str(adjustment_total),
    }
    return report


def write_report(report: dict[str, object], path: Path) -> None:
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
