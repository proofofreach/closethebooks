"""Create workspaces and register account sources.

`init_workspace` scaffolds a new company's books. `add_account_source`
registers a bank or card account: it records the source and opens the ledger
account when it is not already open. Neither function posts transactions.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import tomllib
from importlib import resources
from pathlib import Path

from beancount import loader
from beancount.core.data import Open

from . import tomlwrite
from .accounts import INGESTION_PROVIDERS, LIFECYCLES, STATEMENT_BALANCE_SIGNS
from .workspace import ACCOUNT_PATTERN, CONFIG_NAME, CURRENCY_PATTERN, Workspace, load_workspace

STATEMENT_FORMATS = ("csv", "ofx", "qfx", "qbo", "mt940", "sta", "swift", "pdf")
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

SOURCES_HEADER = """\
# Account sources: one table per bank or card account.
# The key (for example "operating-checking") identifies statement uploads:
#   books propose --statement <key>=<file>
# lifecycle: active | dormant | closed. Set it explicitly; age never changes it.
# ingestion: statement | plaid | teller. Feeds also need feed_account_id.
# statement_balance_sign: "ledger" keeps the bank's sign; "invert" turns a
#   positive card balance into a negative liability.
# statement_extensions limits uploads; statement_preset picks a PDF parser.
# Manage entries with `books add-account` or the Accounts page."""

STARTER_ACCOUNTS = (
    "Assets:Current:Accounts-Receivable",
    "Equity:Opening-Balances",
    "Equity:Owner-Contributions",
    "Equity:Owner-Draws",
    "Income:Sales",
    "Income:Interest",
    "Income:Other",
    "Expenses:Advertising",
    "Expenses:Bank-Fees",
    "Expenses:Contractors",
    "Expenses:Equipment",
    "Expenses:Insurance",
    "Expenses:Meals",
    "Expenses:Office",
    "Expenses:Professional-Fees",
    "Expenses:Rent",
    "Expenses:Software",
    "Expenses:Taxes-Licenses",
    "Expenses:Travel",
    "Expenses:Utilities",
)

GITIGNORE = """\
# Raw downloads, feed pulls, and credentials never belong in git.
inbox/
secrets/
.env
tokens.json
# Derived files
proposed.beancount
reports/
.DS_Store
"""

PRE_COMMIT = """\
#!/bin/sh
# Block commits that leave the ledger invalid.
exec books check --quiet
"""


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fava_config(workspace: Workspace) -> str:
    chart = workspace.chart
    collapse = "|".join(re.escape(prefix.rstrip(":")) for prefix in chart.card_prefixes)
    return f"""\
; Fava report settings. Edit freely; `books` never regenerates this file.
option "documents" "documents"

1970-01-01 custom "fava-option" "invert-income-liabilities-equity" "true"
1970-01-01 custom "fava-option" "sidebar-show-queries" "6"
1970-01-01 custom "fava-option" "collapse-pattern" "{collapse}:.*"

1970-01-01 query "Needs review" "
  SELECT date, payee, narration, account, position
  WHERE flag = '!' OR account = '{chart.uncategorized_expense}'
     OR account = '{chart.uncategorized_income}' ORDER BY date DESC"
1970-01-01 query "Open receivables" "
  SELECT date, payee, narration, position
  WHERE account = '{chart.receivable}'"
1970-01-01 query "Transfer clearing (should be zero)" "
  SELECT sum(position) WHERE account = '{chart.transfer_clearing}'"
1970-01-01 query "Spending by payee (this year)" "
  SELECT payee, sum(position) AS total
  WHERE account ~ '^Expenses' AND year = YEAR(TODAY())
  GROUP BY payee ORDER BY total DESC"
1970-01-01 query "Spending by category (this year)" "
  SELECT account, sum(position) AS total
  WHERE account ~ '^Expenses' AND year = YEAR(TODAY())
  GROUP BY account ORDER BY total DESC"
"""


def config_payload(
    name: str,
    currency: str,
    *,
    entity: str = "",
    email: str = "",
    country: str = "US",
    invoice_prefix: str = "INV",
) -> dict:
    return {
        "company": {
            "name": name,
            "legal_name": "",
            "entity": entity,
            "email": email,
            "address": [],
            "country": country,
            "currency": currency,
        },
        "chart": {
            "cash_prefixes": ["Assets:Bank:"],
            "card_prefixes": ["Liabilities:Cards:"],
            "receivable": "Assets:Current:Accounts-Receivable",
            "sales_income": "Income:Sales",
            "transfer_clearing": "Liabilities:Current:Transfers-In-Transit",
            "uncategorized_expense": "Expenses:Uncategorized",
            "uncategorized_income": "Income:Uncategorized",
        },
        "invoices": {
            "prefix": invoice_prefix,
            "due_days": 14,
            "payment_instructions": "",
            "notes": "Thank you for your business.",
        },
    }


CONFIG_HEADER = """\
# Workspace settings for the `books` tools. Every company-specific fact
# lives here. Paths are relative to this file; add a [paths] table to move
# the ledger, sources, inbox, secrets, proposals, or reports."""


def write_agent_files(workspace: Workspace, overwrite: bool = False) -> list[Path]:
    """Install the bookkeeping agent instructions and skills."""
    template_root = resources.files("bookkeeping") / "templates" / "workspace"
    targets = {
        "AGENTS.md": workspace.root / "AGENTS.md",
        "skills/do-the-books/SKILL.md": workspace.root
        / ".claude/skills/do-the-books/SKILL.md",
        "skills/make-invoice/SKILL.md": workspace.root
        / ".claude/skills/make-invoice/SKILL.md",
    }
    written = []
    for name, target in targets.items():
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((template_root / name).read_text(encoding="utf-8"), encoding="utf-8")
        written.append(target)
    claude = workspace.root / "CLAUDE.md"
    if not claude.exists():
        claude.write_text("@AGENTS.md\n", encoding="utf-8")
        written.append(claude)
    return written


def init_workspace(
    root: Path,
    *,
    name: str,
    currency: str = "USD",
    entity: str = "",
    email: str = "",
    country: str = "US",
    invoice_prefix: str = "INV",
    start_date: dt.date | None = None,
    git: bool = True,
) -> Workspace:
    root = root.expanduser().resolve()
    if (root / CONFIG_NAME).exists():
        raise FileExistsError(f"{root / CONFIG_NAME} already exists.")
    if not name.strip():
        raise ValueError("Enter the company name.")
    currency = currency.strip().upper()
    if not CURRENCY_PATTERN.match(currency):
        raise ValueError(f"{currency!r} is not a valid currency code.")
    start_date = start_date or dt.date(dt.date.today().year, 1, 1)
    root.mkdir(parents=True, exist_ok=True)
    tomlwrite.write(
        root / CONFIG_NAME,
        config_payload(
            name.strip(),
            currency,
            entity=entity,
            email=email,
            country=country,
            invoice_prefix=invoice_prefix,
        ),
        CONFIG_HEADER,
    )
    workspace = load_workspace(root)
    ledger_root = workspace.ledger_root
    (ledger_root / "years").mkdir(parents=True, exist_ok=True)
    workspace.documents.mkdir(parents=True, exist_ok=True)
    workspace.inbox.mkdir(parents=True, exist_ok=True)

    chart = workspace.chart
    accounts = sorted(
        {
            *STARTER_ACCOUNTS,
            chart.receivable,
            chart.sales_income,
            chart.transfer_clearing,
            chart.uncategorized_expense,
            chart.uncategorized_income,
        }
    )
    (ledger_root / "accounts.beancount").write_text(
        "; Chart of accounts. Add accounts with `open` directives as needed.\n\n"
        + "\n".join(f"{start_date.isoformat()} open {account}  {currency}" for account in accounts)
        + "\n",
        encoding="utf-8",
    )
    year_file = ledger_root / "years" / f"{start_date.year}.beancount"
    year_file.write_text(f"; Transactions dated {start_date.year}\n", encoding="utf-8")
    (ledger_root / "fava.beancount").write_text(fava_config(workspace), encoding="utf-8")
    workspace.ledger_main.write_text(
        "\n".join(
            [
                f"option \"title\" {_quote(workspace.company.name)}",
                f'option "operating_currency" "{currency}"',
                "",
                'include "fava.beancount"',
                'include "accounts.beancount"',
                f'include "years/{start_date.year}.beancount"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    tomlwrite.write(workspace.sources_file, {"accounts": {}}, SOURCES_HEADER)
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    write_agent_files(workspace)

    if git and shutil.which("git") and not (root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        hook = root / ".git" / "hooks" / "pre-commit"
        hook.write_text(PRE_COMMIT, encoding="utf-8")
        hook.chmod(0o755)
    return workspace


def _open_accounts(workspace: Workspace) -> dict[str, Open]:
    entries, _errors, _ = loader.load_file(str(workspace.ledger_main))
    return {entry.account: entry for entry in entries if isinstance(entry, Open)}


def read_sources(workspace: Workspace) -> dict:
    if not workspace.sources_file.exists():
        return {"accounts": {}}
    payload = tomllib.loads(workspace.sources_file.read_text(encoding="utf-8"))
    payload.setdefault("accounts", {})
    return payload


def slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value[:63] or "account"


def add_account_source(
    workspace: Workspace,
    *,
    key: str,
    name: str,
    account: str,
    opened: dt.date | None = None,
    lifecycle: str = "active",
    ingestion: str = "statement",
    statement_extensions: list[str] | tuple[str, ...] = ("csv", "ofx", "qfx", "qbo"),
    statement_balance_sign: str | None = None,
    statement_preset: str | None = None,
    expected_cadence_days: int = 35,
    overdue_after_days: int = 90,
    cutover_date: dt.date | None = None,
    feed_account_id: str | None = None,
    evidence_required_after: dt.date | None = None,
    replace: bool = False,
) -> dict:
    """Register (or update) an account source and open its ledger account."""
    key = key.strip().lower()
    if not KEY_PATTERN.match(key):
        raise ValueError("Use lowercase letters, digits, and dashes for the account key.")
    account = account.strip()
    if not ACCOUNT_PATTERN.match(account) or not account.startswith(("Assets:", "Liabilities:")):
        raise ValueError(
            "The ledger account must look like Assets:Bank:Checking or "
            "Liabilities:Cards:Business-Card."
        )
    if lifecycle not in LIFECYCLES:
        raise ValueError(f"Lifecycle must be one of: {', '.join(sorted(LIFECYCLES))}.")
    if ingestion not in INGESTION_PROVIDERS:
        raise ValueError(f"Ingestion must be one of: {', '.join(sorted(INGESTION_PROVIDERS))}.")
    if ingestion != "statement" and not feed_account_id:
        raise ValueError(f"A {ingestion} source needs its feed account ID.")
    extensions = sorted({ext.strip().lower().lstrip(".") for ext in statement_extensions if ext.strip()})
    unknown = [ext for ext in extensions if ext not in STATEMENT_FORMATS]
    if unknown:
        raise ValueError(f"Unsupported statement format: {', '.join(unknown)}.")
    if "pdf" in extensions and not statement_preset:
        raise ValueError("PDF statements need a parser preset (for example quickbooks-checking).")
    if expected_cadence_days < 1 or overdue_after_days < expected_cadence_days:
        raise ValueError("The overdue threshold must be at least the expected cadence.")
    sign = statement_balance_sign or (
        "invert" if account.startswith("Liabilities:") else "ledger"
    )
    if sign not in STATEMENT_BALANCE_SIGNS:
        raise ValueError("Statement balance sign must be ledger or invert.")

    payload = read_sources(workspace)
    sources = payload["accounts"]
    if key in sources and not replace:
        raise ValueError(f"An account with key {key!r} already exists.")
    for other_key, other in sources.items():
        if other_key != key and other.get("account") == account:
            raise ValueError(f"{account} is already registered as {other_key!r}.")

    opened = opened or dt.date.today()
    cutover = cutover_date or opened
    entry = {
        "name": name.strip() or account.rsplit(":", 1)[-1],
        "account": account,
        "lifecycle": lifecycle,
        "ingestion": ingestion,
        "cutover_date": cutover.isoformat(),
        "expected_cadence_days": expected_cadence_days,
        "overdue_after_days": overdue_after_days,
        "reconciliation": "statement_balance",
        "statement_balance_sign": sign,
        "statement_evidence_required_after": (evidence_required_after or cutover).isoformat(),
        "statement_extensions": extensions,
        "invert": False,
    }
    if statement_preset:
        entry["statement_preset"] = statement_preset
    if feed_account_id:
        entry["feed_account_id"] = feed_account_id
    if key in sources:
        preserved = {k: v for k, v in sources[key].items() if k not in entry}
        entry = {**entry, **preserved}
        for field in ("cutover_date", "statement_evidence_required_after"):
            if cutover_date is None and field in sources[key]:
                entry[field] = sources[key][field]
    sources[key] = entry

    opened_now = False
    if account not in _open_accounts(workspace):
        accounts_file = workspace.ledger_root / "accounts.beancount"
        with accounts_file.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n{opened.isoformat()} open {account}  {workspace.currency}\n"
                f"  source-key: {_quote(key)}\n"
            )
        opened_now = True
    tomlwrite.write(workspace.sources_file, payload, SOURCES_HEADER)
    return {"key": key, "account": account, "opened": opened_now}


def update_company(workspace: Workspace, company: dict, invoices: dict) -> Workspace:
    """Update the [company] and [invoices] tables, keeping everything else."""
    payload = tomllib.loads(workspace.config_path.read_text(encoding="utf-8"))
    payload.setdefault("company", {}).update(company)
    payload.setdefault("invoices", {}).update(invoices)
    from .workspace import parse_config

    parse_config(workspace.root, payload)
    tomlwrite.write(workspace.config_path, payload, CONFIG_HEADER)
    return load_workspace(workspace.root)
