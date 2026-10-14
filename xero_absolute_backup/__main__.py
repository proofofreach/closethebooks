from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import json
import csv

from .auth import run_login
from .backup import BackupRunner
from .client import TokenStore, XeroClient
from .config import load_settings
from .state import StateStore
from .reports import (
    generate_cash_pnl,
    generate_gl_pnl,
    generate_gl_balance_sheet,
    fetch_xero_profit_and_loss,
    fetch_xero_balance_sheet,
    flatten_report_rows,
    extract_totals_from_report,
    extract_account_totals_from_report,
    SUMMARY_ROW_LABELS,
    extract_gl_lines_for_account,
)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Resumable Xero backup tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="Run OAuth login flow")
    subparsers.add_parser("auth-check", help="Verify saved tokens and list connected organisations")

    run_parser = subparsers.add_parser("run", help="Run or resume the backup")
    run_parser.add_argument("--sleep-on-rate-limit", action="store_true", help="Sleep for Retry-After when available")
    run_parser.add_argument(
        "--modified-since",
        help="Delta mode: only fetch records modified on/after this UTC datetime (e.g. 2026-04-04 or 2026-04-04T00:00:00). Use with --output-dir and --state-db so the full backup's state is untouched.",
    )
    run_parser.add_argument("--output-dir", help="Override backup output directory (delta runs)")
    run_parser.add_argument("--state-db", help="Override sync state DB path (delta runs)")

    status_parser = subparsers.add_parser("status", help="Show saved sync status")
    status_parser.add_argument("--verbose", action="store_true", help="Include phase progress and attachment counts")

    verify_parser = subparsers.add_parser("verify", help="Verify attachments against Xero")
    verify_parser.add_argument("--full", action="store_true", help="Verify every entity with attachments")
    verify_parser.add_argument(
        "--sample",
        type=int,
        default=25,
        help="Number of entities to sample when not using --full",
    )

    pnl_parser = subparsers.add_parser("pnl", help="Generate cash-basis P&L from backup data")
    pnl_parser.add_argument("--year", type=int, default=2025, help="Year to report (YYYY)")
    pnl_parser.add_argument("--currency", default="USD", help="Currency code (default USD)")
    pnl_parser.add_argument("--source", default="bank", choices=["bank", "gl"], help="Data source for P&L")

    bs_parser = subparsers.add_parser("balancesheet", help="Generate balance sheet from GL data")
    bs_parser.add_argument("--year", type=int, default=2025, help="Year to report (YYYY)")
    bs_parser.add_argument("--currency", default="USD", help="Currency code (default USD)")

    pnlx_parser = subparsers.add_parser("pnl-xero", help="Fetch Xero P&L report and compare to local")
    pnlx_parser.add_argument("--year", type=int, default=2024, help="Year to report (YYYY)")
    pnlx_parser.add_argument("--currency", default="USD", help="Currency code (default USD)")
    pnlx_parser.add_argument("--source", default="bank", choices=["bank", "gl"], help="Local data source")
    pnlx_parser.add_argument("--diff-account", action="store_true", help="Write account-level diff and GL detail for mismatches")

    reset_parser = subparsers.add_parser("reset-phase", help="Reset a saved phase")
    reset_parser.add_argument("--phase", required=True, help="attachments or an endpoint key")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "login":
        settings = load_settings(Path.cwd())
        run_login(settings)
        return
    if args.command == "run":
        settings = load_settings(Path.cwd())
        modified_since = None
        if getattr(args, "modified_since", None):
            value = args.modified_since.strip()
            if "T" not in value:
                value = f"{value}T00:00:00"
            modified_since = value
        if getattr(args, "output_dir", None):
            settings.output_dir = Path(args.output_dir)
        if getattr(args, "state_db", None):
            settings.state_db = Path(args.state_db)
        if modified_since and not (args.output_dir and args.state_db):
            raise SystemExit(
                "--modified-since requires --output-dir and --state-db so the full backup's resume state is not corrupted."
            )
        BackupRunner(settings, modified_since=modified_since).run(
            sleep_on_rate_limit=args.sleep_on_rate_limit
        )
        return
    if args.command == "auth-check":
        settings = load_settings(Path.cwd())
        client = XeroClient(settings, TokenStore(settings.token_file))
        connections = client.list_connections()
        orgs = [
            {
                "tenantId": row.get("tenantId"),
                "tenantName": row.get("tenantName"),
                "tenantType": row.get("tenantType"),
                "createdDateUtc": row.get("createdDateUtc"),
            }
            for row in connections
        ]
        print(json.dumps(orgs, indent=2))
        return
    if args.command == "status":
        state = StateStore(Path.cwd() / "xero_sync_state.db")
        try:
            rows = state.connection.execute(
                "SELECT endpoint, last_page, last_cursor, completed, updated_at, last_error FROM sync_status ORDER BY endpoint"
            ).fetchall()
            print(json.dumps([dict(row) for row in rows], indent=2))
            summary = state.get_summary()
            if args.verbose:
                remaining_entities = state.connection.execute(
                    "SELECT COUNT(*) FROM entities WHERE has_attachments = 1 AND attachment_discovered = 0"
                ).fetchone()[0]
                remaining_downloads = state.connection.execute(
                    "SELECT COUNT(*) FROM attachments_queue WHERE downloaded = 0"
                ).fetchone()[0]
                summary = {
                    **summary,
                    "attachment_discovery_remaining_entities": remaining_entities,
                    "attachment_download_remaining": remaining_downloads,
                }
            print(json.dumps(summary, indent=2))
        finally:
            state.close()
        return
    if args.command == "reset-phase":
        state = StateStore(Path.cwd() / "xero_sync_state.db")
        try:
            if args.phase == "attachments":
                state.clear_endpoint("attachment_discovery")
                state.clear_endpoint("attachment_downloads")
                state.connection.execute("UPDATE entities SET attachment_discovered = 0 WHERE has_attachments = 1")
                state.connection.execute("UPDATE attachments_queue SET downloaded = 0, local_path = NULL")
                state.commit()
            else:
                state.clear_endpoint(args.phase)
                state.commit()
        finally:
            state.close()
        print(f"Reset phase: {args.phase}")
        return
    if args.command == "verify":
        settings = load_settings(Path.cwd())
        runner = BackupRunner(settings)
        try:
            result = runner.verify_attachments(sample_size=args.sample, full=args.full)
            print(json.dumps(result, indent=2))
        finally:
            runner.state.close()
        return
    if args.command == "pnl":
        settings = load_settings(Path.cwd())
        if args.source == "gl":
            output_path = generate_gl_pnl(
                settings.output_dir,
                year=args.year,
                currency=args.currency,
            )
        else:
            output_path = generate_cash_pnl(
                settings.output_dir,
                year=args.year,
                currency=args.currency,
            )
        print(f"Wrote P&L to {output_path}")
        return
    if args.command == "balancesheet":
        settings = load_settings(Path.cwd())
        output_path = generate_gl_balance_sheet(
            settings.output_dir,
            year=args.year,
            currency=args.currency,
        )
        print(f"Wrote balance sheet to {output_path}")
        return
    if args.command == "pnl-xero":
        settings = load_settings(Path.cwd())
        if args.source == "gl":
            local_path = generate_gl_pnl(
                settings.output_dir,
                year=args.year,
                currency=args.currency,
            )
        else:
            local_path = generate_cash_pnl(
                settings.output_dir,
                year=args.year,
                currency=args.currency,
            )
        report = fetch_xero_profit_and_loss(
            settings,
            year=args.year,
            currency=args.currency,
            cash=True,
        )
        balance_sheet = fetch_xero_balance_sheet(
            settings,
            year=args.year,
            currency=args.currency,
            cash=True,
        )
        tenant_dir = local_path.parent.parent
        report_json_path = tenant_dir / "manifests" / f"pnl_{args.year}_xero.json"
        report_rows = flatten_report_rows(report)
        report_csv_path = tenant_dir / "csv" / f"pnl_{args.year}_xero.csv"
        report_json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        from .exporters import write_csv_rows
        write_csv_rows(tenant_dir, f"pnl_{args.year}_xero.csv", report_rows)

        bs_json_path = tenant_dir / "manifests" / f"balancesheet_{args.year}_xero.json"
        bs_rows = flatten_report_rows(balance_sheet)
        bs_csv_path = tenant_dir / "csv" / f"balancesheet_{args.year}_xero.csv"
        bs_json_path.write_text(json.dumps(balance_sheet, indent=2), encoding="utf-8")
        write_csv_rows(tenant_dir, f"balancesheet_{args.year}_xero.csv", bs_rows)

        if args.source == "gl":
            local_summary_path = tenant_dir / "manifests" / f"pnl_{args.year}_gl_{args.currency.lower()}_summary.json"
        else:
            local_summary_path = tenant_dir / "manifests" / f"pnl_{args.year}_cash_{args.currency.lower()}_summary.json"
        local_summary = {}
        if local_summary_path.exists():
            local_summary = json.loads(local_summary_path.read_text(encoding="utf-8"))
        xero_totals = extract_totals_from_report(report)
        comparison = {
            "local_summary": local_summary,
            "xero_totals": xero_totals,
            "local_csv": str(local_path),
            "xero_csv": str(report_csv_path),
            "xero_json": str(report_json_path),
            "balancesheet_xero_csv": str(bs_csv_path),
            "balancesheet_xero_json": str(bs_json_path),
        }
        comparison_path = tenant_dir / "manifests" / f"pnl_{args.year}_comparison.json"
        comparison_path.write_text(json.dumps(comparison, indent=2), encoding="utf-8")

        # Account-level diff
        if args.source == "gl":
            xero_accounts = extract_account_totals_from_report(report)
            local_accounts = {}
            local_csv_path = Path(local_path)
            with local_csv_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    name = (row.get("account_name") or "").strip()
                    net_raw = (row.get("net") or "").strip()
                    try:
                        net_val = float(net_raw)
                    except ValueError:
                        net_val = 0.0
                    if name:
                        local_accounts[name] = net_val
            diff_rows = []
            all_names = sorted(set(local_accounts) | set(xero_accounts))
            for name in all_names:
                if name.strip().lower() in SUMMARY_ROW_LABELS:
                    continue
                local_val = local_accounts.get(name, 0.0)
                xero_val = xero_accounts.get(name, 0.0)
                # Xero reports present expense lines as positive values.
                if xero_val < 0 and local_val < 0:
                    local_val = abs(local_val)
                    xero_val = abs(xero_val)
                diff_rows.append(
                    {
                        "account_name": name,
                        "local_amount": f"{local_val:.2f}",
                        "xero_amount": f"{xero_val:.2f}",
                        "diff": f"{(local_val - xero_val):.2f}",
                    }
                )
            write_csv_rows(tenant_dir, f"pnl_{args.year}_gl_vs_xero_account_diff.csv", diff_rows)

            # Balance sheet diff
            local_bs_path = generate_gl_balance_sheet(
                settings.output_dir,
                year=args.year,
                currency=args.currency,
            )
            bs_accounts = extract_account_totals_from_report(balance_sheet)
            local_bs_accounts = {}
            with Path(local_bs_path).open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    name = (row.get("account_name") or "").strip()
                    bal_raw = (row.get("balance") or "").strip()
                    try:
                        bal_val = float(bal_raw)
                    except ValueError:
                        bal_val = 0.0
                    if name:
                        local_bs_accounts[name] = bal_val
            bs_diff_rows = []
            all_bs_names = sorted(set(local_bs_accounts) | set(bs_accounts))
            for name in all_bs_names:
                local_val = local_bs_accounts.get(name, 0.0)
                xero_val = bs_accounts.get(name, 0.0)
                bs_diff_rows.append(
                    {
                        "account_name": name,
                        "local_amount": f"{local_val:.2f}",
                        "xero_amount": f"{xero_val:.2f}",
                        "diff": f"{(local_val - xero_val):.2f}",
                    }
                )
            write_csv_rows(tenant_dir, f"balancesheet_{args.year}_gl_vs_xero_account_diff.csv", bs_diff_rows)

            if args.diff_account:
                # Write GL detail for the two most mismatched accounts.
                sorted_rows = sorted(
                    diff_rows,
                    key=lambda r: abs(float(r["diff"])),
                    reverse=True,
                )
                for row in sorted_rows[:2]:
                    name = row["account_name"]
                    lines = extract_gl_lines_for_account(settings.output_dir, year=args.year, account_name=name)
                    if not lines:
                        continue
                    write_csv_rows(
                        tenant_dir,
                        f"pnl_{args.year}_gl_detail_{name.replace(' ', '_').replace('/', '_')}.csv",
                        lines,
                    )

        print(f"Wrote comparison to {comparison_path}")
        return


if __name__ == "__main__":
    main()
