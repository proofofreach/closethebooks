"""Guided one-time migration from Xero into a new workspace.

Steps: connect (OAuth with PKCE), back up the organisation through the API,
merge the General Ledger Detail export, convert, verify, and register the
bank and card accounts as sources. Conversion regenerates the ledger, so it
is allowed only while the ledger holds nothing but a previous conversion.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from beancount import loader
from beancount.core.data import Open, Transaction

from .workspace import Workspace

# Granular read scopes (Xero retired the broad accounting.transactions scope
# in 2026). Override per workspace with "scopes" in secrets/xero/client.json.
AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "accounting.invoices.read",
    "accounting.banktransactions.read",
    "accounting.contacts.read",
    "accounting.settings.read",
    "accounting.attachments.read",
    "accounting.reports.profitandloss.read",
    "accounting.reports.balancesheet.read",
)


@dataclass
class BackupJob:
    state: str = "idle"  # idle | running | paused | complete | failed
    message: str = ""
    started: dt.datetime | None = None
    finished: dt.datetime | None = None
    thread: threading.Thread | None = field(default=None, repr=False)


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class XeroMigration:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.secret_dir = workspace.secrets / "xero"
        self.client_file = self.secret_dir / "client.json"
        self.token_file = self.secret_dir / "tokens.json"
        self.output_dir = workspace.root / "imports" / "xero"
        self.state_db = self.output_dir / "state.db"
        self.job = BackupJob()
        self._pending: dict[str, dict] = {}

    # Connection ------------------------------------------------------------

    def client(self) -> dict:
        if self.client_file.exists():
            return json.loads(self.client_file.read_text(encoding="utf-8"))
        return {}

    def _write_private(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def save_client(self, client_id: str, client_secret: str = "") -> None:
        client_id = client_id.strip()
        if not client_id or len(client_id) > 128 or not client_id.isalnum():
            raise ValueError("Paste the Client ID from your Xero app. It is a string of letters and digits.")
        payload = {**self.client(), "client_id": client_id}
        if client_secret.strip():
            payload["client_secret"] = client_secret.strip()
        else:
            payload.pop("client_secret", None)
        self._write_private(self.client_file, payload)

    def connected(self) -> bool:
        return self.token_file.exists()

    def settings(self, redirect_uri: str = "http://localhost/unused"):
        from xero_absolute_backup.config import Settings

        client = self.client()
        if not client.get("client_id"):
            raise ValueError("Add your Xero app's Client ID first.")
        return Settings(
            client_id=client["client_id"],
            client_secret=client.get("client_secret"),
            redirect_uri=redirect_uri,
            scopes=tuple(client.get("scopes") or SCOPES),
            tenant_id=client.get("tenant_id"),
            token_file=self.token_file,
            state_db=self.state_db,
            output_dir=self.output_dir,
            daily_call_floor=25,
        )

    def authorization_url(self, redirect_uri: str) -> str:
        client = self.client()
        if not client.get("client_id"):
            raise ValueError("Add your Xero app's Client ID first.")
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "scope": " ".join(client.get("scopes") or SCOPES),
            "state": state,
        }
        pending = {"redirect_uri": redirect_uri, "created": time.time()}
        if not client.get("client_secret"):
            verifier, challenge = _pkce()
            params.update(code_challenge=challenge, code_challenge_method="S256")
            pending["verifier"] = verifier
        self._pending = {state: pending}
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def complete_authorization(self, code: str | None, state: str | None, error: str | None = None) -> None:
        from xero_absolute_backup.client import XeroApiError, http_form_post

        if error:
            raise ValueError(f"Xero declined the connection ({error}). Try connecting again.")
        pending = self._pending.pop(state or "", None)
        if not pending or not code or time.time() - pending["created"] > 900:
            raise ValueError("The Xero sign-in expired or did not match. Start the connection again.")
        client = self.client()
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "client_id": client["client_id"],
        }
        if "verifier" in pending:
            payload["code_verifier"] = pending["verifier"]
        if client.get("client_secret"):
            payload["client_secret"] = client["client_secret"]
        try:
            token = http_form_post(TOKEN_URL, payload)
        except XeroApiError as exc:
            raise ValueError(
                "Xero rejected the sign-in. Check that the redirect URI in your Xero app "
                f"is exactly {pending['redirect_uri']}."
            ) from exc
        token["expires_at"] = int(time.time()) + int(token.get("expires_in", 1800))
        self._write_private(self.token_file, token)

    def organisations(self) -> list[dict]:
        from xero_absolute_backup.client import TokenStore, XeroClient

        client = XeroClient(self.settings(), TokenStore(self.token_file))
        return [
            {"id": row.get("tenantId"), "name": row.get("tenantName")}
            for row in client.list_connections()
            if row.get("tenantType") == "ORGANISATION"
        ]

    def choose_organisation(self, tenant_id: str) -> None:
        self._write_private(self.client_file, {**self.client(), "tenant_id": tenant_id})

    def disconnect(self) -> None:
        self.token_file.unlink(missing_ok=True)

    # Backup ----------------------------------------------------------------

    def start_backup(self) -> None:
        if self.job.state == "running":
            return
        settings = self.settings()
        self.job = BackupJob(state="running", message="Connecting to Xero…", started=dt.datetime.now())

        def run() -> None:
            from xero_absolute_backup.backup import BackupRunner

            try:
                runner = BackupRunner(settings)
                self.job.message = f"Backing up {runner.connection.get('tenantName', 'the organisation')}…"
                runner.run(sleep_on_rate_limit=False)
                manifest = self.backup_manifest() or {}
                if manifest.get("status") == "complete":
                    self.job.state, self.job.message = "complete", "Backup complete."
                else:
                    self.job.state = "paused"
                    self.job.message = (
                        manifest.get("rate_limit_reason")
                        or "Xero's API limit was reached. Resume later; finished work is kept."
                    )
            except Exception as exc:  # surfaced to the owner, details logged
                traceback.print_exc()
                self.job.state = "failed"
                self.job.message = f"The backup stopped: {exc}. Finished work is kept; resume to continue."
            finally:
                self.job.finished = dt.datetime.now()

        self.job.thread = threading.Thread(target=run, name="xero-backup", daemon=True)
        self.job.thread.start()

    def backup_root(self) -> Path | None:
        configured = self.workspace.xero_backup
        if (configured / "csv" / "accounts.csv").exists():
            return configured
        if self.output_dir.is_dir():
            for child in sorted(self.output_dir.iterdir()):
                if (child / "csv" / "accounts.csv").exists():
                    return child
        return None

    def backup_manifest(self) -> dict | None:
        root = self.backup_root()
        candidates = [root] if root else []
        if self.output_dir.is_dir():
            candidates += [child for child in self.output_dir.iterdir() if child.is_dir()]
        for candidate in candidates:
            path = candidate / "manifests" / "run_summary.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        return None

    def progress(self) -> dict:
        summary: dict = {}
        if self.state_db.exists():
            from xero_absolute_backup.state import StateStore

            store = StateStore(self.state_db)
            try:
                summary = store.get_summary()
            finally:
                store.close()
        return summary

    # General ledger and conversion ----------------------------------------

    def merged_gl(self) -> Path | None:
        root = self.backup_root()
        path = root / "csv" / "general_ledger_merged.csv" if root else None
        return path if path and path.exists() else None

    def merge_gl(self, files: list[Path]) -> dict:
        from .xero_gl import merge_gl_files

        root = self.backup_root()
        if root is None:
            raise ValueError("Finish the Xero backup before adding the general ledger export.")
        output = root / "csv" / "general_ledger_merged.csv"
        return merge_gl_files(files, output)

    def conversion_blocker(self) -> str | None:
        """Why conversion is not allowed, or None when it is safe."""
        entries, errors, _ = loader.load_file(str(self.workspace.ledger_main))
        for entry in entries:
            if isinstance(entry, Transaction) and not (entry.meta or {}).get("xero-je"):
                return (
                    "This ledger already has transactions that did not come from Xero. "
                    "Conversion would replace them, so it is only available in a new workspace."
                )
        if self.root_has_sources():
            return (
                "Accounts are already registered. Convert first in a new workspace, then "
                "register accounts from the converted chart."
            )
        if self.backup_root() is None:
            return "Run the Xero backup first."
        if self.merged_gl() is None:
            return "Add the General Ledger Detail export first."
        return None

    def root_has_sources(self) -> bool:
        from .setup import read_sources

        return bool(read_sources(self.workspace)["accounts"])

    def convert(self) -> dict:
        from .beancount_convert import convert
        from .verify_conversion import verify, write_report

        blocker = self.conversion_blocker()
        if blocker:
            raise ValueError(blocker)
        root = self.backup_root()
        assert root is not None
        stats = convert(self.workspace, root)
        self._open_role_accounts()
        report = verify(root, self.workspace.ledger_root)
        write_report(report, self.workspace.ledger_root / "verify_report.json")
        return {"stats": stats, "report": report}

    def _open_role_accounts(self) -> None:
        """Open the books.toml role accounts that Xero's chart lacks."""
        entries, _errors, _ = loader.load_file(str(self.workspace.ledger_main))
        opened = {e.account for e in entries if isinstance(e, Open)}
        first = min((e.date for e in entries if isinstance(e, Open)), default=dt.date.today())
        chart = self.workspace.chart
        missing = [
            account
            for account in (
                chart.receivable,
                chart.sales_income,
                chart.transfer_clearing,
                chart.uncategorized_expense,
                chart.uncategorized_income,
                "Equity:Opening-Balances",
            )
            if account not in opened
        ]
        if missing:
            with (self.workspace.ledger_root / "accounts.beancount").open("a", encoding="utf-8") as handle:
                handle.write("\n; Accounts used by the books tools (books.toml roles)\n")
                for account in dict.fromkeys(missing):
                    handle.write(f"{first.isoformat()} open {account}  {self.workspace.currency}\n")

    def verify_report(self) -> dict | None:
        path = self.workspace.ledger_root / "verify_report.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def register_sources(self, active_within_days: int = 180) -> list[dict]:
        """Register converted bank and card accounts as statement sources.

        Accounts with activity within `active_within_days` of the end of the
        Xero history become active; the rest are dormant. Imports start the
        day after Xero ends. Xero's closing balances, dated that day, are the
        converted evidence; statements are required after it.
        """
        from .setup import add_account_source, read_sources, slug

        entries, _errors, _ = loader.load_file(str(self.workspace.ledger_main))
        chart = self.workspace.chart
        opened: dict[str, Open] = {e.account: e for e in entries if isinstance(e, Open)}
        last: dict[str, dt.date] = {}
        end = dt.date.min
        for entry in entries:
            if isinstance(entry, Transaction):
                end = max(end, entry.date)
                for posting in entry.postings:
                    last[posting.account] = max(last.get(posting.account, dt.date.min), entry.date)
        if end == dt.date.min:
            raise ValueError("Convert the Xero data before registering accounts.")
        cutover = end + dt.timedelta(days=1)
        existing = {entry.get("account") for entry in read_sources(self.workspace)["accounts"].values()}
        used_keys = set(read_sources(self.workspace)["accounts"])
        registered = []
        for account, open_entry in sorted(opened.items()):
            if not chart.is_statement_account(account) or account in existing or account not in last:
                continue
            key = slug(account.rsplit(":", 1)[-1])
            base, number = key, 2
            while key in used_keys:
                key, number = f"{base}-{number}", number + 1
            used_keys.add(key)
            name = str(open_entry.meta.get("xero-name") or account.rsplit(":", 1)[-1].replace("-", " "))
            lifecycle = "active" if (end - last[account]).days <= active_within_days else "dormant"
            add_account_source(
                self.workspace,
                key=key,
                name=name,
                account=account,
                opened=open_entry.date,
                cutover_date=cutover,
                evidence_required_after=cutover + dt.timedelta(days=1),
                lifecycle=lifecycle,
            )
            registered.append({"key": key, "account": account, "lifecycle": lifecycle})
        return registered
