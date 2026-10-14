"""Local-only Plaid Link flow for connecting the owner's bank accounts."""

from __future__ import annotations

import html
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from .plaid import PlaidClient, PlaidItemStore, complete_connection


def _javascript(value: str) -> str:
    return json.dumps(value).replace("</", "<\\/")


def render_link_page(link_token: str, nonce: str, company: str = "your books") -> str:
    company = html.escape(company)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect bank · {company}</title>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <style>
    body {{ font: 16px system-ui; margin: 10vh auto; max-width: 34rem; padding: 1rem; }}
    button {{ font: inherit; min-height: 44px; padding: .6rem 1rem; }}
    #status {{ color: #555; line-height: 1.5; }}
  </style>
</head>
<body>
  <h1>Connect a financial institution</h1>
  <p>Plaid receives your bank authorization. Only the resulting access token
  is stored, locally, in this workspace's <code>secrets/plaid/</code> folder.
  It is never sent anywhere else.</p>
  <button id="connect" type="button">Connect with Plaid</button>
  <p id="status" role="status" aria-live="polite"></p>
  <script>
    const status = document.getElementById("status");
    const options = {{
      token: {_javascript(link_token)},
      onSuccess: async (publicToken) => {{
        status.textContent = "Saving the connection locally…";
        const response = await fetch("/exchange", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{public_token: publicToken, nonce: {_javascript(nonce)}}})
        }});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Connection failed.");
        status.textContent = `${{result.institution_name}} connected. You can close this page.`;
      }},
      onExit: (_error, metadata) => {{
        if (metadata.status) status.textContent = `Plaid Link closed: ${{metadata.status}}`;
      }}
    }};
    if (new URLSearchParams(location.search).has("oauth_state_id")) {{
      options.receivedRedirectUri = location.href;
    }}
    const handler = Plaid.create(options);
    document.getElementById("connect").addEventListener("click", () => handler.open());
  </script>
</body>
</html>"""


def run_connect(
    client: PlaidClient,
    store: PlaidItemStore,
    redirect_uri: str,
    *,
    company: str = "your books",
    client_user_id: str = "books-owner",
    country_code: str = "US",
    timeout_seconds: int = 600,
) -> dict:
    redirect = urlparse(redirect_uri)
    if redirect.scheme != "http" or redirect.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Plaid redirect URI must use http://localhost or 127.0.0.1.")
    port = redirect.port or 80
    link_token = client.create_link_token(
        redirect_uri,
        client_name=company,
        client_user_id=client_user_id,
        country_code=country_code,
    )
    nonce = secrets.token_urlsafe(24)
    result: dict = {}
    completed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = render_link_page(link_token, nonce, company).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/exchange":
                self.send_error(404)
                return
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 16_384)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not secrets.compare_digest(payload.get("nonce", ""), nonce):
                    raise ValueError("Connection request expired. Reload and try again.")
                connected = complete_connection(client, store, payload["public_token"])
                result["connected"] = connected
                response = json.dumps(
                    {
                        "institution_name": connected["institution_name"],
                        "accounts": connected["accounts"],
                    }
                ).encode("utf-8")
                status = 200
            except Exception as exc:
                result["error"] = exc
                response = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(response)
            completed.set()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer((redirect.hostname or "127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Open {redirect_uri} to connect an institution with Plaid.")
    webbrowser.open(redirect_uri)
    try:
        if not completed.wait(timeout_seconds):
            raise TimeoutError("Plaid Link did not finish before the local session expired.")
        if "error" in result:
            raise RuntimeError(f"Plaid connection failed: {result['error']}")
        return result["connected"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
