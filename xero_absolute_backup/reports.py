from __future__ import annotations

from datetime import datetime
from pathlib import Path
import csv
import json

from .exporters import write_csv_rows
from .client import XeroClient, TokenStore, serialize
from .config import Settings



def resolve_tenant_output_dir(output_root: Path) -> Path:
    if not output_root.exists():
        raise FileNotFoundError(f"Output directory not found: {output_root}")
    candidates = [p for p in output_root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(f"No tenant folders found in {output_root}")
    names = ", ".join(sorted(p.name for p in candidates))
    raise RuntimeError(f"Multiple tenant folders found: {names}. Set XERO_TENANT_ID or remove extras.")


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    if "T" in value:
        value = value.split("T", 1)[0]
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def load_accounts_map(accounts_csv: Path) -> dict[str, dict[str, str]]:
    if not accounts_csv.exists():
        return {}
    mapping: dict[str, dict[str, str]] = {}
    with accounts_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            code = (row.get("Code") or "").strip()
            if not code:
                continue
            mapping[code] = {
                "name": (row.get("Name") or "").strip(),
                "type": (row.get("Type") or "").strip(),
            }
    return mapping


def parse_account_from_related(value: str) -> tuple[str, str]:
    raw = (value or "").strip()
    if " - " in raw:
        code, name = raw.split(" - ", 1)
        return code.strip(), name.strip()
    return "", raw


def build_account_lookup(accounts_csv: Path) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    if not accounts_csv.exists():
        return lookup
    with accounts_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            code = (row.get("Code") or "").strip()
            name = (row.get("Name") or "").strip()
            acc_type = (row.get("Type") or "").strip()
            bank_type = (row.get("BankAccountType") or "").strip()
            acc_class = (row.get("Class") or "").strip()
            if name:
                lookup.setdefault(
                    name.lower(),
                    {
                        "code": code,
                        "name": name,
                        "type": acc_type,
                        "bank_account_type": bank_type,
                        "class": acc_class,
                    },
                )
            if code and name:
                key_full = f"{code} - {name}".strip()
                lookup.setdefault(
                    key_full.lower(),
                    {
                        "code": code,
                        "name": name,
                        "type": acc_type,
                        "bank_account_type": bank_type,
                        "class": acc_class,
                    },
                )
    return lookup


PNL_ACCOUNT_TYPES = {
    "REVENUE",
    "SALES",
    "OTHERINCOME",
    "DIRECTCOSTS",
    "COGS",
    "EXPENSE",
    "OVERHEAD",
    "OVERHEADS",
    "DEPRECIATION",
}

BALANCE_SHEET_ACCOUNT_TYPES = {
    "BANK",
    "CURRENT",
    "FIXED",
    "LIABILITY",
    "CURRLIAB",
    "TERMLIAB",
    "EQUITY",
}


def generate_cash_pnl(
    output_root: Path,
    *,
    year: int,
    currency: str,
) -> Path:
    tenant_dir = resolve_tenant_output_dir(output_root)
    csv_dir = tenant_dir / "csv"
    bank_csv = csv_dir / "bank_transactions.csv"
    accounts_csv = csv_dir / "accounts.csv"

    if not bank_csv.exists():
        raise FileNotFoundError(f"Missing bank transactions CSV: {bank_csv}")

    accounts_map = load_accounts_map(accounts_csv)
    account_lookup = build_account_lookup(accounts_csv)
    rows: dict[str, dict[str, str]] = {}
    totals = {"income": 0.0, "expense": 0.0}
    unmapped: dict[str, int] = {}

    with bank_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_currency = (row.get("CurrencyCode") or "").strip()
            if row_currency and row_currency.upper() != currency.upper():
                continue
            date_value = parse_date((row.get("DateString") or row.get("Date") or "").strip())
            if not date_value or date_value.year != year:
                continue

            txn_type = (row.get("Type") or "").upper()
            status = (row.get("Status") or "").upper()
            if status and status not in {"AUTHORISED", "PAID"}:
                continue
            line_items_raw = row.get("LineItems") or ""
            try:
                line_items = json.loads(line_items_raw) if line_items_raw else []
            except json.JSONDecodeError:
                line_items = []

            for item in line_items:
                account_code = str(item.get("AccountCode") or "").strip()
                account_meta = accounts_map.get(account_code or "", {"name": "", "type": ""})
                account_type = (account_meta.get("type") or "").upper()
                if account_type not in PNL_ACCOUNT_TYPES:
                    continue
                if not account_code:
                    account_code = "UNSPECIFIED"
                amount = item.get("LineAmount")
                try:
                    amount_value = float(amount)
                except (TypeError, ValueError):
                    amount_value = 0.0

                if txn_type.startswith("SPEND"):
                    income = 0.0
                    expense = abs(amount_value)
                elif txn_type.startswith("RECEIVE"):
                    income = abs(amount_value)
                    expense = 0.0
                else:
                    continue

                totals["income"] += income
                totals["expense"] += expense

                if account_code not in rows:
                    meta = accounts_map.get(account_code, {"name": "", "type": ""})
                    rows[account_code] = {
                        "account_code": account_code,
                        "account_name": meta["name"],
                        "account_type": meta["type"],
                        "income": "0",
                        "expense": "0",
                        "net": "0",
                    }

                entry = rows[account_code]
                income_val = float(entry["income"]) + income
                expense_val = float(entry["expense"]) + expense
                entry["income"] = f"{income_val:.2f}"
                entry["expense"] = f"{expense_val:.2f}"
                entry["net"] = f"{(income_val - expense_val):.2f}"

    output_rows = [rows[key] for key in sorted(rows.keys())]
    output_path = write_csv_rows(
        tenant_dir,
        f"pnl_{year}_cash_{currency.lower()}_account_level.csv",
        output_rows,
    )

    summary_path = tenant_dir / "manifests" / f"pnl_{year}_cash_{currency.lower()}_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "year": year,
                "currency": currency.upper(),
                "basis": "cash",
                "income_total": round(totals["income"], 2),
                "expense_total": round(totals["expense"], 2),
                "net": round(totals["income"] - totals["expense"], 2),
                "accounts": len(output_rows),
                "unmapped_accounts": unmapped,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return output_path


def generate_gl_pnl(
    output_root: Path,
    *,
    year: int,
    currency: str,
) -> Path:
    tenant_dir = resolve_tenant_output_dir(output_root)
    csv_dir = tenant_dir / "csv"
    gl_csv = csv_dir / "general_ledger_merged.csv"
    accounts_csv = csv_dir / "accounts.csv"

    if not gl_csv.exists():
        raise FileNotFoundError(f"Missing merged GL CSV: {gl_csv}")

    accounts_map = load_accounts_map(accounts_csv)
    account_lookup = build_account_lookup(accounts_csv)
    rows: dict[str, dict[str, str]] = {}
    totals = {"income": 0.0, "expense": 0.0}
    unmapped: dict[str, int] = {}
    income_types = {"REVENUE", "OTHERINCOME", "SALES"}

    cutoff = datetime(year, 12, 31)
    with gl_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            date_value = parse_date((row.get("Date") or "").strip())
            if not date_value or date_value > cutoff:
                continue

            account_label = row.get("Account") or ""
            meta = account_lookup.get(account_label.strip().lower())
            if meta is None:
                account_code, account_name = parse_account_from_related(account_label)
                meta = accounts_map.get(account_code, {"name": account_name, "type": ""})
            if not meta.get("name"):
                unmapped[account_label] = unmapped.get(account_label, 0) + 1
            account_name = meta.get("name") or account_label
            account_code = meta.get("code") or ""
            account_type = (meta.get("type") or "").upper()
            if account_type not in PNL_ACCOUNT_TYPES:
                continue

            debit = row.get("Debit") or "0"
            credit = row.get("Credit") or "0"
            try:
                debit_val = float(str(debit).replace(",", ""))
            except ValueError:
                debit_val = 0.0
            try:
                credit_val = float(str(credit).replace(",", ""))
            except ValueError:
                credit_val = 0.0

            is_income = account_type in income_types
            # Xero P&L reports present income as credits and expenses as debits.
            if is_income:
                income = max(credit_val, 0.0)
                expense = 0.0
            else:
                income = 0.0
                expense = max(debit_val, 0.0)

            totals["income"] += income
            totals["expense"] += expense

            key = account_code or account_name
            if key not in rows:
                rows[key] = {
                    "account_code": account_code,
                    "account_name": account_name,
                    "account_type": meta.get("type") or "",
                    "income": "0",
                    "expense": "0",
                    "net": "0",
                }
            entry = rows[key]
            income_val = float(entry["income"]) + income
            expense_val = float(entry["expense"]) + expense
            entry["income"] = f"{income_val:.2f}"
            entry["expense"] = f"{expense_val:.2f}"
            entry["net"] = f"{(income_val - expense_val):.2f}"

    output_rows = [rows[key] for key in sorted(rows.keys())]
    output_path = write_csv_rows(
        tenant_dir,
        f"pnl_{year}_gl_{currency.lower()}_account_level.csv",
        output_rows,
    )
    summary_path = tenant_dir / "manifests" / f"pnl_{year}_gl_{currency.lower()}_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "year": year,
                "currency": currency.upper(),
                "basis": "gl",
                "income_total": round(totals["income"], 2),
                "expense_total": round(totals["expense"], 2),
                "net": round(totals["income"] - totals["expense"], 2),
                "accounts": len(output_rows),
                "unmapped_accounts": unmapped,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return output_path


def generate_gl_balance_sheet(
    output_root: Path,
    *,
    year: int,
    currency: str,
) -> Path:
    tenant_dir = resolve_tenant_output_dir(output_root)
    csv_dir = tenant_dir / "csv"
    gl_csv = csv_dir / "general_ledger_merged.csv"
    accounts_csv = csv_dir / "accounts.csv"

    if not gl_csv.exists():
        raise FileNotFoundError(f"Missing merged GL CSV: {gl_csv}")

    accounts_map = load_accounts_map(accounts_csv)
    account_lookup = build_account_lookup(accounts_csv)
    rows: dict[str, dict[str, str]] = {}
    totals = {"balance": 0.0}
    unmapped: dict[str, int] = {}

    cutoff = datetime(year, 12, 31)
    with gl_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            date_value = parse_date((row.get("Date") or "").strip())
            if not date_value or date_value > cutoff:
                continue

            account_label = row.get("Account") or ""
            meta = account_lookup.get(account_label.strip().lower())
            if meta is None:
                account_code, account_name = parse_account_from_related(account_label)
                meta = accounts_map.get(account_code, {"name": account_name, "type": ""})
            if not meta.get("name"):
                unmapped[account_label] = unmapped.get(account_label, 0) + 1
            account_name = meta.get("name") or account_label
            account_code = meta.get("code") or ""
            account_type = (meta.get("type") or "").upper()
            bank_account_type = (meta.get("bank_account_type") or "").upper()
            if account_type not in BALANCE_SHEET_ACCOUNT_TYPES:
                continue

            key = account_code or account_name
            if key not in rows:
                rows[key] = {
                    "account_code": account_code,
                    "account_name": account_name,
                    "account_type": meta.get("type") or "",
                    "balance": "0",
                }

            debit = row.get("Debit") or "0"
            credit = row.get("Credit") or "0"
            try:
                debit_val = float(str(debit).replace(",", ""))
            except ValueError:
                debit_val = 0.0
            try:
                credit_val = float(str(credit).replace(",", ""))
            except ValueError:
                credit_val = 0.0

            if account_type == "BANK" and bank_account_type == "CREDITCARD":
                balance = credit_val - debit_val
            elif account_type in {"BANK", "CURRENT", "FIXED"}:
                balance = debit_val - credit_val
            else:
                balance = credit_val - debit_val

            entry = rows[key]
            balance_val = float(entry["balance"]) + balance
            entry["balance"] = f"{balance_val:.2f}"

    output_rows = [rows[key] for key in sorted(rows.keys())]
    totals["balance"] = sum(float(r["balance"]) for r in output_rows)
    output_path = write_csv_rows(
        tenant_dir,
        f"balancesheet_{year}_gl_{currency.lower()}_account_level.csv",
        output_rows,
    )
    summary_path = tenant_dir / "manifests" / f"balancesheet_{year}_gl_{currency.lower()}_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "year": year,
                "currency": currency.upper(),
                "basis": "gl",
                "balance_total": round(totals["balance"], 2),
                "accounts": len(output_rows),
                "unmapped_accounts": unmapped,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return output_path


def extract_gl_lines_for_account(
    output_root: Path,
    *,
    year: int,
    account_name: str,
) -> list[dict[str, str]]:
    tenant_dir = resolve_tenant_output_dir(output_root)
    gl_csv = tenant_dir / "csv" / "general_ledger_merged.csv"
    if not gl_csv.exists():
        raise FileNotFoundError(f"Missing merged GL CSV: {gl_csv}")
    results = []
    with gl_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            date_value = parse_date((row.get("Date") or "").strip())
            if not date_value or date_value.year != year:
                continue
            if (row.get("Account") or "").strip() != account_name:
                continue
            results.append(row)
    return results


def fetch_xero_profit_and_loss(
    settings: Settings,
    *,
    year: int,
    currency: str,
    cash: bool = True,
) -> dict:
    client = XeroClient(settings, TokenStore(settings.token_file))
    connections = client.list_connections()
    active = [row for row in connections if row.get("tenantType") == "ORGANISATION"]
    if not active:
        raise RuntimeError("No active Xero organisation connections were found.")
    tenant_id = settings.tenant_id or active[0]["tenantId"]

    from_date = f"{year}-01-01"
    to_date = f"{year}-12-31"
    report = client.accounting_api.get_report_profit_and_loss(
        tenant_id,
        from_date=from_date,
        to_date=to_date,
        standard_layout=True,
        payments_only=cash,
    )
    report_dict = serialize(report)
    report_dict["metadata"] = {
        "year": year,
        "currency": currency.upper(),
        "basis": "cash" if cash else "accrual",
    }
    return report_dict


def fetch_xero_balance_sheet(
    settings: Settings,
    *,
    year: int,
    currency: str,
    cash: bool = True,
) -> dict:
    client = XeroClient(settings, TokenStore(settings.token_file))
    connections = client.list_connections()
    active = [row for row in connections if row.get("tenantType") == "ORGANISATION"]
    if not active:
        raise RuntimeError("No active Xero organisation connections were found.")
    tenant_id = settings.tenant_id or active[0]["tenantId"]

    report = client.accounting_api.get_report_balance_sheet(
        tenant_id,
        date=f"{year}-12-31",
        standard_layout=True,
        payments_only=cash,
    )
    report_dict = serialize(report)
    report_dict["metadata"] = {
        "year": year,
        "currency": currency.upper(),
        "basis": "cash" if cash else "accrual",
    }
    return report_dict


def flatten_report_rows(report: dict) -> list[dict[str, str]]:
    rows = []
    reports = report.get("reports") or report.get("Reports") or []
    if not reports:
        return rows
    report_body = reports[0]
    for row in report_body.get("rows", []) or report_body.get("Rows", []):
        rows.extend(_flatten_report_row(row))
    return rows


def _flatten_report_row(row: dict) -> list[dict[str, str]]:
    row_type = row.get("row_type") or row.get("RowType") or ""
    title = row.get("title") or row.get("Title") or ""
    cells = row.get("cells") or row.get("Cells") or []
    cell_values = [str(c.get("value") or c.get("Value") or "") for c in cells]
    record = {
        "row_type": row_type,
        "title": title,
        "cells": " | ".join(cell_values),
    }
    flattened = [record]
    for sub in row.get("rows", []) or row.get("Rows", []):
        flattened.extend(_flatten_report_row(sub))
    return flattened


def extract_account_totals_from_report(report: dict) -> dict[str, float]:
    totals: dict[str, float] = {}
    reports = report.get("Reports") or report.get("reports") or []
    if not reports:
        return totals
    rows = reports[0].get("Rows") or reports[0].get("rows") or []
    _walk_account_rows(rows, totals)
    return totals


def _walk_account_rows(rows: list, totals: dict[str, float]) -> None:
    for row in rows:
        row_type = row.get("RowType") or row.get("row_type") or ""
        if row_type == "Row":
            cells = row.get("Cells") or row.get("cells") or []
            if len(cells) >= 2:
                name = str(cells[0].get("Value") or cells[0].get("value") or "").strip()
                amount_raw = str(cells[-1].get("Value") or cells[-1].get("value") or "").strip()
                amount = _extract_last_number(amount_raw)
                if name and amount is not None:
                    totals[name] = amount
        child_rows = row.get("Rows") or row.get("rows") or []
        if child_rows:
            _walk_account_rows(child_rows, totals)


SUMMARY_ROW_LABELS = {
    "gross profit",
    "operating income / (loss)",
    "net income",
    "net income  / (loss) before tax",
    "total comprehensive income",
}


def extract_totals_from_report(report: dict) -> dict[str, float]:
    totals = {"income_total": None, "expense_total": None, "net": None}
    reports = report.get("Reports") or report.get("reports") or []
    if not reports:
        return totals
    rows = reports[0].get("Rows") or reports[0].get("rows") or []
    _walk_report_rows(rows, totals)
    return totals


def _walk_report_rows(rows: list, totals: dict[str, float | None]) -> None:
    for row in rows:
        cells = row.get("Cells") or row.get("cells") or []
        if len(cells) >= 2:
            label = str(cells[0].get("Value") or cells[0].get("value") or "").strip().lower()
            amount_raw = str(cells[-1].get("Value") or cells[-1].get("value") or "").strip()
            amount = _extract_last_number(amount_raw)
            if label:
                if "total revenue" in label or "total income" in label:
                    totals["income_total"] = amount
                elif "total expenses" in label or "total expense" in label or "total other income and expense" in label:
                    totals["expense_total"] = amount
                elif "net profit" in label or "net income" in label:
                    totals["net"] = amount
        child_rows = row.get("Rows") or row.get("rows") or []
        if child_rows:
            _walk_report_rows(child_rows, totals)


def _extract_last_number(value: str) -> float | None:
    parts = [p.strip() for p in value.split("|") if p.strip()]
    if not parts:
        return None
    raw = parts[-1].replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None
