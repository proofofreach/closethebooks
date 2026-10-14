from __future__ import annotations

import datetime as dt
import json
import sys
from argparse import ArgumentParser
from pathlib import Path

from .workspace import PRODUCT_NAME, Workspace, WorkspaceError, resolve_workspace


def _date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="books",
        description=f"{PRODUCT_NAME}: local-first bookkeeping on a plain-text ledger.",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        default=None,
        help="Workspace directory (default: $BOOKS_WORKSPACE, then the nearest books.toml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a new company workspace")
    init_parser.add_argument("directory", nargs="?", default=".")
    init_parser.add_argument("--name", required=True, help="Company name")
    init_parser.add_argument("--currency", default="USD")
    init_parser.add_argument("--entity", default="", help="e.g. Single-member LLC")
    init_parser.add_argument("--email", default="")
    init_parser.add_argument("--country", default="US")
    init_parser.add_argument("--invoice-prefix", default="INV")
    init_parser.add_argument(
        "--start-date", type=_date, default=None, help="First day of the books (YYYY-MM-DD)"
    )
    init_parser.add_argument("--no-git", action="store_true")

    demo_parser = subparsers.add_parser(
        "demo", help="Create a fictional demo workspace to try the app"
    )
    demo_parser.add_argument("directory", nargs="?", default="books-demo")

    agent_parser = subparsers.add_parser(
        "agent-files", help="Install or refresh the bookkeeping agent instructions"
    )
    agent_parser.add_argument("--force", action="store_true", help="Overwrite existing files")

    add_parser = subparsers.add_parser(
        "add-account", help="Register a bank or card account as a statement or feed source"
    )
    add_parser.add_argument("key", help="Short label, e.g. operating-checking")
    add_parser.add_argument("--account", required=True, help="e.g. Assets:Bank:Operating-Checking")
    add_parser.add_argument("--name", default="", help="Display name")
    add_parser.add_argument("--opened", type=_date, default=None)
    add_parser.add_argument(
        "--cutover", type=_date, default=None, help="Ignore feed rows before this date"
    )
    add_parser.add_argument(
        "--lifecycle", choices=("active", "dormant", "closed"), default="active"
    )
    add_parser.add_argument(
        "--ingestion", choices=("statement", "plaid", "teller"), default="statement"
    )
    add_parser.add_argument("--feed-account-id", default=None)
    add_parser.add_argument("--formats", default="csv,ofx,qfx,qbo")
    add_parser.add_argument("--preset", default=None, help="PDF parser preset")
    add_parser.add_argument("--balance-sign", choices=("ledger", "invert"), default=None)
    add_parser.add_argument("--cadence-days", type=int, default=35)
    add_parser.add_argument("--overdue-days", type=int, default=90)
    add_parser.add_argument("--replace", action="store_true", help="Update an existing key")

    status_parser = subparsers.add_parser(
        "status", help="Per-account coverage, verification, and close obligations"
    )
    status_parser.add_argument("--json", action="store_true", help="Emit JSON")

    check_parser = subparsers.add_parser("check", help="Validate the ledger")
    check_parser.add_argument("--quiet", action="store_true")

    query_parser = subparsers.add_parser("query", help="Run a Beancount query (BQL)")
    query_parser.add_argument("query", nargs="+")
    query_parser.add_argument("--format", "-f", choices=("text", "csv"), default="text")

    fetch_parser = subparsers.add_parser(
        "fetch", help="Pull posted transactions from Plaid or Teller into the inbox"
    )
    fetch_parser.add_argument(
        "--provider", choices=("auto", "plaid", "teller"), default="auto"
    )

    plaid_connect_parser = subparsers.add_parser(
        "plaid-connect", help="Connect an institution through local-only Plaid Link"
    )
    plaid_connect_parser.add_argument("--redirect-uri", default="http://localhost:8767/")

    subparsers.add_parser(
        "plaid-accounts", help="List connected Plaid account IDs for add-account"
    )

    plaid_revisions_parser = subparsers.add_parser(
        "plaid-revisions",
        help="List or resolve bank revisions that may affect posted transactions",
    )
    plaid_revisions_parser.add_argument(
        "--resolve",
        action="append",
        default=[],
        metavar="TRANSACTION_ID",
        help="Mark a reviewed transaction revision resolved",
    )

    propose_parser = subparsers.add_parser(
        "propose",
        help="Propose deduplicated entries from statements and connected feeds",
    )
    propose_parser.add_argument(
        "--statement",
        action="append",
        default=[],
        metavar="KEY=PATH[:preset]",
        help="Statement file for an account key, e.g. operating-checking=inbox/june.csv",
    )
    propose_parser.add_argument("--output", default=None)

    report_parser = subparsers.add_parser(
        "report", help="Write per-year reports (income, balance, cash flow, trial, summary)"
    )
    report_parser.add_argument("--output", default=None)
    report_parser.add_argument("--year", type=int, default=None)

    invoice_parser = subparsers.add_parser(
        "invoice", help="Create an invoice document and print its ledger entry"
    )
    invoice_parser.add_argument("--client", required=True)
    invoice_parser.add_argument("--address", action="append", default=[])
    invoice_parser.add_argument("--item", action="append", required=True, dest="items")
    invoice_parser.add_argument("--amount", action="append", required=True, dest="amounts")
    invoice_parser.add_argument("--number", default=None)
    invoice_parser.add_argument("--date", type=_date, default=None)
    invoice_parser.add_argument("--due-days", type=int, default=None)
    invoice_parser.add_argument("--notes", default=None)

    serve_parser = subparsers.add_parser("serve", help="Open the local web app")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5170)
    serve_parser.add_argument("--no-browser", action="store_true")

    convert_parser = subparsers.add_parser(
        "xero-convert", help="Build the ledger from a Xero backup (one-time migration)"
    )
    convert_parser.add_argument("--backup-root", default=None)

    merge_parser = subparsers.add_parser(
        "xero-merge-gl",
        help="Merge Xero 'General Ledger Detail' XLSX exports before xero-convert",
    )
    merge_parser.add_argument("xlsx", nargs="+", help="GL Detail XLSX export(s) from Xero")
    merge_parser.add_argument("--backup-root", default=None)

    verify_parser = subparsers.add_parser(
        "xero-verify", help="Prove the converted ledger matches the Xero general ledger"
    )
    verify_parser.add_argument("--backup-root", default=None)
    return parser


def _load_dotenv(path: Path) -> None:
    """Load KEY=value pairs (Plaid credentials) without overriding the shell."""
    import os

    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def run(args, workspace: Workspace) -> int:
    command = args.command

    if command == "agent-files":
        from .setup import write_agent_files

        written = write_agent_files(workspace, overwrite=args.force)
        _print({"written": [workspace.relative(path) for path in written]})
        return 0

    if command == "add-account":
        from .setup import add_account_source

        result = add_account_source(
            workspace,
            key=args.key,
            name=args.name,
            account=args.account,
            opened=args.opened,
            cutover_date=args.cutover,
            lifecycle=args.lifecycle,
            ingestion=args.ingestion,
            feed_account_id=args.feed_account_id,
            statement_extensions=args.formats.split(","),
            statement_preset=args.preset,
            statement_balance_sign=args.balance_sign,
            expected_cadence_days=args.cadence_days,
            overdue_after_days=args.overdue_days,
            replace=args.replace,
        )
        _print(result)
        return 0

    if command == "status":
        from .status import account_status, render

        status = account_status(workspace)
        print(json.dumps(status, indent=2) if args.json else render(status))
        return 0

    if command == "check":
        from beancount import loader
        from beancount.parser import printer

        _entries, errors, _ = loader.load_file(str(workspace.ledger_main))
        if errors:
            printer.print_errors(errors, file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"{workspace.relative(workspace.ledger_main)}: no errors.")
        return 0

    if command == "query":
        from beanquery.shell import main as bean_query

        return bean_query.main(
            args=[str(workspace.ledger_main), " ".join(args.query), "--format", args.format],
            standalone_mode=False,
        ) or 0

    if command == "fetch":
        from .accounts import configured_feed_providers, load_account_sources

        if args.provider == "auto":
            providers = configured_feed_providers(
                load_account_sources(workspace.sources_file, workspace.currency)
            )
        else:
            providers = {args.provider}
        summary: dict = {}
        if "plaid" in providers:
            from .plaid import PlaidItemStore, PlaidSnapshot, client_from_environment, fetch_all

            summary["plaid"] = fetch_all(
                client_from_environment(),
                PlaidItemStore(workspace.secrets / "plaid"),
                PlaidSnapshot(workspace.inbox / "plaid"),
                dt.date.today().isoformat(),
            )
        if "teller" in providers:
            from .teller import TellerClient

            client = TellerClient(workspace.secrets / "teller")
            result = client.fetch_all(workspace.inbox / "teller", dt.date.today().isoformat())
            summary["teller"] = {
                account_id: {
                    "total": len(txns),
                    "pending": sum(1 for t in txns if t.pending),
                    "newest": max((t.date for t in txns), default=None),
                }
                for account_id, txns in result.items()
            }
        if not providers:
            summary["feed"] = "No active Plaid or Teller sources are configured."
        _print(summary)
        return 0

    if command == "plaid-connect":
        from .plaid import PlaidItemStore, client_from_environment
        from .plaid_connect import run_connect

        connected = run_connect(
            client_from_environment(),
            PlaidItemStore(workspace.secrets / "plaid"),
            args.redirect_uri,
            company=workspace.company.name,
            country_code=workspace.company.country,
        )
        _print(
            {
                "institution_name": connected["institution_name"],
                "accounts": connected["accounts"],
            }
        )
        return 0

    if command == "plaid-accounts":
        from .plaid import PlaidItemStore

        items = PlaidItemStore(workspace.secrets / "plaid").items()
        _print(
            [
                {
                    "institution": item.get("institution_name", ""),
                    "account_id": account.get("account_id"),
                    "name": account.get("name"),
                    "official_name": account.get("official_name"),
                    "mask": account.get("mask"),
                    "type": account.get("type"),
                    "subtype": account.get("subtype"),
                }
                for item in items
                for account in item.get("accounts", [])
            ]
        )
        return 0

    if command == "plaid-revisions":
        from .plaid import PlaidSnapshot

        snapshot = PlaidSnapshot(workspace.inbox / "plaid")
        resolved = {
            transaction_id: snapshot.resolve_revision(transaction_id)
            for transaction_id in args.resolve
        }
        _print({"resolved": resolved, "pending": snapshot.pending_revisions()})
        return 0

    if command == "propose":
        from .propose import parse_statement_spec, run_propose

        _print(
            run_propose(
                workspace,
                [parse_statement_spec(spec) for spec in args.statement],
                Path(args.output) if args.output else None,
            )
        )
        return 0

    if command == "report":
        from .reports import generate

        _print(generate(workspace, Path(args.output) if args.output else None, args.year))
        return 0

    if command == "invoice":
        from .invoices import create_invoice

        if len(args.items) != len(args.amounts):
            raise ValueError("Give one --amount for each --item.")
        result = create_invoice(
            workspace,
            client=args.client,
            client_address=args.address,
            items=list(zip(args.items, args.amounts)),
            issue_date=args.date,
            due_days=args.due_days,
            number=args.number,
            notes=args.notes,
        )
        print(f"Wrote {workspace.relative(result.document)}")
        print(
            f"Total: {result.invoice.total:,.2f} {workspace.currency}. "
            "Open it in a browser and print to PDF to send."
        )
        print(
            "\nLedger entry (append to "
            f"{workspace.relative(workspace.ledger_root)}/years/"
            f"{result.invoice.issue_date.year}.beancount after review):\n"
        )
        print(result.entry)
        return 0

    if command == "serve":
        from .app import serve

        serve(workspace, host=args.host, port=args.port, open_browser=not args.no_browser)
        return 0

    if command == "xero-convert":
        from .beancount_convert import convert

        backup = Path(args.backup_root) if args.backup_root else None
        _print(convert(workspace, backup))
        return 0

    if command == "xero-merge-gl":
        from .xero_gl import merge_gl_files

        backup = Path(args.backup_root) if args.backup_root else workspace.xero_backup
        paths = [Path(path) for path in args.xlsx]
        for path in paths:
            if not path.exists():
                raise FileNotFoundError(f"Missing GL export: {path}")
        output = backup / "csv" / "general_ledger_merged.csv"
        output.parent.mkdir(parents=True, exist_ok=True)
        _print({"output": workspace.relative(output), **merge_gl_files(paths, output)})
        return 0

    if command == "xero-verify":
        from .verify_conversion import verify, write_report

        backup = Path(args.backup_root) if args.backup_root else workspace.xero_backup
        report = verify(backup, workspace.ledger_root)
        write_report(report, workspace.ledger_root / "verify_report.json")
        _print(report)
        return 0 if report["status"] == "verified" else 1

    raise AssertionError(f"Unhandled command {command}")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            from .setup import init_workspace

            workspace = init_workspace(
                Path(args.directory),
                name=args.name,
                currency=args.currency,
                entity=args.entity,
                email=args.email,
                country=args.country,
                invoice_prefix=args.invoice_prefix,
                start_date=args.start_date,
                git=not args.no_git,
            )
            print(f"Created workspace for {workspace.company.name} in {workspace.root}")
            print("Next: books add-account <key> --account Assets:Bank:<Name>, then books serve")
            return
        if args.command == "demo":
            from .demo import create_demo

            root = create_demo(Path(args.directory))
            print(f"Created a demo workspace in {root}")
            print(f"Open it: books --workspace {root} serve")
            return
        workspace = resolve_workspace(args.workspace)
        _load_dotenv(workspace.root / ".env")
        sys.exit(run(args, workspace))
    except (WorkspaceError, FileExistsError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"books: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
