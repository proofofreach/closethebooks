"""Workspace configuration: one company's books, described by `books.toml`.

A workspace is a directory holding a company's ledger, statement inbox,
secrets, and account sources. Every company-specific fact (name, currency,
account roles, invoice numbering, file layout) lives in `books.toml`; the
package itself carries none.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "books.toml"
ENVIRONMENT_VARIABLE = "BOOKS_WORKSPACE"
PRODUCT_NAME = "CloseTheBooks"
CURRENCY_PATTERN = re.compile(r"^[A-Z][A-Z0-9'._-]{0,22}[A-Z0-9]$")
ACCOUNT_PATTERN = re.compile(r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z0-9][A-Za-z0-9-]*)+$")


class WorkspaceError(RuntimeError):
    """The workspace is missing or its configuration is invalid."""


@dataclass(frozen=True)
class Company:
    name: str
    legal_name: str = ""
    email: str = ""
    address: tuple[str, ...] = ()
    entity: str = ""
    country: str = "US"
    currency: str = "USD"

    @property
    def display_name(self) -> str:
        return self.legal_name or self.name


@dataclass(frozen=True)
class Chart:
    """Ledger accounts that carry a specific meaning for the tools."""

    cash_prefixes: tuple[str, ...] = ("Assets:Bank:",)
    card_prefixes: tuple[str, ...] = ("Liabilities:Cards:",)
    fixed_asset_prefixes: tuple[str, ...] = ("Assets:Fixed:",)
    long_term_liability_prefixes: tuple[str, ...] = ("Liabilities:Term:",)
    receivable: str = "Assets:Current:Accounts-Receivable"
    sales_income: str = "Income:Sales"
    transfer_clearing: str = "Liabilities:Current:Transfers-In-Transit"
    uncategorized_expense: str = "Expenses:Uncategorized"
    uncategorized_income: str = "Income:Uncategorized"

    def is_cash(self, account: str) -> bool:
        return account.startswith(self.cash_prefixes)

    def is_statement_account(self, account: str) -> bool:
        return account.startswith(self.cash_prefixes + self.card_prefixes)

    def is_uncategorized(self, account: str) -> bool:
        return account in {self.uncategorized_expense, self.uncategorized_income}


@dataclass(frozen=True)
class Invoicing:
    prefix: str = "INV"
    due_days: int = 14
    notes: str = "Thank you for your business."
    payment_instructions: str = ""


@dataclass(frozen=True)
class Assistant:
    """Local AI command-line tools used for read-only questions."""

    engines: tuple[str, ...] = ("claude", "codex")
    timeout_seconds: int = 240


@dataclass(frozen=True)
class Workspace:
    root: Path
    company: Company
    chart: Chart = field(default_factory=Chart)
    invoicing: Invoicing = field(default_factory=Invoicing)
    assistant: Assistant = field(default_factory=Assistant)
    ledger_main: Path = Path("ledger/main.beancount")
    sources_file: Path = Path("sources.toml")
    inbox: Path = Path("inbox")
    secrets: Path = Path("secrets")
    proposals: Path = Path("proposed.beancount")
    reports: Path = Path("reports")
    xero_backup: Path = Path("imports/xero")

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def ledger_root(self) -> Path:
        return self.ledger_main.parent

    @property
    def documents(self) -> Path:
        return self.ledger_root / "documents"

    @property
    def evidence(self) -> Path:
        return self.ledger_root / "evidence" / "statements"

    @property
    def currency(self) -> str:
        return self.company.currency

    @property
    def invoice_dir(self) -> Path:
        return self.documents / Path(*self.chart.sales_income.split(":"))

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)


def _strings(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkspaceError(f"{CONFIG_NAME}: {name} must be a list of strings.")
    return tuple(value)


def _account(value: object, name: str) -> str:
    if not isinstance(value, str) or not ACCOUNT_PATTERN.match(value):
        raise WorkspaceError(
            f"{CONFIG_NAME}: {name} must be a Beancount account name, got {value!r}."
        )
    return value


def _prefixes(value: object, name: str) -> tuple[str, ...]:
    prefixes = _strings(value, name)
    for prefix in prefixes:
        if not ACCOUNT_PATTERN.match(prefix.rstrip(":")):
            raise WorkspaceError(f"{CONFIG_NAME}: {name} has invalid prefix {prefix!r}.")
    return tuple(prefix if prefix.endswith(":") else f"{prefix}:" for prefix in prefixes)


def parse_config(root: Path, payload: dict) -> Workspace:
    company_data = payload.get("company") or {}
    name = str(company_data.get("name") or "").strip()
    if not name:
        raise WorkspaceError(f"{CONFIG_NAME}: [company] name is required.")
    currency = str(company_data.get("currency") or "USD").strip()
    if not CURRENCY_PATTERN.match(currency):
        raise WorkspaceError(f"{CONFIG_NAME}: invalid currency {currency!r}.")
    company = Company(
        name=name,
        legal_name=str(company_data.get("legal_name") or ""),
        email=str(company_data.get("email") or ""),
        address=_strings(company_data.get("address", []), "company.address"),
        entity=str(company_data.get("entity") or ""),
        country=str(company_data.get("country") or "US"),
        currency=currency,
    )

    defaults = Chart()
    chart_data = payload.get("chart") or {}
    chart = Chart(
        cash_prefixes=_prefixes(
            chart_data.get("cash_prefixes", list(defaults.cash_prefixes)),
            "chart.cash_prefixes",
        ),
        card_prefixes=_prefixes(
            chart_data.get("card_prefixes", list(defaults.card_prefixes)),
            "chart.card_prefixes",
        ),
        fixed_asset_prefixes=_prefixes(
            chart_data.get("fixed_asset_prefixes", list(defaults.fixed_asset_prefixes)),
            "chart.fixed_asset_prefixes",
        ),
        long_term_liability_prefixes=_prefixes(
            chart_data.get(
                "long_term_liability_prefixes",
                list(defaults.long_term_liability_prefixes),
            ),
            "chart.long_term_liability_prefixes",
        ),
        **{
            key: _account(chart_data.get(key, getattr(defaults, key)), f"chart.{key}")
            for key in (
                "receivable",
                "sales_income",
                "transfer_clearing",
                "uncategorized_expense",
                "uncategorized_income",
            )
        },
    )

    invoice_data = payload.get("invoices") or {}
    prefix = str(invoice_data.get("prefix") or "INV")
    if not re.fullmatch(r"[A-Za-z0-9]{1,12}", prefix):
        raise WorkspaceError(f"{CONFIG_NAME}: invoices.prefix must be 1-12 letters or digits.")
    invoicing = Invoicing(
        prefix=prefix,
        due_days=int(invoice_data.get("due_days", 14)),
        notes=str(invoice_data.get("notes", Invoicing.notes)),
        payment_instructions=str(invoice_data.get("payment_instructions", "")),
    )

    assistant_data = payload.get("assistant") or {}
    assistant = Assistant(
        engines=_strings(assistant_data.get("engines", ["claude", "codex"]), "assistant.engines"),
        timeout_seconds=int(assistant_data.get("timeout_seconds", 240)),
    )

    path_data = payload.get("paths") or {}

    def resolve(key: str, default: str) -> Path:
        value = Path(str(path_data.get(key, default))).expanduser()
        return value if value.is_absolute() else root / value

    return Workspace(
        root=root,
        company=company,
        chart=chart,
        invoicing=invoicing,
        assistant=assistant,
        ledger_main=resolve("ledger", "ledger/main.beancount"),
        sources_file=resolve("sources", "sources.toml"),
        inbox=resolve("inbox", "inbox"),
        secrets=resolve("secrets", "secrets"),
        proposals=resolve("proposals", "proposed.beancount"),
        reports=resolve("reports", "reports"),
        xero_backup=resolve("xero_backup", "imports/xero"),
    )


def load_workspace(root: Path) -> Workspace:
    root = root.expanduser().resolve()
    config = root / CONFIG_NAME
    if not config.is_file():
        raise WorkspaceError(f"No {CONFIG_NAME} in {root}. Run `books init` to create one.")
    try:
        payload = tomllib.loads(config.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise WorkspaceError(f"{config}: {exc}") from exc
    return parse_config(root, payload)


def find_workspace_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    return None


def resolve_workspace(explicit: str | Path | None = None, start: Path | None = None) -> Workspace:
    """Locate the workspace: explicit path, then $BOOKS_WORKSPACE, then parents of cwd."""
    if explicit:
        return load_workspace(Path(explicit))
    from_environment = os.environ.get(ENVIRONMENT_VARIABLE)
    if from_environment:
        return load_workspace(Path(from_environment))
    root = find_workspace_root(start)
    if root is None:
        raise WorkspaceError(
            f"No {CONFIG_NAME} found in {start or Path.cwd()} or its parents. "
            "Run `books init <directory>` to create a workspace, or pass --workspace."
        )
    return load_workspace(root)
