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


def test_authorization_uses_granular_scopes_and_allows_override(migration):
    xero, _backup = migration
    xero.save_client("ABC123")
    url = xero.authorization_url("http://localhost:5170/migrate/callback")
    assert "accounting.transactions.read" not in url
    assert "accounting.banktransactions.read" in url
    xero._write_private(xero.client_file, {**xero.client(), "scopes": ["openid", "accounting.settings.read"]})
    url = xero.authorization_url("http://localhost:5170/migrate/callback")
    assert "scope=openid+accounting.settings.read&" in url


def test_delta_pages_merge_in_place_with_latest_version_winning(tmp_path):
    import json

    from xero_absolute_backup.backup import BackupRunner
    from xero_absolute_backup.models import ENDPOINTS

    runner = object.__new__(BackupRunner)
    runner.output_root = tmp_path
    runner.records_cache = {}
    runner.delta_id = "20261014T120000"
    raw = tmp_path / "raw" / "contacts"
    raw.mkdir(parents=True)
    (raw / "page-00001.json").write_text(json.dumps([{"ContactID": "c1", "Name": "Old"}, {"ContactID": "c2", "Name": "Same"}]))
    (raw / "delta-20261014T120000-page-00001.json").write_text(json.dumps([{"ContactID": "c1", "Name": "New"}, {"ContactID": "c3", "Name": "Added"}]))
    records = runner._load_existing_records("contacts", refresh=True)
    assert [(r["ContactID"], r["Name"]) for r in records] == [("c1", "New"), ("c2", "Same"), ("c3", "Added")]
    contacts = next(e for e in ENDPOINTS if e.key == "contacts")
    assert runner._cursor_label(contacts, 1, None).startswith("delta-20261014T120000-")
    assert runner._status_key("contacts") == "contacts@20261014T120000"


def test_fetch_changes_needs_a_full_backup_then_resumes_a_paused_run(migration, monkeypatch):
    import json
    import time as _time

    import xero_absolute_backup.backup as backup_module

    xero, backup = migration
    xero.save_client("ABC123")
    with pytest.raises(ValueError, match="full backup"):
        xero.start_backup(changes_only=True)

    calls = []

    class FakeRunner:
        def __init__(self, settings, *, modified_since=None, delta_id=None):
            calls.append((modified_since, delta_id))
            self.connection = {"tenantName": "Migrated Co"}

        def run(self, sleep_on_rate_limit=False):
            status = "paused" if len(calls) == 2 else "complete"
            (backup / "manifests").mkdir(parents=True, exist_ok=True)
            (backup / "manifests" / "run_summary.json").write_text(
                json.dumps({"status": status, "generated_at": "2026-10-01T10:00:00+00:00"})
            )

    monkeypatch.setattr(backup_module, "BackupRunner", FakeRunner)

    def wait():
        while xero.job.state == "running":
            _time.sleep(0.01)

    xero.start_backup(); wait()                      # full backup completes
    assert calls[-1] == (None, None) and xero.job.state == "complete"
    xero.start_backup(changes_only=True); wait()     # delta pauses at the API limit
    since, delta_id = calls[-1]
    assert since == "2026-09-30T10:00:00" and delta_id and xero.job.state == "paused"
    xero.start_backup(); wait()                      # a plain resume continues the same delta
    assert calls[-1] == (since, delta_id) and xero.job.state == "complete"
    assert xero.pending_delta() is None


def test_progress_reports_the_current_phase(migration):
    from xero_absolute_backup.models import ENDPOINTS
    from xero_absolute_backup.state import StateStore

    xero, _backup = migration
    xero.state_db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(xero.state_db)
    store.upsert_sync_status(ENDPOINTS[0].key, completed=True)
    store.upsert_entity("e1", "invoices", True)
    store.commit()
    store.close()
    phase = xero.progress()["phase"]
    assert (phase["step"], phase["done"], phase["total"]) == (1, 1, len(ENDPOINTS))

    store = StateStore(xero.state_db)
    for endpoint in ENDPOINTS:
        store.upsert_sync_status(endpoint.key, completed=True)
    store.commit()
    store.close()
    assert xero.progress()["phase"]["label"] == "Finding attachments"


def test_existing_backup_layout_from_books_toml_is_reused(tmp_path):
    from bookkeeping import tomlwrite
    from bookkeeping.workspace import load_workspace

    workspace = init_workspace(tmp_path / "ws", name="Legacy Co", git=False)
    backup = workspace.root / "backup_output" / "legacy-co"
    write_csv(backup / "csv" / "accounts.csv", ACCOUNTS)
    (workspace.root / "xero_sync_state.db").write_bytes(b"")
    config = workspace.config_path.read_text() + '\n[paths]\nxero_backup = "backup_output/legacy-co"\n'
    workspace.config_path.write_text(config)
    xero = XeroMigration(load_workspace(workspace.root))
    assert xero.output_dir == workspace.root / "backup_output"
    assert xero.state_db == workspace.root / "xero_sync_state.db"
    assert xero.backup_root() == backup


def test_attachment_exports_are_rewritten_after_downloads(tmp_path, monkeypatch):
    from xero_absolute_backup import backup as backup_module
    from xero_absolute_backup.backup import BackupRunner

    order = []
    runner = object.__new__(BackupRunner)
    runner.output_root = tmp_path
    for name in ("phase_one_fetch_data", "phase_two_discover_attachments", "phase_three_download_attachments", "write_attachment_exports"):
        monkeypatch.setattr(runner, name, lambda name=name: order.append(name), raising=False)
    runner.build_manifest = lambda **kwargs: {"status": kwargs["status"]}

    class FakeState:
        def commit(self): pass
        def close(self): pass

    runner.state = FakeState()
    monkeypatch.setattr(backup_module, "write_manifest", lambda root, manifest: None)
    runner.run()
    assert order[-2:] == ["phase_three_download_attachments", "write_attachment_exports"]
