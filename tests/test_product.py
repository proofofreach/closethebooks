import datetime as dt
import hashlib
import re
import io
import subprocess
import sys
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from werkzeug.datastructures import FileStorage

from bookkeeping import tomlwrite
from bookkeeping.accounts import load_account_sources
from bookkeeping.app import create_app, save_statement_upload
from bookkeeping.demo import create_demo
from bookkeeping.invoices import create_invoice, next_invoice_number
from bookkeeping.overview import build_overview, month_coverage
from bookkeeping.reports import generate
from bookkeeping.setup import add_account_source, init_workspace
from bookkeeping.workspace import WorkspaceError, load_workspace, parse_config, resolve_workspace

ROOT = Path(__file__).resolve().parents[1]
TODAY = dt.date(2026, 7, 15)


@pytest.fixture
def workspace(tmp_path):
    return init_workspace(tmp_path / "books", name="Acme Studio", start_date=dt.date(2026, 1, 1), git=False)


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    root = create_demo(tmp_path_factory.mktemp("demo") / "demo", today=TODAY)
    return load_workspace(root)


def load(workspace):
    entries, errors, _ = loader.load_file(str(workspace.ledger_main))
    return entries, errors


# Product neutrality --------------------------------------------------------

# SHA-256 of lowercase terms from the private workspace this product was
# extracted from. Hashed so the public repository never names the company.
FORBIDDEN_TERM_HASHES = {
    "917c5c8714b39c98dfd6a8c2ed164845c43194dd68ef1ab09785d976cc44002f",
    "6219e76f0b280e446f626c97aa603ecf1caabe6ce9dff41cd788fa745d48350f",
    "3756b5fab42243e42c7b73e1948df6716eb2463fabe958667a0174b13d5ee77b",
    "e3d7eb0f19a4aceb177e5284210da706dbfb301d80615c2495bb25848480125b",
    "4a6f4cd57109b554e4313d1cfbc0eddda0f2bd8d81969b95295b3dded2fa55b2",
    "1f12cc5ef7bca4bc333d29bb18741d48e042ed447871f485aedfc6d6dbb91efa",
    "6985863a776a40a3bb0968ab9d214bfbb7476f4b21077d0b96c2761a9ba5ba72",
    "4efc350fea17b6229eda3848795161d8e93a366318c08565d085faaf5cc67130",
}


def test_repository_carries_no_company_specific_names():
    # Everything git would publish: tracked files plus untracked, unignored ones.
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    offenders = []
    for name in listed:
        path = ROOT / name
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".html", ".css", ".js", ".md", ".toml", ".txt", ".json", ".example", ""}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        tokens = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text))
        tokens |= {piece for token in tokens for piece in token.split("-")}
        if any(hashlib.sha256(token.encode()).hexdigest() in FORBIDDEN_TERM_HASHES for token in tokens):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


# Workspace configuration ---------------------------------------------------

def test_config_requires_company_name(tmp_path):
    with pytest.raises(WorkspaceError, match="name is required"):
        parse_config(tmp_path, {"company": {}})


def test_config_rejects_invalid_account_roles(tmp_path):
    with pytest.raises(WorkspaceError, match="chart.receivable"):
        parse_config(tmp_path, {"company": {"name": "X"}, "chart": {"receivable": "Receivables"}})


def test_config_resolves_relative_paths_and_currency(tmp_path):
    workspace = parse_config(
        tmp_path,
        {"company": {"name": "X", "currency": "EUR"}, "paths": {"ledger": "books/main.beancount"}},
    )
    assert workspace.ledger_main == tmp_path / "books/main.beancount"
    assert workspace.currency == "EUR"


def test_workspace_is_found_from_a_subdirectory(workspace, monkeypatch):
    monkeypatch.delenv("BOOKS_WORKSPACE", raising=False)
    nested = workspace.root / "ledger" / "years"
    assert resolve_workspace(start=nested).root == workspace.root


def test_missing_workspace_explains_how_to_create_one(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOKS_WORKSPACE", raising=False)
    with pytest.raises(WorkspaceError, match="books init"):
        resolve_workspace(start=tmp_path)


def test_toml_writer_round_trips_nested_tables(tmp_path):
    payload = {"company": {"name": 'Quote "Co"', "address": ["1 Main", "Town"]}, "accounts": {"a-b": {"x": 1, "y": True}}}
    tomlwrite.write(tmp_path / "x.toml", payload, "# header")
    assert tomllib.loads((tmp_path / "x.toml").read_text()) == payload


# Setup -----------------------------------------------------------------------

def test_init_creates_a_valid_ledger_and_agent_files(workspace):
    _entries, errors = load(workspace)
    assert errors == []
    assert (workspace.root / "AGENTS.md").exists()
    assert (workspace.root / ".claude/skills/do-the-books/SKILL.md").exists()
    assert "inbox/" in (workspace.root / ".gitignore").read_text()
    with pytest.raises(FileExistsError):
        init_workspace(workspace.root, name="Again", git=False)


def test_add_account_opens_ledger_account_and_registers_source(workspace):
    result = add_account_source(
        workspace, key="main-card", name="Main card", account="Liabilities:Cards:Main", opened=dt.date(2026, 2, 1)
    )
    assert result["opened"] is True
    _entries, errors = load(workspace)
    assert errors == []
    source = load_account_sources(workspace.sources_file)["main-card"]
    assert source.statement_balance_sign == "invert"
    assert source.cutover_date == "2026-02-01"
    with pytest.raises(ValueError, match="already exists"):
        add_account_source(workspace, key="main-card", name="Again", account="Liabilities:Cards:Other")
    updated = add_account_source(
        workspace, key="main-card", name="Renamed", account="Liabilities:Cards:Main", lifecycle="dormant", replace=True
    )
    assert updated["opened"] is False
    source = load_account_sources(workspace.sources_file)["main-card"]
    assert (source.name, source.lifecycle, source.cutover_date) == ("Renamed", "dormant", "2026-02-01")


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"key": "Bad Key", "account": "Assets:Bank:A"}, "lowercase"),
        ({"key": "ok", "account": "Expenses:Food"}, "Assets:Bank"),
        ({"key": "ok", "account": "Assets:Bank:A", "statement_extensions": ["pdf"]}, "parser preset"),
        ({"key": "ok", "account": "Assets:Bank:A", "ingestion": "plaid"}, "feed account ID"),
    ],
)
def test_add_account_validates_input(workspace, kwargs, message):
    with pytest.raises(ValueError, match=message):
        add_account_source(workspace, name="X", **kwargs)


# Invoices --------------------------------------------------------------------

def test_invoice_document_entry_and_numbering(workspace):
    first = create_invoice(
        workspace,
        client="<Client & Co>",
        items=[("Design", "1,000.50"), ("Hosting", "20")],
        issue_date=dt.date(2026, 3, 2),
    )
    assert first.invoice.number == "INV-20260302-1"
    assert first.invoice.total == Decimal("1020.50")
    html = first.document.read_text()
    assert "&lt;Client &amp; Co&gt;" in html and "<Client" not in html
    assert "Assets:Current:Accounts-Receivable  1020.50 USD" in first.entry
    assert next_invoice_number(workspace, dt.date(2026, 3, 2)) == "INV-20260302-2"
    with pytest.raises(ValueError, match="greater than zero"):
        create_invoice(workspace, client="X", items=[("A", "-1")])


def test_invoice_numbers_already_in_the_ledger_are_not_reused(workspace):
    year_file = workspace.ledger_root / "years" / "2026.beancount"
    year_file.write_text(
        year_file.read_text()
        + '2026-03-02 * "Old" "Invoice INV-20260302-1"\n  invoice: "INV-20260302-1"\n'
        "  Assets:Current:Accounts-Receivable  5.00 USD\n  Income:Sales  -5.00 USD\n"
    )
    assert next_invoice_number(workspace, dt.date(2026, 3, 2)) == "INV-20260302-2"


# Overview ----------------------------------------------------------------------

def test_overview_without_accounts_asks_for_setup(workspace):
    entries, errors = load(workspace)
    data = build_overview(workspace, entries, errors, {}, today=TODAY)
    assert data["state"] == "incomplete"
    assert data["actions"][0].href == "/accounts/new"


def test_overview_demo_needs_attention_with_specific_actions(demo):
    entries, errors = load(demo)
    data = build_overview(demo, entries, errors, load_account_sources(demo.sources_file), today=TODAY)
    assert errors == []
    assert data["state"] == "attention"
    lead = data["actions"][0]
    assert lead.kind == "statement" and lead.account_key == "business-card"
    assert lead.accept == ".csv,.ofx,.qbo,.qfx"
    kinds = {action.kind for action in data["actions"]}
    assert {"inbox", "review"} <= kinds
    # Coverage is the oldest active account date, never the newest.
    assert data["coverage_through"] == min(row.last_transaction for row in data["active"])
    assert data["metrics"]["needs_review"] == 1


def test_failed_balance_assertion_is_not_reported_as_verified(workspace):
    add_account_source(workspace, key="checking", name="Checking", account="Assets:Bank:Checking", opened=dt.date(2026, 1, 1))
    year_file = workspace.ledger_root / "years" / "2026.beancount"
    year_file.write_text(
        year_file.read_text()
        + '2026-06-01 * "Deposit"\n  Assets:Bank:Checking  10.00 USD\n  Income:Sales  -10.00 USD\n'
        "2026-06-02 balance Assets:Bank:Checking  99.00 USD\n"
    )
    entries, errors = load(workspace)
    data = build_overview(workspace, entries, errors, load_account_sources(workspace.sources_file), today=TODAY)
    assert data["state"] == "blocked"
    assert data["active"][0].last_verified is None


def test_month_coverage_marks_verified_imported_and_missing():
    months = month_coverage(TODAY, dt.date(2026, 3, 1), dt.date(2026, 6, 20), dt.date(2026, 5, 1), count=6)
    assert [state for _short, _long, state in months] == ["before", "verified", "verified", "imported", "imported", "missing"]


def test_reports_work_for_any_chart(demo, tmp_path):
    result = generate(demo, tmp_path / "reports", TODAY.year)
    assert result["years"] == [TODAY.year]
    assert (tmp_path / "reports" / f"FY{TODAY.year}" / "cash_flow_statement.csv").exists()


# Statement uploads -------------------------------------------------------------

def upload(name, data=b"Date,Amount\n"):
    return FileStorage(stream=io.BytesIO(data), filename=name)


def test_upload_uses_safe_account_prefixed_name_and_preserves_different_contents(tmp_path):
    first = save_statement_upload(upload("../June statement.CSV"), tmp_path, "checking")
    second = save_statement_upload(upload("June statement.csv", b"Date,Amount\n2026-06-01,1.00\n"), tmp_path, "checking")
    assert first.name == "checking--June_statement.csv"
    assert second.name == "checking--June_statement-2.csv"


def test_upload_rejects_invalid_files_without_leaving_a_file(tmp_path):
    with pytest.raises(ValueError, match="not accepted"):
        save_statement_upload(upload("notes.exe"), tmp_path, "checking")
    with pytest.raises(ValueError, match="20 MB"):
        save_statement_upload(upload("big.csv", b"x" * 20), tmp_path, "checking", max_bytes=10)
    assert list(tmp_path.iterdir()) == []


# Web app -----------------------------------------------------------------------

@pytest.fixture
def client(demo):
    app = create_app(demo, mount_reports=False)
    app.config["TESTING"] = True
    test_client = app.test_client()
    page = test_client.get("/").get_data(as_text=True)
    token = page.split('name="books-token" content="', 1)[1].split('"', 1)[0]
    return test_client, token


@pytest.mark.parametrize("path", ["/", "/?period=last-year", "/import", "/accounts", "/accounts/new", "/accounts/business-card", "/invoices", "/invoices/new", "/ask", "/settings", "/migrate"])
def test_pages_render(client, path):
    test_client, _token = client
    response = test_client.get(path)
    assert response.status_code == 200
    assert "Northwind Studio" in response.get_data(as_text=True)


def test_overview_orders_health_before_actions_before_figures(client):
    test_client, _token = client
    page = test_client.get("/").get_data(as_text=True)
    assert page.index('id="health-title"') < page.index('id="actions-heading"') < page.index('id="coverage-heading"') < page.index('id="snapshot-heading"')
    assert 'data-dropzone' in page and 'accept=".csv,.ofx,.qbo,.qfx"' in page
    assert 'aria-controls="inactive-accounts"' not in page or 'aria-expanded="false"' in page


def test_posts_require_the_page_token(client):
    test_client, token = client
    assert test_client.post("/settings", data={"name": "X"}).status_code == 403
    assert test_client.post("/settings", data={"name": "X"}, headers={"X-Books-Token": "wrong"}).status_code == 403


def test_requests_from_other_hosts_are_refused(client):
    test_client, _token = client
    assert test_client.get("/", environ_overrides={"REMOTE_ADDR": "10.0.0.5"}).status_code == 403


def test_upload_endpoint_enforces_account_and_formats(client, demo):
    test_client, token = client
    headers = {"X-Books-Token": token}
    bad_account = test_client.post("/upload", data={"account_key": "nope", "statement": (io.BytesIO(b"x"), "a.csv")}, headers=headers)
    assert bad_account.status_code == 400
    wrong_type = test_client.post("/upload", data={"account_key": "business-card", "statement": (io.BytesIO(b"x"), "a.pdf")}, headers=headers)
    assert wrong_type.status_code == 400 and "CSV" in wrong_type.get_json()["error"]
    ok = test_client.post("/upload", data={"account_key": "business-card", "statement": (io.BytesIO(b"Date,Amount\n"), "July.csv")}, headers=headers)
    assert ok.status_code == 200
    saved = demo.inbox / ok.get_json()["filename"]
    assert saved.name.startswith("business-card--") and saved.exists()
    saved.unlink()


def test_prepare_writes_proposals_with_learned_categories(client, demo):
    test_client, token = client
    response = test_client.post("/import/prepare", data={"csrf": token}, follow_redirects=True)
    page = response.get_data(as_text=True)
    assert "Prepared" in page
    assert "Expenses:Software" in page
    assert demo.proposals.exists()


def test_account_form_adds_an_account(client, demo):
    test_client, token = client
    response = test_client.post(
        "/accounts/new",
        data={"csrf": token, "kind": "card", "name": "Travel card", "formats": ["csv"], "cadence": "30", "overdue": "60", "lifecycle": "active"},
    )
    assert response.status_code == 302
    assert "Liabilities:Cards:Travel-Card" in demo.sources_file.read_text()
    invalid = test_client.post("/accounts/new", data={"csrf": token, "name": "X", "account": "Expenses:X", "cadence": "30", "overdue": "60"})
    assert "must look like" in invalid.get_data(as_text=True)


def test_cli_end_to_end(tmp_path):
    root = tmp_path / "cli"
    def books(*args):
        return subprocess.run([sys.executable, "-m", "bookkeeping", *args], capture_output=True, text=True, cwd=ROOT)
    assert books("init", str(root), "--name", "CLI Co", "--currency", "CAD", "--no-git").returncode == 0
    assert books("-w", str(root), "add-account", "chequing", "--account", "Assets:Bank:Chequing").returncode == 0
    check = books("-w", str(root), "check")
    assert check.returncode == 0, check.stderr
    status = books("-w", str(root), "status")
    assert "Assets:Bank:Chequing" in status.stdout
    query = books("-w", str(root), "query", "SELECT count(*)")
    assert query.returncode == 0, query.stderr
    missing = books("-w", str(tmp_path / "nowhere"), "status")
    assert missing.returncode == 2 and "books init" in missing.stderr


def test_statement_request_starts_after_the_last_verified_balance(workspace):
    from bookkeeping.status import account_status

    from bookkeeping.setup import SOURCES_HEADER, read_sources

    add_account_source(workspace, key="checking", name="Checking", account="Assets:Bank:Checking", opened=dt.date(2026, 1, 1))
    payload = read_sources(workspace)
    payload["accounts"]["checking"].pop("statement_evidence_required_after")
    tomlwrite.write(workspace.sources_file, payload, SOURCES_HEADER)
    year_file = workspace.ledger_root / "years" / "2026.beancount"
    year_file.write_text(
        year_file.read_text()
        + '2026-01-10 * "Deposit"\n  Assets:Bank:Checking  10.00 USD\n  Income:Sales  -10.00 USD\n'
        "2026-04-01 balance Assets:Bank:Checking  10.00 USD\n"
    )
    status = account_status(workspace, today=TODAY)
    overdue = next(o for o in status["obligations"] if o["kind"] == "overdue")
    assert overdue["coverage_start"] == "2026-04-01"


def test_no_platform_specific_date_directives():
    """%-d works only on Unix and %#d only on Windows; use bookkeeping.dates."""
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in (ROOT / "bookkeeping").rglob("*")
        if path.suffix in {".py", ".html"} and path.name != "dates.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"%[-#][a-zA-Z]", line)
    ]
    assert offenders == []
