from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
import base64
import hashlib
import socket
import secrets
import threading
import time
import webbrowser

from .client import TokenStore, XeroApiError, http_form_post
from .config import Settings


@dataclass(slots=True)
class OAuthCallbackResult:
    code: str | None = None
    error: str | None = None
    state: str | None = None


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


class ReusableHTTPServerV6(ReusableHTTPServer):
    address_family = socket.AF_INET6


def generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def run_login(settings: Settings) -> None:
    redirect = urlparse(settings.redirect_uri)
    if redirect.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Only localhost redirect URIs are supported by this CLI helper.")

    use_pkce = not settings.client_secret
    verifier, challenge = generate_pkce_pair() if use_pkce else (None, None)
    state = secrets.token_urlsafe(24)
    callback_result = OAuthCallbackResult()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            callback_result.code = query.get("code", [None])[0]
            callback_result.error = query.get("error", [None])[0]
            callback_result.state = query.get("state", [None])[0]
            self.send_response(302)
            self.send_header("Location", "https://xero.com")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    servers: list[HTTPServer] = []
    port = redirect.port or 8765
    try:
        if redirect.hostname == "localhost":
            try:
                servers.append(ReusableHTTPServer(("127.0.0.1", port), CallbackHandler))
            except OSError:
                pass
            try:
                servers.append(ReusableHTTPServerV6(("::1", port), CallbackHandler))
            except OSError:
                pass
        else:
            servers.append(ReusableHTTPServer((redirect.hostname or "127.0.0.1", port), CallbackHandler))
    except OSError as exc:
        if exc.errno != 48:
            raise

    if servers:
        print(f"Listening for OAuth callback on {settings.redirect_uri}")
        for server in servers:
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()
    else:
        print(
            f"Local callback port {port} is already in use or could not be bound. "
            "You can either free that port or complete the login manually."
        )

    auth_params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "scope": " ".join(settings.scopes),
        "state": state,
    }
    if use_pkce:
        auth_params["code_challenge"] = challenge
        auth_params["code_challenge_method"] = "S256"
    auth_url = f"{settings.auth_authorize_url}?{urlencode(auth_params)}"
    print("Open this URL in your browser and complete the Xero consent flow:")
    print(auth_url)
    webbrowser.open(auth_url)

    if servers:
        deadline = time.time() + settings.auth_timeout_seconds
        while time.time() < deadline:
            if callback_result.code or callback_result.error:
                break
            time.sleep(0.5)
        for server in servers:
            server.server_close()
    else:
        pasted_url = input(
            "Paste the final redirected callback URL from your browser after approval.\n"
            "It should look like http://localhost:8766/callback?code=...&state=...\n"
        ).strip()
        query = parse_qs(urlparse(pasted_url).query)
        callback_result.code = query.get("code", [None])[0]
        callback_result.error = query.get("error", [None])[0]
        callback_result.state = query.get("state", [None])[0]

    if callback_result.error:
        raise RuntimeError(f"OAuth failed: {callback_result.error}")
    if callback_result.state != state or not callback_result.code:
        raise RuntimeError("OAuth callback did not complete successfully.")

    payload = {
        "grant_type": "authorization_code",
        "code": callback_result.code,
        "redirect_uri": settings.redirect_uri,
        "client_id": settings.client_id,
    }
    if use_pkce and verifier:
        payload["code_verifier"] = verifier
    if settings.client_secret:
        payload["client_secret"] = settings.client_secret
    try:
        token = http_form_post(settings.auth_token_url, payload)
    except XeroApiError as exc:
        raise RuntimeError(
            "Token exchange failed. This is usually a redirect URI or scope mismatch.\n"
            f"HTTP {exc.status_code}: {exc.body}"
        ) from exc
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 1800))
    store = TokenStore(settings.token_file)
    store.save(token)
    print(f"Saved OAuth tokens to {settings.token_file}")
