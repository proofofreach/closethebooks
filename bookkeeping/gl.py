"""Xero general-ledger grouping.

Groups Xero general-ledger rows into balanced journal entries using a
cascade of grouping keys. Validated against a production export of
3,158 balanced groups with one known unbalanced source payment.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

TWO_PLACES = Decimal("0.01")


def parse_amount(value: str | None) -> Decimal:
    if not value:
        return Decimal("0")
    return Decimal(str(value).replace(",", "")).quantize(TWO_PLACES)


def read_gl_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def with_source_index(index: int, row: dict[str, str]) -> dict[str, str]:
    return {"_source_row": str(index), **row}


def group_key_account_pair_amount(row: dict[str, str]) -> tuple:
    accounts = tuple(sorted([row.get("Account", ""), row.get("Related account", "")]))
    amount = parse_amount(row.get("Debit")) + parse_amount(row.get("Credit"))
    return (row.get("Date", ""), row.get("Source", ""), row.get("Reference", ""), accounts, amount)


def group_key_date_source_reference(row: dict[str, str]) -> tuple:
    return (row.get("Date", ""), row.get("Source", ""), row.get("Reference", ""))


def group_key_date_source(row: dict[str, str]) -> tuple:
    return (row.get("Date", ""), row.get("Source", ""))


def build_group(number: int, rows: list[dict[str, str]], strategy: str) -> dict:
    debit = sum(parse_amount(row.get("Debit")) for row in rows)
    credit = sum(parse_amount(row.get("Credit")) for row in rows)
    return {
        "number": f"XERO-JE-{number:05d}",
        "rows": rows,
        "debit": debit,
        "credit": credit,
        "balanced": debit == credit and debit > 0,
        "grouping_strategy": strategy,
    }


def group_ledger_rows(rows: list[dict[str, str]]) -> list[dict]:
    remaining = [with_source_index(index, row) for index, row in enumerate(rows, start=1)]
    balanced_groups: list[dict] = []
    for strategy, key_fn in [
        ("account_pair_amount", group_key_account_pair_amount),
        ("date_source_reference", group_key_date_source_reference),
        ("date_source", group_key_date_source),
    ]:
        grouped = defaultdict(list)
        for row in remaining:
            grouped[key_fn(row)].append(row)
        next_remaining = []
        for items in grouped.values():
            group = build_group(len(balanced_groups) + 1, items, strategy)
            if group["balanced"]:
                balanced_groups.append(group)
            else:
                next_remaining.extend(items)
        remaining = next_remaining

    exception_groups = []
    grouped_exceptions = defaultdict(list)
    for row in remaining:
        grouped_exceptions[group_key_date_source_reference(row)].append(row)
    for items in grouped_exceptions.values():
        exception_groups.append(
            build_group(len(balanced_groups) + len(exception_groups) + 1, items, "exception")
        )
    return balanced_groups + exception_groups


class AccountResolver:
    """Resolve GL account labels ('Name (last4)', 'CODE - Name', bare name) to accounts.csv rows."""

    def __init__(self, accounts: list[dict[str, str]]) -> None:
        self.by_name = {a["Name"]: a for a in accounts}
        self.by_code = {a["Code"]: a for a in accounts if a.get("Code")}

    def resolve(self, label: str) -> dict[str, str] | None:
        if label in self.by_name:
            return self.by_name[label]
        match = re.match(r"^(\d\w*) - (.*)$", label)
        if match:
            if match.group(1) in self.by_code:
                return self.by_code[match.group(1)]
            if match.group(2) in self.by_name:
                return self.by_name[match.group(2)]
        match = re.match(r"^(.*) \((\w+)\)$", label)
        if match and match.group(1) in self.by_name:
            return self.by_name[match.group(1)]
        return None
