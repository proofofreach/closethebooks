from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


DEFAULT_SCOPES = (
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


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(slots=True)
class Settings:
    client_id: str
    client_secret: str | None
    redirect_uri: str
    scopes: tuple[str, ...]
    tenant_id: str | None
    token_file: Path
    state_db: Path
    output_dir: Path
    daily_call_floor: int
    auth_timeout_seconds: int = 300

    @property
    def auth_authorize_url(self) -> str:
        return "https://login.xero.com/identity/connect/authorize"

    @property
    def auth_token_url(self) -> str:
        return "https://identity.xero.com/connect/token"

    @property
    def api_base_url(self) -> str:
        return "https://api.xero.com/api.xro/2.0"

    @property
    def connections_url(self) -> str:
        return "https://api.xero.com/connections"


def load_settings(base_dir: Path | None = None) -> Settings:
    root = base_dir or Path.cwd()
    load_dotenv(root / ".env")
    client_id = os.environ.get("XERO_CLIENT_ID", "").strip()
    if not client_id:
        raise ValueError("XERO_CLIENT_ID is required")

    scopes_value = os.environ.get("XERO_SCOPES", "").strip()
    scopes = tuple(scopes_value.split()) if scopes_value else DEFAULT_SCOPES

    client_secret = os.environ.get("XERO_CLIENT_SECRET", "").strip() or None
    redirect_uri = os.environ.get("XERO_REDIRECT_URI", "http://localhost:8766/callback").strip()
    tenant_id = os.environ.get("XERO_TENANT_ID", "").strip() or None
    daily_call_floor = int(os.environ.get("XERO_DAILY_CALL_FLOOR", "25").strip())
    token_file = root / "tokens.json"
    state_db = root / "xero_sync_state.db"
    output_dir = root / "backup_output"
    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=scopes,
        tenant_id=tenant_id,
        token_file=token_file,
        state_db=state_db,
        output_dir=output_dir,
        daily_call_floor=daily_call_floor,
    )
