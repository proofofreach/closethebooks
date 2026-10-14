"""Local web app: book health, imports, accounts, invoices, and questions.

The app reads the ledger and writes only to the inbox, the proposal file,
statement evidence, invoice documents, and workspace configuration. Posting
transactions stays a reviewed step. Fava is mounted at /reports for detail.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from beancount import loader
from beancount.core.data import Transaction
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
    url_for,
)
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from .. import assistant
from ..accounts import AccountSource, load_account_sources
from ..workspace import PRODUCT_NAME, Workspace, WorkspaceError, load_workspace

MAX_STATEMENT_BYTES = 20 * 1024 * 1024
DEFAULT_STATEMENT_EXTENSIONS = {".csv", ".mt940", ".ofx", ".qbo", ".qfx", ".sta", ".swift"}
ALL_STATEMENT_EXTENSIONS = DEFAULT_STATEMENT_EXTENSIONS | {".pdf"}
PDF_PRESETS = {"quickbooks-checking": "QuickBooks Checking PDF"}


def save_statement_upload(
    upload: FileStorage,
    inbox: Path,
    account_key: str,
    *,
    allowed_extensions: set[str] | None = None,
    max_bytes: int = MAX_STATEMENT_BYTES,
) -> Path:
    """Save an uploaded statement under a safe, account-identifying filename."""
    filename = secure_filename(upload.filename or "")
    suffix = Path(filename).suffix.lower()
    allowed = DEFAULT_STATEMENT_EXTENSIONS if allowed_extensions is None else allowed_extensions
    if not filename or suffix not in allowed:
        formats = ", ".join(sorted(ext.lstrip(".").upper() for ext in allowed))
        raise ValueError(f"That file type is not accepted for this account. Use {formats}.")
    stem = Path(filename).stem[:120] or "statement"
    inbox.mkdir(parents=True, exist_ok=True)
    for number in range(1, 10_000):
        qualifier = "" if number == 1 else f"-{number}"
        destination = inbox / f"{account_key}--{stem}{qualifier}{suffix}"
        try:
            with destination.open("xb") as target:
                total = 0
                while chunk := upload.stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Statement files must be 20 MB or smaller.")
                    target.write(chunk)
            return destination
        except FileExistsError:
            continue
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    raise ValueError("Too many files with this name are already in the inbox.")


def statement_extensions(source: AccountSource) -> set[str]:
    if not source.statement_extensions:
        return set(DEFAULT_STATEMENT_EXTENSIONS)
    normalized = {
        value if value.startswith(".") else f".{value}"
        for value in (str(ext).lower() for ext in source.statement_extensions)
    }
    return normalized & ALL_STATEMENT_EXTENSIONS


@dataclass
class LedgerSnapshot:
    entries: list
    errors: list
    options: dict
    signature: tuple


class State:
    """The workspace and a cached ledger load, refreshed when files change."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._lock = threading.Lock()
        self._snapshot: LedgerSnapshot | None = None

    def reload_workspace(self) -> None:
        self.workspace = load_workspace(self.workspace.root)

    @staticmethod
    def _mtimes(paths) -> tuple:
        result = []
        for path in paths:
            try:
                result.append((str(path), Path(path).stat().st_mtime_ns))
            except OSError:
                result.append((str(path), None))
        return tuple(result)

    def ledger(self) -> LedgerSnapshot:
        with self._lock:
            snapshot = self._snapshot
            if snapshot is not None and self._mtimes(path for path, _ in snapshot.signature) == snapshot.signature:
                return snapshot
            entries, errors, options = loader.load_file(str(self.workspace.ledger_main))
            files = list(dict.fromkeys([str(self.workspace.ledger_main), *options.get("include", [])]))
            snapshot = LedgerSnapshot(entries, errors, options, self._mtimes(files))
            self._snapshot = snapshot
            return snapshot

    def sources(self) -> dict[str, AccountSource]:
        if not self.workspace.sources_file.exists():
            return {}
        return load_account_sources(self.workspace.sources_file, self.workspace.currency)

    def revisions(self) -> list[dict]:
        path = self.workspace.inbox / "plaid" / "current" / "revisions.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def inbox_files(workspace: Workspace, sources: dict[str, AccountSource]) -> list[dict]:
    files = []
    if not workspace.inbox.is_dir():
        return files
    for path in sorted(workspace.inbox.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_file() or path.name.startswith("."):
            continue
        key, separator, _rest = path.name.partition("--")
        source = sources.get(key) if separator else None
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "key": source.key if source else None,
                "account": source.name if source else None,
                "active": bool(source and source.lifecycle == "active"),
                "size": stat.st_size,
                "added": dt.datetime.fromtimestamp(stat.st_mtime),
            }
        )
    return files


def read_proposals(workspace: Workspace) -> dict | None:
    from beancount.core.data import Balance
    from beancount.parser import parser

    path = workspace.proposals
    if not path.exists():
        return None
    entries, errors, _ = parser.parse_file(str(path))
    transactions = []
    balances = []
    for entry in entries:
        if isinstance(entry, Transaction):
            source_posting = next((p for p in entry.postings if p.meta and "stmt-id" in p.meta), entry.postings[0])
            other = next((p for p in entry.postings if p is not source_posting), None)
            confidence = entry.meta.get("category-confidence")
            transactions.append(
                {
                    "date": entry.date,
                    "description": entry.payee or entry.narration,
                    "account": source_posting.account,
                    "amount": source_posting.units.number if source_posting.units else None,
                    "currency": source_posting.units.currency if source_posting.units else "",
                    "category": other.account if other else "",
                    "suggested": "category-suggested" in entry.meta,
                    "confidence": float(confidence) if confidence else None,
                    "evidence": entry.meta.get("category-evidence", ""),
                    "flag": entry.flag,
                }
            )
        elif isinstance(entry, Balance):
            balances.append(
                {"date": entry.date, "account": entry.account, "amount": entry.amount.number, "currency": entry.amount.currency}
            )
    return {
        "transactions": transactions,
        "balances": balances,
        "errors": len(errors),
        "updated": dt.datetime.fromtimestamp(path.stat().st_mtime),
        "path": workspace.relative(path),
    }


def invoice_rows(workspace: Workspace, entries: list) -> list[dict]:
    from ..invoices import existing_invoices

    chart = workspace.chart
    outstanding: dict[str, Decimal] = {}
    recorded: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        number = str(entry.meta.get("invoice", "")) if entry.meta else ""
        if not number and entry.narration and entry.narration.startswith("Invoice "):
            number = entry.narration.removeprefix("Invoice ").strip()
        if not number:
            continue
        for posting in entry.postings:
            if posting.account == chart.receivable and posting.units is not None:
                outstanding[number] = outstanding.get(number, Decimal()) + posting.units.number
                if posting.units.number > 0 and number not in recorded:
                    recorded[number] = {
                        "client": entry.payee or "",
                        "amount": posting.units.number,
                        "date": entry.date,
                        "due": entry.meta.get("due"),
                    }
    rows = []
    documents = {invoice["number"]: invoice for invoice in existing_invoices(workspace)}
    for number in sorted(set(documents) | set(recorded), reverse=True):
        record = recorded.get(number)
        document = documents.get(number)
        balance = outstanding.get(number, Decimal())
        if record is None:
            state, label = "draft", "Not recorded"
        elif balance > 0:
            due = dt.date.fromisoformat(record["due"]) if record.get("due") else None
            if due and due < dt.date.today():
                state, label = "overdue", f"Overdue since {due:%b %-d}"
            else:
                state, label = "open", "Open"
        else:
            state, label = "paid", "Paid"
        rows.append(
            {
                "number": number,
                "client": record["client"] if record else "",
                "date": record["date"] if record else dt.date.fromisoformat(document["date"]),
                "amount": record["amount"] if record else None,
                "balance": balance,
                "state": state,
                "label": label,
                "document": workspace.relative(document["path"]) if document else None,
                "document_url": (
                    document["path"].relative_to(workspace.documents).as_posix()
                    if document and document["path"].is_relative_to(workspace.documents)
                    else None
                ),
            }
        )
    return sorted(rows, key=lambda row: (row["date"], row["number"]), reverse=True)


def _money(value, currency: str | None = None, signed: bool = False) -> str:
    if value is None:
        return "—"
    number = Decimal(value)
    text = f"{abs(number):,.2f}"
    if number < 0:
        text = f"−{text}"
    elif signed and number > 0:
        text = f"+{text}"
    return f"{text} {currency}" if currency else text


def _date(value, style: str = "medium") -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        value = dt.date.fromisoformat(value)
    if style == "short":
        return f"{value:%b %-d}"
    return f"{value:%b %-d, %Y}"


def create_app(workspace: Workspace, *, mount_reports: bool = True) -> Flask:
    package = Path(__file__).parent
    app = Flask(
        __name__,
        template_folder=str(package / "templates"),
        static_folder=str(package / "static"),
        static_url_path="/static",
    )
    app.secret_key = secrets.token_bytes(32)
    app.config["MAX_CONTENT_LENGTH"] = MAX_STATEMENT_BYTES + 256_000
    state = State(workspace)
    app.extensions["books_state"] = state
    token = secrets.token_urlsafe(24)

    app.jinja_env.filters["money"] = _money
    app.jinja_env.filters["day"] = _date
    app.jinja_env.globals.update(product_name=PRODUCT_NAME, csrf_token=token)

    @app.before_request
    def guard():
        try:
            remote = ipaddress.ip_address(request.remote_addr or "")
        except ValueError:
            remote = None
        if remote is None or not remote.is_loopback:
            abort(403)
        if request.method == "POST":
            supplied = request.headers.get("X-Books-Token") or request.form.get("csrf")
            if not supplied or not secrets.compare_digest(supplied, token):
                abort(403)
        g.workspace = state.workspace

    @app.context_processor
    def inject():
        return {"workspace": state.workspace, "company": state.workspace.company, "nav": request.endpoint}

    @app.errorhandler(WorkspaceError)
    def workspace_error(exc):
        return render_template("error.html", title="Workspace problem", message=str(exc)), 500

    @app.errorhandler(403)
    def forbidden(_exc):
        message = "This page only accepts requests from this computer's own browser. Reload the page and try again."
        if request.accept_mimetypes.best == "application/json" or request.headers.get("X-Books-Token") is not None:
            return jsonify({"error": message}), 403
        return render_template("error.html", title="Request refused", message=message), 403

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify({"error": "Statement files must be 20 MB or smaller."}), 413

    @app.get("/")
    def overview():
        from ..overview import build_overview

        snapshot = state.ledger()
        data = build_overview(
            state.workspace,
            snapshot.entries,
            snapshot.errors,
            state.sources(),
            period_key=request.args.get("period", "ytd"),
            plaid_revisions=state.revisions(),
        )
        from .chart import cash_chart

        return render_template(
            "overview.html", d=data, chart=cash_chart(data["cash_series"]), title="Overview"
        )

    @app.post("/upload")
    def upload():
        sources = state.sources()
        key = (request.form.get("account_key") or "").strip()
        source = sources.get(key)
        wants_json = request.headers.get("X-Books-Token") is not None
        try:
            if source is None:
                raise ValueError("Choose an account for this statement.")
            if source.lifecycle != "active":
                raise ValueError(f"{source.name} is {source.lifecycle}. Activate it before importing.")
            upload_file = request.files.get("statement")
            if upload_file is None or not upload_file.filename:
                raise ValueError("Choose a statement file to upload.")
            destination = save_statement_upload(
                upload_file,
                state.workspace.inbox,
                key,
                allowed_extensions=statement_extensions(source),
            )
        except ValueError as exc:
            if wants_json:
                return jsonify({"error": str(exc)}), 400
            flash(str(exc), "error")
            return redirect(request.form.get("next") or url_for("import_page"))
        except OSError:
            message = "The statement could not be saved to the inbox. Check that the folder is writable."
            if wants_json:
                return jsonify({"error": message}), 500
            flash(message, "error")
            return redirect(url_for("import_page"))
        if wants_json:
            return jsonify({"filename": destination.name, "account": source.name})
        flash(f"Saved {destination.name} to the inbox.", "success")
        return redirect(request.form.get("next") or url_for("import_page"))

    @app.get("/import")
    def import_page():
        sources = state.sources()
        return render_template(
            "import.html",
            title="Import",
            files=inbox_files(state.workspace, sources),
            proposals=read_proposals(state.workspace),
            sources=[s for s in sources.values() if s.lifecycle == "active"],
            extensions={key: ",".join(sorted(statement_extensions(s))) for key, s in sources.items()},
        )

    @app.post("/import/prepare")
    def prepare():
        from ..propose import run_propose

        sources = state.sources()
        files = inbox_files(state.workspace, sources)
        statements = [
            (item["key"], state.workspace.inbox / item["name"], None)
            for item in files
            if item["key"] and item["active"]
        ]
        skipped = [item["name"] for item in files if not (item["key"] and item["active"])]
        try:
            result = run_propose(state.workspace, statements)
        except (ValueError, RuntimeError, OSError) as exc:
            flash(f"Proposals were not prepared. {exc}", "error")
            return redirect(url_for("import_page"))
        stats = result["stats"]
        message = (
            f"Prepared {stats.get('proposed', 0)} new transaction"
            f"{'s' if stats.get('proposed', 0) != 1 else ''}"
        )
        if stats.get("duplicate"):
            message += f"; {stats['duplicate']} already in the ledger"
        if stats.get("balance_assertions"):
            message += f"; {stats['balance_assertions']} statement balance{'s' if stats['balance_assertions'] != 1 else ''} to verify"
        flash(message + ".", "success")
        if skipped:
            flash(
                f"Skipped {len(skipped)} file{'s' if len(skipped) != 1 else ''} without an active account: "
                + ", ".join(skipped),
                "warning",
            )
        return redirect(url_for("import_page"))

    @app.get("/accounts")
    def accounts():
        from ..overview import build_overview

        snapshot = state.ledger()
        data = build_overview(state.workspace, snapshot.entries, snapshot.errors, state.sources())
        return render_template("accounts.html", title="Accounts", d=data, sources=state.sources())

    def _account_form(key: str | None):
        from ..setup import STATEMENT_FORMATS, add_account_source, read_sources, slug

        existing = read_sources(state.workspace)["accounts"].get(key) if key else None
        if key and existing is None:
            abort(404)
        errors: list[str] = []
        values = dict(existing or {})
        if key:
            values["key"] = key
        if request.method == "POST":
            form = request.form
            kind = form.get("kind", "bank")
            name = form.get("name", "").strip()
            account = form.get("account", "").strip()
            if not account and name:
                prefix = (
                    state.workspace.chart.card_prefixes[0]
                    if kind == "card"
                    else state.workspace.chart.cash_prefixes[0]
                )
                account = prefix + "-".join(part.capitalize() for part in slug(name).split("-"))
            new_key = key or form.get("key", "").strip() or slug(name)
            values = {
                "key": new_key,
                "name": name,
                "account": account,
                "lifecycle": form.get("lifecycle", "active"),
                "statement_extensions": form.getlist("formats"),
                "statement_preset": form.get("preset") or None,
                "expected_cadence_days": form.get("cadence", "35"),
                "overdue_after_days": form.get("overdue", "90"),
                "kind": kind,
                "opened": form.get("opened", ""),
            }
            try:
                opened = dt.date.fromisoformat(values["opened"]) if values["opened"] else None
                add_account_source(
                    state.workspace,
                    key=new_key,
                    name=name,
                    account=account,
                    opened=opened,
                    cutover_date=opened if not key else None,
                    lifecycle=values["lifecycle"],
                    ingestion=existing.get("ingestion", "statement") if existing else "statement",
                    feed_account_id=existing.get("feed_account_id") if existing else None,
                    statement_extensions=values["statement_extensions"],
                    statement_preset=values["statement_preset"],
                    statement_balance_sign=existing.get("statement_balance_sign") if existing else None,
                    expected_cadence_days=int(values["expected_cadence_days"]),
                    overdue_after_days=int(values["overdue_after_days"]),
                    replace=bool(key),
                )
            except ValueError as exc:
                errors.append(str(exc).replace("invalid literal for int() with base 10", "Enter whole days"))
            else:
                flash(f"Saved {name or account}.", "success")
                return redirect(url_for("accounts"))
        return render_template(
            "account_form.html",
            title="Edit account" if key else "Add account",
            editing=bool(key),
            values=values,
            errors=errors,
            formats=STATEMENT_FORMATS,
            presets=PDF_PRESETS,
        )

    @app.route("/accounts/new", methods=["GET", "POST"])
    def account_new():
        return _account_form(None)

    @app.route("/accounts/<key>", methods=["GET", "POST"])
    def account_edit(key: str):
        return _account_form(key)

    @app.get("/invoices")
    def invoices():
        snapshot = state.ledger()
        rows = invoice_rows(state.workspace, snapshot.entries)
        open_total = sum((row["balance"] for row in rows if row["balance"] > 0), Decimal())
        return render_template("invoices.html", title="Invoices", rows=rows, open_total=open_total)

    @app.route("/invoices/new", methods=["GET", "POST"])
    def invoice_new():
        from ..invoices import create_invoice, next_invoice_number

        workspace = state.workspace
        errors: list[str] = []
        form = request.form
        if request.method == "POST":
            items = list(zip(form.getlist("item"), form.getlist("amount")))
            try:
                issue = dt.date.fromisoformat(form.get("date") or dt.date.today().isoformat())
                result = create_invoice(
                    workspace,
                    client=form.get("client", ""),
                    client_address=form.get("address", "").splitlines(),
                    items=items,
                    issue_date=issue,
                    due_days=int(form.get("due_days") or workspace.invoicing.due_days),
                    notes=form.get("notes"),
                )
            except ValueError as exc:
                errors.append(str(exc))
            else:
                return render_template(
                    "invoice_created.html",
                    title=f"Invoice {result.invoice.number}",
                    result=result,
                    document_url=result.document.relative_to(workspace.documents).as_posix(),
                    year_file=f"{workspace.relative(workspace.ledger_root)}/years/{result.invoice.issue_date.year}.beancount",
                )
        today = dt.date.today()
        default_notes = "\n\n".join(
            part for part in (workspace.invoicing.payment_instructions, workspace.invoicing.notes) if part
        )
        return render_template(
            "invoice_form.html",
            title="New invoice",
            errors=errors,
            form=form,
            today=today,
            next_number=next_invoice_number(workspace, today),
            default_notes=default_notes,
        )

    @app.get("/r/<path:rest>")
    def report_link(rest: str):
        """Deep link into Fava without knowing its per-ledger URL slug."""
        from fava.util import slugify

        snapshot = state.ledger()
        slug = slugify(snapshot.options.get("title") or "") or slugify(str(state.workspace.ledger_main))
        query = request.query_string.decode()
        return redirect(f"/reports/{slug}/{rest}" + (f"?{query}" if query else ""))

    @app.get("/documents/<path:filename>")
    def document(filename: str):
        return send_from_directory(state.workspace.documents, filename)

    @app.get("/ask")
    def ask():
        return render_template("ask.html", title="Ask", engines=assistant.available_engines(state.workspace))

    @app.post("/ask")
    def ask_stream():
        payload = request.get_json(silent=True) or {}
        question = str(payload.get("question") or "").strip()
        if not question:
            return jsonify({"error": "Type a question first."}), 400
        history = payload.get("history") if isinstance(payload.get("history"), list) else []
        workspace = state.workspace
        return Response(
            stream_with_context(assistant.answer(workspace, question[:4000], history)),
            mimetype="application/x-ndjson",
        )

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        from ..setup import update_company

        errors: list[str] = []
        workspace = state.workspace
        if request.method == "POST":
            form = request.form
            try:
                update_company(
                    workspace,
                    {
                        "name": form.get("name", "").strip(),
                        "legal_name": form.get("legal_name", "").strip(),
                        "entity": form.get("entity", "").strip(),
                        "email": form.get("email", "").strip(),
                        "address": [line.strip() for line in form.get("address", "").splitlines() if line.strip()],
                    },
                    {
                        "prefix": form.get("prefix", "").strip(),
                        "due_days": int(form.get("due_days") or 14),
                        "payment_instructions": form.get("payment_instructions", "").strip(),
                        "notes": form.get("notes", "").strip(),
                    },
                )
            except (ValueError, WorkspaceError) as exc:
                errors.append(str(exc).replace("books.toml: ", ""))
            else:
                state.reload_workspace()
                flash("Settings saved.", "success")
                return redirect(url_for("settings"))
        return render_template("settings.html", title="Settings", errors=errors)

    from ..xero_migration import XeroMigration

    migration = XeroMigration(workspace)
    app.extensions["xero_migration"] = migration

    def xero_redirect_uri() -> str:
        port = request.host.rsplit(":", 1)[-1] if ":" in request.host else "80"
        return f"http://localhost:{port}/migrate/callback"

    def migration_view() -> dict:
        organisations: list[dict] = []
        organisation_error = None
        if migration.connected():
            try:
                organisations = migration.organisations()
            except Exception as exc:  # network or expired token
                organisation_error = f"Could not list organisations: {exc}"
        return {
            "client": migration.client(),
            "connected": migration.connected(),
            "organisations": organisations,
            "organisation_error": organisation_error,
            "redirect_uri": xero_redirect_uri(),
            "job": migration.job,
            "progress": migration.progress(),
            "manifest": migration.backup_manifest(),
            "backup_root": migration.backup_root(),
            "merged_gl": migration.merged_gl(),
            "blocker": migration.conversion_blocker(),
            "report": migration.verify_report(),
            "sources": state.sources(),
        }

    @app.get("/migrate")
    def migrate():
        return render_template("migrate.html", title="Move from Xero", m=migration_view())

    @app.get("/migrate/status")
    def migrate_status():
        job = migration.job
        return jsonify({"state": job.state, "message": job.message, "progress": migration.progress()})

    def migrate_step(action):
        try:
            message = action()
        except (ValueError, RuntimeError, OSError) as exc:
            flash(str(exc), "error")
        else:
            if message:
                flash(message, "success")
        return redirect(url_for("migrate"))

    @app.post("/migrate/client")
    def migrate_client():
        return migrate_step(
            lambda: migration.save_client(request.form.get("client_id", ""), request.form.get("client_secret", ""))
            or "Saved the Xero app details."
        )

    @app.post("/migrate/connect")
    def migrate_connect():
        try:
            return redirect(migration.authorization_url(xero_redirect_uri()))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("migrate"))

    @app.get("/migrate/callback")
    def migrate_callback():
        return migrate_step(
            lambda: migration.complete_authorization(
                request.args.get("code"), request.args.get("state"), request.args.get("error")
            )
            or "Connected to Xero."
        )

    @app.post("/migrate/organisation")
    def migrate_organisation():
        return migrate_step(
            lambda: migration.choose_organisation(request.form.get("tenant_id", "")) or "Organisation selected."
        )

    @app.post("/migrate/disconnect")
    def migrate_disconnect():
        return migrate_step(lambda: migration.disconnect() or "Disconnected from Xero. The backup stays.")

    @app.post("/migrate/backup")
    def migrate_backup():
        return migrate_step(lambda: migration.start_backup() or "Backup started. You can leave this page open.")

    @app.post("/migrate/gl")
    def migrate_gl():
        def store():
            uploads = [f for f in request.files.getlist("gl") if f and f.filename]
            if not uploads:
                raise ValueError("Choose the General Ledger Detail export (.xlsx).")
            folder = migration.output_dir / "uploads"
            folder.mkdir(parents=True, exist_ok=True)
            saved = []
            for upload_file in uploads:
                name = secure_filename(upload_file.filename)
                if not name.lower().endswith(".xlsx"):
                    raise ValueError("The general ledger export must be an Excel .xlsx file.")
                destination = folder / name
                upload_file.save(destination)
                saved.append(destination)
            stats = migration.merge_gl(saved)
            return f"Merged {stats['output_rows']:,} general ledger rows from {len(saved)} file{'s' if len(saved) != 1 else ''}."

        return migrate_step(store)

    @app.post("/migrate/convert")
    def migrate_convert():
        def run():
            result = migration.convert()
            status = result["report"].get("status")
            if status != "verified":
                raise RuntimeError(
                    "The ledger was built but does not match Xero's general ledger yet. "
                    "Review the verification details below before using it."
                )
            stats = result["stats"]
            return (
                f"Converted {stats['groups']:,} transactions and verified them against Xero to the cent. "
                f"{stats['documents_total']} attachments filed."
            )

        return migrate_step(run)

    @app.post("/migrate/register")
    def migrate_register():
        def run():
            registered = migration.register_sources()
            active = sum(1 for item in registered if item["lifecycle"] == "active")
            return (
                f"Registered {len(registered)} bank and card accounts ({active} active). "
                "Review them on the Accounts page."
            )

        return migrate_step(run)

    if mount_reports:
        from fava.application import create_app as create_fava
        from werkzeug.middleware.dispatcher import DispatcherMiddleware

        fava = create_fava([str(workspace.ledger_main)], load=True, read_only=True)
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/reports": fava.wsgi_app})  # type: ignore[method-assign]
    return app


def serve(workspace: Workspace, *, host: str = "127.0.0.1", port: int = 5170, open_browser: bool = True) -> None:
    from werkzeug.serving import run_simple

    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError
    except ValueError:
        if host != "localhost":
            raise ValueError("The app serves local files and only listens on 127.0.0.1 or localhost.")
    app = create_app(workspace)
    url = f"http://{host}:{port}/"
    print(f"{PRODUCT_NAME} for {workspace.company.name}: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    run_simple(host, port, app, threaded=True, use_reloader=False)
