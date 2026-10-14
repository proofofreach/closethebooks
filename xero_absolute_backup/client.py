from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import ssl
import time

from .config import Settings

try:
    from xero_python.api_client import ApiClient, serialize
    from xero_python.api_client.configuration import Configuration
    from xero_python.api_client.oauth2 import OAuth2Token
    from xero_python.identity import IdentityApi
    from xero_python.accounting import AccountingApi
    from xero_python.exceptions import HTTPStatusException
except ImportError:  # pragma: no cover - optional at runtime
    ApiClient = None
    Configuration = None
    OAuth2Token = None
    IdentityApi = None
    AccountingApi = None
    HTTPStatusException = None
    serialize = None


class XeroApiError(Exception):
    def __init__(self, status_code: int, body: str, headers: dict[str, str]) -> None:
        super().__init__(f"Xero API error {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body
        self.headers = headers


class RateLimitReached(Exception):
    def __init__(
        self,
        reason: str,
        retry_after_seconds: int | None = None,
        *,
        day_remaining: int | None = None,
        minute_remaining: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        self.day_remaining = day_remaining
        self.minute_remaining = minute_remaining


class BurstLimiter:
    def __init__(self, max_calls: int = 60, period_seconds: int = 60) -> None:
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self.calls: deque[float] = deque()

    def wait(self) -> None:
        now = time.time()
        while self.calls and now - self.calls[0] >= self.period_seconds:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            sleep_for = self.period_seconds - (now - self.calls[0]) + 0.25
            if sleep_for > 0:
                time.sleep(sleep_for)
        now = time.time()
        while self.calls and now - self.calls[0] >= self.period_seconds:
            self.calls.popleft()
        self.calls.append(time.time())


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise FileNotFoundError("No saved token file found. Run the login command first.")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, token: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(token, indent=2), encoding="utf-8")
        self.path.chmod(0o600)


class XeroClient:
    def __init__(self, settings: Settings, token_store: TokenStore) -> None:
        self.settings = settings
        self.token_store = token_store
        self.tokens = token_store.load()
        self.burst_limiter = BurstLimiter()
        self.api_client = self._build_sdk_client()
        self.identity_api = IdentityApi(self.api_client)
        self.accounting_api = AccountingApi(self.api_client)
        self.last_response_limits: dict[str, int | None] = {
            "day_remaining": None,
            "minute_remaining": None,
        }

    def _build_sdk_client(self) -> Any:
        if ApiClient is None or Configuration is None or OAuth2Token is None or IdentityApi is None:
            raise RuntimeError(
                "The official Xero SDK is required. Install it with: .venv/bin/pip install xero-python"
            )
        oauth2_kwargs = {"client_id": self.settings.client_id}
        if self.settings.client_secret:
            oauth2_kwargs["client_secret"] = self.settings.client_secret
        api_client = ApiClient(
            Configuration(
                oauth2_token=OAuth2Token(**oauth2_kwargs),
            ),
            pool_threads=1,
        )

        @api_client.oauth2_token_getter
        def get_token() -> dict[str, Any]:
            return self.tokens

        @api_client.oauth2_token_saver
        def save_token(token: dict[str, Any]) -> None:
            self._merge_and_store_token(token)

        api_client.set_oauth2_token(self.tokens)
        return api_client

    def access_token(self) -> str:
        if self._token_expired():
            self.refresh_access_token()
        return self.tokens["access_token"]

    def _token_expired(self) -> bool:
        expires_at = self.tokens.get("expires_at", 0)
        return time.time() >= float(expires_at) - 120

    def refresh_access_token(self) -> None:
        token = self.api_client.refresh_oauth2_token()
        self._merge_and_store_token(token)

    def _merge_and_store_token(self, token: dict[str, Any]) -> None:
        expires_in = int(token.get("expires_in", 1800))
        token["expires_at"] = int(time.time()) + expires_in
        existing = dict(self.tokens)
        existing.update(token)
        self.tokens = existing
        self.token_store.save(existing)

    def api_get_json(
        self,
        path: str,
        tenant_id: str,
        *,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body, headers = self._request(
            "GET",
            f"{self.settings.api_base_url}{path}",
            tenant_id,
            params=params,
            extra_headers=extra_headers,
        )
        return json.loads(body.decode("utf-8")), headers

    def api_get_bytes(
        self,
        url: str,
        tenant_id: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        return self._request("GET", url, tenant_id, extra_headers=extra_headers)

    def _request(
        self,
        method: str,
        url: str,
        tenant_id: str,
        *,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        max_retries = 3
        attempt = 0
        last_error: Exception | None = None
        while attempt <= max_retries:
            self.burst_limiter.wait()
            request_url = url
            if params:
                query = urlencode({k: v for k, v in params.items() if v is not None})
                request_url = f"{request_url}?{query}"
            headers = {
                "Authorization": f"Bearer {self.access_token()}",
                "Accept": "application/json",
                "xero-tenant-id": tenant_id,
            }
            if extra_headers:
                headers.update(extra_headers)
            request = Request(request_url, method=method, headers=headers)
            try:
                with urlopen(request, timeout=120) as response:
                    response_headers = {k.lower(): v for k, v in response.headers.items()}
                    self._update_last_limits(response_headers)
                    return response.read(), response_headers
            except HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                response_headers = {k.lower(): v for k, v in error.headers.items()}
                self._update_last_limits(response_headers)
                if error.code == 401 and "expired" in body.lower():
                    self.refresh_access_token()
                    attempt += 1
                    continue
                if error.code == 429:
                    retry_after = parse_retry_after(response_headers.get("retry-after"))
                    raise RateLimitReached(
                        "Received HTTP 429 from Xero",
                        retry_after,
                        day_remaining=self.last_response_limits["day_remaining"],
                        minute_remaining=self.last_response_limits["minute_remaining"],
                    )
                raise XeroApiError(error.code, body, response_headers) from error
            except (URLError, ssl.SSLError) as error:
                last_error = error
                if attempt >= max_retries:
                    break
                backoff = 2 ** attempt
                time.sleep(backoff)
                attempt += 1
                continue
        if last_error:
            raise last_error
        raise RuntimeError("Request failed without a captured error.")

    def list_connections(self) -> list[dict[str, Any]]:
        try:
            return serialize(self.identity_api.get_connections())
        except Exception as exc:
            if HTTPStatusException is None or not isinstance(exc, HTTPStatusException):
                raise
            if getattr(exc, "status", None) != 401:
                raise
            self.refresh_access_token()
            return serialize(self.identity_api.get_connections())

    def _update_last_limits(self, headers: dict[str, str]) -> None:
        self.last_response_limits = {
            "day_remaining": parse_int_header(headers.get("x-daylimit-remaining")),
            "minute_remaining": parse_int_header(headers.get("x-rate-limit-remaining")),
        }


def http_form_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = urlencode(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        headers = {k.lower(): v for k, v in error.headers.items()}
        raise XeroApiError(error.code, body, headers) from error


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    try:
        retry_dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_dt.tzinfo is None:
        retry_dt = retry_dt.replace(tzinfo=timezone.utc)
    delta = retry_dt - datetime.now(timezone.utc)
    return max(int(delta.total_seconds()), 0)


def parse_int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
