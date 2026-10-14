"""Generate the standard per-fiscal-year report set from the ledger.

For each year: income_statement.csv, balance_sheet.csv, trial_balance.csv,
cash_flow_statement.csv (indirect method), and summary.txt — into
reports/FY<year>/. Everything is derived; regenerate anytime.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from beancount import loader
from beancount.core.data import Transaction

from .workspace import Chart, Workspace


def _bucket(account: str, chart: Chart) -> str:
    if chart.is_cash(account):
        return "cash"
    if account.startswith(("Income:", "Expenses:")):
        return "pnl"
    if account.startswith(chart.fixed_asset_prefixes):
        return "investing"
    if account.startswith(chart.long_term_liability_prefixes) or account.startswith(
        "Equity:"
    ):
        return "financing"
    if account.startswith("Assets:"):
        return "op_asset"
    if account.startswith("Liabilities:"):
        return "op_liab"
    raise ValueError(f"Unbucketed account: {account}")


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def generate(
    workspace: Workspace, output_root: Path | None = None, only_year: int | None = None
) -> dict:
    output_root = output_root or workspace.reports
    chart = workspace.chart
    currency = workspace.currency
    unit = currency.lower()
    entries, errors, _ = loader.load_file(str(workspace.ledger_main))
    if errors:
        raise SystemExit(f"Ledger has {len(errors)} errors; fix before reporting.")

    moves: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for entry in entries:
        if isinstance(entry, Transaction):
            for posting in entry.postings:
                if posting.units is not None and posting.units.currency == currency:
                    moves[entry.date.year][posting.account] += posting.units.number

    years = sorted(moves)
    index_rows = []
    for year in years:
        if only_year is not None and year != only_year:
            continue
        out = output_root / f"FY{year}"
        out.mkdir(parents=True, exist_ok=True)
        year_moves = moves[year]

        # Income statement: current-year movement of Income/Expenses accounts.
        _write_csv(
            out / "income_statement.csv",
            ["account", f"amount_{unit}"],
            [
                [a, f"{v:.2f}"]
                for a, v in sorted(year_moves.items())
                if _bucket(a, chart) == "pnl" and v != 0
            ],
        )

        # Balance sheet: cumulative A/L/E balances through year end.
        cumulative: dict[str, Decimal] = defaultdict(Decimal)
        for y in years:
            if y > year:
                break
            for a, v in moves[y].items():
                cumulative[a] += v
        _write_csv(
            out / "balance_sheet.csv",
            ["account", f"balance_{unit}"],
            [
                [a, f"{v:.2f}"]
                for a, v in sorted(cumulative.items())
                if _bucket(a, chart) != "pnl" and v != 0
            ],
        )

        # Trial balance: every account's net movement for the year.
        _write_csv(
            out / "trial_balance.csv",
            ["account", f"net_movement_{unit}"],
            [[a, f"{v:.2f}"] for a, v in sorted(year_moves.items()) if v != 0],
        )

        # Cash flow, indirect method. Signs: beancount balances are
        # debit-normal, so a liability increase (cash source) is negative —
        # flip non-cash movements so sources of cash are positive.
        net_income = -sum(v for a, v in year_moves.items() if _bucket(a, chart) == "pnl")
        rows: list[list] = [["Operating", "Net income", f"{net_income:.2f}"]]
        sections = {"Operating": net_income, "Investing": Decimal(), "Financing": Decimal()}
        labels = {
            "op_asset": ("Operating", "(Increase)/decrease in {a}"),
            "op_liab": ("Operating", "Increase/(decrease) in {a}"),
            "investing": ("Investing", "(Purchases)/disposals of {a}"),
            "financing": ("Financing", "Net movement in {a}"),
        }
        for a in sorted(year_moves):
            bucket = _bucket(a, chart)
            if bucket not in labels or year_moves[a] == 0:
                continue
            section, template = labels[bucket]
            value = -year_moves[a]
            sections[section] += value
            rows.append([section, template.format(a=a), f"{value:.2f}"])
        delta_cash = sum(v for a, v in year_moves.items() if _bucket(a, chart) == "cash")
        computed = sum(sections.values())
        assert computed == delta_cash, (year, computed, delta_cash)
        opening = sum(
            moves[y][a] for y in years if y < year for a in moves[y] if _bucket(a, chart) == "cash"
        )
        rows += [
            ["Total", "Net cash from operating activities", f"{sections['Operating']:.2f}"],
            ["Total", "Net cash from investing activities", f"{sections['Investing']:.2f}"],
            ["Total", "Net cash from financing activities", f"{sections['Financing']:.2f}"],
            ["Total", "Net change in cash", f"{delta_cash:.2f}"],
            ["Total", "Cash at beginning of year", f"{opening:.2f}"],
            ["Total", "Cash at end of year", f"{opening + delta_cash:.2f}"],
        ]
        _write_csv(out / "cash_flow_statement.csv", ["section", "line", f"amount_{unit}"], rows)

        revenue = -sum(v for a, v in year_moves.items() if a.startswith("Income:"))
        expenses = sum(v for a, v in year_moves.items() if a.startswith("Expenses:"))
        assets = sum(v for a, v in cumulative.items() if a.startswith("Assets:"))
        liabilities = -sum(v for a, v in cumulative.items() if a.startswith("Liabilities:"))
        (out / "summary.txt").write_text(
            f"FY{year} ({currency})\n"
            f"Revenue:        {revenue:>14,.2f}\n"
            f"Expenses:       {expenses:>14,.2f}\n"
            f"Net income:     {revenue - expenses:>14,.2f}\n"
            f"Assets (EOY):   {assets:>14,.2f}\n"
            f"Liabilities:    {liabilities:>14,.2f}\n"
            f"Cash (EOY):     {opening + delta_cash:>14,.2f}\n",
            encoding="utf-8",
        )
        index_rows.append((year, revenue, revenue - expenses, assets))

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "README.md").write_text(
        "# Per-fiscal-year reports (derived — regenerate with `books report`)\n\n"
        "Each FY folder: income_statement.csv, balance_sheet.csv,\n"
        "cash_flow_statement.csv (indirect method; ties exactly to the change in\n"
        "cash accounts), trial_balance.csv, summary.txt.\n\n"
        "| FY | Revenue | Net income | Assets EOY |\n|---|---:|---:|---:|\n"
        + "\n".join(
            f"| {y} | {r:,.2f} | {n:,.2f} | {a:,.2f} |" for y, r, n, a in index_rows
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "years": [row[0] for row in index_rows],
        "output": workspace.relative(output_root),
    }
