"""Plaid bank-data adapter with revision-aware transaction sync."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .statements import FeedTransaction, money

API_BASES = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


class PlaidApiError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def client_from_environment(environment: dict[str, str] | None = None) -> "PlaidClient":
    values = os.environ if environment is None else environment
    client_id = values.get("PLAID_CLIENT_ID", "").strip()
    secret = values.get("PLAID_SECRET", "").strip()
    if not client_id or not secret:
        raise RuntimeError(
            "Set PLAID_CLIENT_ID and PLAID_SECRET from your Plaid dashboard."
        )
    return PlaidClient(
        client_id,
        secret,
        environment=values.get("PLAID_ENV", "sandbox").strip() or "sandbox",
    )


@dataclass(frozen=True)
class PlaidSync:
    added: list[dict]
    modified: list[dict]
    removed: list[dict]
    cursor: str


class PlaidSnapshot:
    """Materialized Plaid transactions backed by immutable raw sync events."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.current_file = root / "current" / "transactions.json"
        self.accounts_file = root / "current" / "accounts.json"
        self.revisions_file = root / "current" / "revisions.json"

    def transactions(self) -> list[dict]:
        if not self.current_file.exists():
            return []
        payload = json.loads(self.current_file.read_text(encoding="utf-8"))
        return [payload[key] for key in sorted(payload)]

    def accounts(self) -> list[dict]:
        if not self.accounts_file.exists():
            return []
        payload = json.loads(self.accounts_file.read_text(encoding="utf-8"))
        return [payload[key] for key in sorted(payload)]

    def record_accounts(
        self,
        item_id: str,
        accounts: list[dict],
        fetched_on: str,
    ) -> None:
        raw_dir = self.root / "raw" / fetched_on
        raw_dir.mkdir(parents=True, exist_ok=True)
        safe_item = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in item_id
        )
        number = 1
        while True:
            raw_file = raw_dir / f"accounts-{safe_item}-{number}.json"
            try:
                with raw_file.open("x", encoding="utf-8") as handle:
                    json.dump(accounts, handle, indent=2, sort_keys=True)
                break
            except FileExistsError:
                number += 1
        current = {row["account_id"]: row for row in self.accounts()}
        current.update({row["account_id"]: row for row in accounts})
        self.accounts_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.accounts_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(current, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.accounts_file)

    def apply(self, item_id: str, sync: PlaidSync, fetched_on: str) -> dict[str, int]:
        raw_dir = self.root / "raw" / fetched_on
        raw_dir.mkdir(parents=True, exist_ok=True)
        safe_item = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in item_id
        )
        number = 1
        while True:
            raw_file = raw_dir / f"sync-{safe_item}-{number}.json"
            try:
                with raw_file.open("x", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "item_id": item_id,
                            "cursor": sync.cursor,
                            "added": sync.added,
                            "modified": sync.modified,
                            "removed": sync.removed,
                        },
                        handle,
                        indent=2,
                        sort_keys=True,
                    )
                break
            except FileExistsError:
                number += 1

        state = {
            row["transaction_id"]: row for row in self.transactions()
        }
        for row in sync.added:
            state[row["transaction_id"]] = row
        for row in sync.modified:
            state[row["transaction_id"]] = row
        for row in sync.removed:
            state.pop(row["transaction_id"], None)

        self.current_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.current_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.current_file)

        revisions = self.pending_revisions()
        known = {
            (revision["kind"], revision["transaction_id"], revision["cursor"])
            for revision in revisions
        }
        for kind, rows in (("modified", sync.modified), ("removed", sync.removed)):
            for row in rows:
                key = (kind, row["transaction_id"], sync.cursor)
                if key not in known:
                    revisions.append(
                        {
                            "kind": kind,
                            "transaction_id": row["transaction_id"],
                            "cursor": sync.cursor,
                            "fetched_on": fetched_on,
                            "item_id": item_id,
                            "source": row,
                        }
                    )
        self._write_revisions(revisions)
        return {
            "added": len(sync.added),
            "modified": len(sync.modified),
            "removed": len(sync.removed),
            "current": len(state),
        }

    def pending_revisions(self) -> list[dict]:
        if not self.revisions_file.exists():
            return []
        return json.loads(self.revisions_file.read_text(encoding="utf-8"))

    def _write_revisions(self, revisions: list[dict]) -> None:
        self.revisions_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.revisions_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                sorted(revisions, key=lambda row: (row["transaction_id"], row["kind"])),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.revisions_file)

    def resolve_revision(self, transaction_id: str) -> int:
        revisions = self.pending_revisions()
        remaining = [
            revision
            for revision in revisions
            if revision["transaction_id"] != transaction_id
        ]
        self._write_revisions(remaining)
        return len(revisions) - len(remaining)


class PlaidItemStore:
    """Local secret store for long-lived Plaid Item access tokens and cursors."""

    def __init__(self, secrets_dir: Path) -> None:
        self.path = secrets_dir / "items.json"

    def items(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8")).get("items", [])

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"items": items}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)

    def upsert(self, item: dict) -> None:
        if not item.get("item_id") or not item.get("access_token"):
            raise ValueError("Plaid Items require item_id and access_token.")
        items = self.items()
        for index, existing in enumerate(items):
            if existing["item_id"] == item["item_id"]:
                replacement = {**existing, **item}
                if item.get("cursor") is None and existing.get("cursor"):
                    replacement["cursor"] = existing["cursor"]
                items[index] = replacement
                break
        else:
            items.append({"cursor": None, **item})
        self._write(items)

    def update_cursor(self, item_id: str, cursor: str) -> None:
        items = self.items()
        for item in items:
            if item["item_id"] == item_id:
                item["cursor"] = cursor
                self._write(items)
                return
        raise KeyError(f"Unknown Plaid Item: {item_id}")


class PlaidClient:
    def __init__(
        self,
        client_id: str,
        secret: str,
        *,
        environment: str = "sandbox",
        post: Callable[[str, dict], dict] | None = None,
    ) -> None:
        if environment not in API_BASES:
            raise ValueError(f"Unknown Plaid environment: {environment}")
        self.client_id = client_id
        self.secret = secret
        self.environment = environment
        self._post = post

    def _request(self, path: str, payload: dict) -> dict:
        body = {"client_id": self.client_id, "secret": self.secret, **payload}
        if self._post is not None:
            return self._post(path, body)
        request = Request(
            f"{API_BASES[self.environment]}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                error = {}
            raise PlaidApiError(
                error.get("error_code", f"HTTP_{exc.code}"),
                error.get("error_message", "Plaid request failed."),
            ) from exc

    def sync_transactions(
        self,
        access_token: str,
        cursor: str | None = None,
    ) -> PlaidSync:
        for attempt in range(3):
            added: list[dict] = []
            modified: list[dict] = []
            removed: list[dict] = []
            next_cursor = cursor
            try:
                while True:
                    payload = {"access_token": access_token, "count": 500}
                    if next_cursor:
                        payload["cursor"] = next_cursor
                    page = self._request("/transactions/sync", payload)
                    added.extend(page.get("added", []))
                    modified.extend(page.get("modified", []))
                    removed.extend(page.get("removed", []))
                    next_cursor = page["next_cursor"]
                    if not page.get("has_more"):
                        return PlaidSync(added, modified, removed, next_cursor)
            except PlaidApiError as exc:
                if (
                    exc.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
                    or attempt == 2
                ):
                    raise
        raise RuntimeError("Plaid transaction sync could not stabilize.")

    def create_link_token(
        self,
        redirect_uri: str | None = None,
        *,
        client_name: str = "Books",
        client_user_id: str = "books-owner",
        country_code: str = "US",
    ) -> str:
        payload = {
            "client_name": client_name[:30],
            "country_codes": [country_code],
            "language": "en",
            "products": ["transactions"],
            "user": {"client_user_id": client_user_id},
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        return self._request("/link/token/create", payload)["link_token"]

    def exchange_public_token(self, public_token: str) -> dict:
        return self._request(
            "/item/public_token/exchange",
            {"public_token": public_token},
        )

    def item(self, access_token: str) -> dict:
        return self._request("/item/get", {"access_token": access_token})["item"]

    def institution(self, institution_id: str) -> dict:
        return self._request(
            "/institutions/get_by_id",
            {"institution_id": institution_id, "country_codes": ["US"]},
        )["institution"]

    def accounts(self, access_token: str) -> list[dict]:
        return self._request("/accounts/get", {"access_token": access_token})[
            "accounts"
        ]


def complete_connection(
    client: PlaidClient,
    store: PlaidItemStore,
    public_token: str,
) -> dict:
    exchange = client.exchange_public_token(public_token)
    access_token = exchange["access_token"]
    item = client.item(access_token)
    institution_id = item["institution_id"]
    connected = {
        "item_id": exchange["item_id"],
        "access_token": access_token,
        "institution_id": institution_id,
        "institution_name": client.institution(institution_id)["name"],
        "accounts": client.accounts(access_token),
        "cursor": None,
    }
    store.upsert(connected)
    return connected


def fetch_all(
    client: PlaidClient,
    store: PlaidItemStore,
    snapshot: PlaidSnapshot,
    fetched_on: str,
) -> dict[str, dict[str, int]]:
    items = store.items()
    if not items:
        raise FileNotFoundError(
            f"{store.path} has no Plaid Items. Run `books plaid-connect` first."
        )
    summary = {}
    for item in items:
        accounts = client.accounts(item["access_token"])
        snapshot.record_accounts(item["item_id"], accounts, fetched_on)
        store.upsert({**item, "accounts": accounts})
        sync = client.sync_transactions(item["access_token"], item.get("cursor"))
        stats = snapshot.apply(item["item_id"], sync, fetched_on)
        store.update_cursor(item["item_id"], sync.cursor)
        summary[item["item_id"]] = {
            **stats,
            "institution": item.get("institution_name", ""),
        }
    return summary


def feed_transactions(rows: list[dict]) -> list[FeedTransaction]:
    """Normalize Plaid's money-out-positive convention for ledger proposals."""
    return [
        FeedTransaction(
            txn_id=row["transaction_id"],
            date=row.get("date", ""),
            amount=-money(row.get("amount")),
            description=(row.get("merchant_name") or row.get("name") or "").strip(),
            source="plaid",
            account_key=row["account_id"],
            pending=bool(row.get("pending", False)),
            raw=row,
        )
        for row in rows
    ]


def affected_revisions(
    revisions: list[dict],
    ledger_statement_ids: set[str],
) -> list[dict]:
    return [
        revision
        for revision in revisions
        if f"plaid:{revision['transaction_id']}" in ledger_statement_ids
    ]
