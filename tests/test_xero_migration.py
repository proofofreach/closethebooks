import csv
import datetime as dt
from pathlib import Path

import pytest

from bookkeeping.accounts import load_account_sources
from bookkeeping.setup import init_workspace
from bookkeeping.xero_migration import XeroMigration

ACCOUNTS = [
    {"AccountID": "a1", "Name": "Operating Checking", "Type": "BANK", "Class": "ASSET", "BankAccountType": "BANK", "Code": ""},
    {"AccountID": "a2", "Name": "Old Savings", "Type": "BANK", "Class": "ASSET", "BankAccountType": "BANK", "Code": ""},
    {"AccountID": "a3", "Name": "Sales", "Type": "REVENUE", "Class": "REVENUE", "BankAccountType": "", "Code": "200"},
    {"AccountID": "a4", "Name": "Software", "Type": "EXPENSE", "Class": "EXPENSE", "BankAccountType": "", "Code": "400"},
]
GL = [
    ("2024-01-05", "Receive Money", "Client payment", "100.00", "0", "Sales", "Operating Checking"),
    ("2024-01-05", "Receive Money", "Client payment", "0", "100.00", "Operating Checking", "Sales"),
    ("2024-06-10", "Spend Money", "Hosting", "0", "20.00", "Software", "Operating Checking"),
    ("2024-06-10", "Spend Money", "Hosting", "20.00", "0", "Operating Checking", "Software"),
    ("2023-02-01", "Receive Money", "Interest", "5.00", "0", "Sales", "Old Savings"),
    ("2023-02-01", "Receive Money", "Interest", "0", "5.00", "Old Savings", "Sales"),
]


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def migration(tmp_path):
    workspace = init_workspace(tmp_path / "ws", name="Migrated Co", start_date=dt.date(2023, 1, 1), git=False)
    backup = tmp_path / "ws" / "imports" / "xero" / "migrated-co"
    write_csv(backup / "csv" / "accounts.csv", ACCOUNTS)
    return XeroMigration(workspace), backup


def add_gl(backup: Path) -> None:
    keys = ("Date", "Source", "Description", "Debit", "Credit", "Related account", "Account")
    write_csv(
        backup / "csv" / "general_ledger_merged.csv",
        [{**dict(zip(keys, row)), "Reference": "", "Running Balance": "0"} for row in GL],
    )


def test_client_id_is_validated_and_stored_privately(migration):
    xero, _backup = migration
    with pytest.raises(ValueError, match="Client ID"):
        xero.save_client("not a client id!")
    xero.save_client("ABC123")
    assert xero.client() == {"client_id": "ABC123"}
    assert oct(xero.client_file.stat().st_mode & 0o777) == "0o600"
    url = xero.authorization_url("http://localhost:5170/migrate/callback")
    assert "code_challenge=" in url and "client_id=ABC123" in url


def test_authorization_rejects_unknown_state(migration):
    xero, _backup = migration
    xero.save_client("ABC123")
    xero.authorization_url("http://localhost:5170/migrate/callback")
    with pytest.raises(ValueError, match="expired or did not match"):
        xero.complete_authorization("code", "forged-state")


def test_conversion_needs_the_general_ledger(migration):
    xero, backup = migration
    assert xero.backup_root() == backup
    assert "General Ledger" in xero.conversion_blocker()


def test_convert_verify_and_register(migration):
    xero, backup = migration
    add_gl(backup)
    assert xero.conversion_blocker() is None
    result = xero.convert()
    assert result["report"]["status"] == "verified"
    accounts = (xero.workspace.ledger_root / "accounts.beancount").read_text()
    assert "open Liabilities:Current:Transfers-In-Transit" in accounts
    registered = {item["key"]: item for item in xero.register_sources()}
    assert registered["operating-checking"]["lifecycle"] == "active"
    assert registered["old-savings"]["lifecycle"] == "dormant"
    source = load_account_sources(xero.workspace.sources_file)["operating-checking"]
    assert source.cutover_date == "2024-06-11"
    assert source.statement_evidence_required_after == "2024-06-12"
    assert "already registered" in xero.conversion_blocker()


def test_conversion_refuses_a_ledger_with_its_own_transactions(migration):
    xero, backup = migration
    add_gl(backup)
    year = xero.workspace.ledger_root / "years" / "2023.beancount"
    year.write_text(
        year.read_text()
        + '2023-03-01 * "Manual"\n  Expenses:Software  1.00 USD\n  Equity:Opening-Balances  -1.00 USD\n'
    )
    assert "did not come from Xero" in xero.conversion_blocker()
    with pytest.raises(ValueError):
        xero.convert()
