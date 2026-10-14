"""Teller API client (free developer tier, no KYC).

Auth: mTLS client certificate (downloaded from the Teller dashboard) plus a
per-enrollment access token sent as HTTP Basic username. Stdlib only.

Secrets layout (gitignored):
  secrets/teller/certificate.pem
  secrets/teller/private_key.pem
  secrets/teller/tokens.json   # {"enrollments": [{"name": "...", "access_token": "token_..."}]}

Sign convention: Teller amounts are returned as signed strings. We pass them
through unchanged (money-in positive for depository accounts). If a card
account turns out inverted, set `invert = true` for it in account_map.toml.
"""

from __future__ import annotations

import base64
import json
import ssl
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

from .statements import FeedTransaction, money

API_BASE = "https://api.teller.io"


@dataclass(frozen=True)
class TellerAccount:
    account_id: str
    enrollment_name: str
    name: str
    institution: str
    account_type: str
    last_four: str
    balance: Decimal | None = None


class TellerClient:
    def __init__(self, secrets_dir: Path) -> None:
        self.secrets_dir = secrets_dir
        cert = secrets_dir / "certificate.pem"
        key = secrets_dir / "private_key.pem"
        if not cert.exists() or not key.exists():
            raise FileNotFoundError(
                f"Teller client certificate not found in {secrets_dir}. Download "
                "certificate.pem and private_key.pem from https://teller.io dashboard."
            )
        self.context = ssl.create_default_context()
        self.context.load_cert_chain(certfile=str(cert), keyfile=str(key))

    def _get(self, path: str, access_token: str) -> object:
        credentials = base64.b64encode(f"{access_token}:".encode("ascii")).decode("ascii")
        request = Request(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
        )
        with urlopen(request, timeout=60, context=self.context) as response:
            return json.loads(response.read().decode("utf-8"))

    def enrollments(self) -> list[dict]:
        tokens_path = self.secrets_dir / "tokens.json"
        if not tokens_path.exists():
            raise FileNotFoundError(
                f"{tokens_path} missing. After enrolling a bank via Teller Connect, save "
                'its access token there: {"enrollments": [{"name": "chase", "access_token": "token_..."}]}'
            )
        payload = json.loads(tokens_path.read_text(encoding="utf-8"))
        return payload.get("enrollments", [])

    def list_accounts(self) -> list[TellerAccount]:
        accounts: list[TellerAccount] = []
        for enrollment in self.enrollments():
            token = enrollment["access_token"]
            for item in self._get("/accounts", token):
                balance = None
                try:
                    detail = self._get(f"/accounts/{item['id']}/balances", token)
                    balance = money(detail.get("available") or detail.get("ledger"))
                except Exception:
                    pass
                accounts.append(
                    TellerAccount(
                        account_id=item["id"],
                        enrollment_name=enrollment.get("name", ""),
                        name=item.get("name", ""),
                        institution=item.get("institution", {}).get("name", ""),
                        account_type=item.get("type", ""),
                        last_four=item.get("last_four", ""),
                        balance=balance,
                    )
                )
        return accounts

    def transactions(self, account_id: str, access_token: str, count: int = 500) -> list[dict]:
        return self._get(f"/accounts/{account_id}/transactions?count={count}", access_token)

    def fetch_all(self, raw_dir: Path, today: str) -> dict[str, list[FeedTransaction]]:
        """Fetch posted transactions for every enrolled account.

        Returns {teller_account_id: [FeedTransaction, ...]} and writes the raw
        API responses to raw_dir/<today>/ for audit/replay.
        """
        out_dir = raw_dir / today
        out_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, list[FeedTransaction]] = {}
        for enrollment in self.enrollments():
            token = enrollment["access_token"]
            accounts = self._get("/accounts", token)
            (out_dir / f"accounts-{enrollment.get('name', 'enrollment')}.json").write_text(
                json.dumps(accounts, indent=2), encoding="utf-8"
            )
            for item in accounts:
                account_id = item["id"]
                raw_txns = self.transactions(account_id, token)
                (out_dir / f"transactions-{account_id}.json").write_text(
                    json.dumps(raw_txns, indent=2), encoding="utf-8"
                )
                feed: list[FeedTransaction] = []
                for txn in raw_txns:
                    pending = txn.get("status") == "pending"
                    feed.append(
                        FeedTransaction(
                            txn_id=txn["id"],
                            date=txn.get("date", ""),
                            amount=money(txn.get("amount")),
                            description=(txn.get("description") or "").strip(),
                            source="teller",
                            account_key=account_id,
                            pending=pending,
                            raw=txn,
                        )
                    )
                result[account_id] = feed
        return result
