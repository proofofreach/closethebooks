# Plaid bank-feed setup

> **Experimental.** The Plaid adapter is covered by automated tests but has not
> yet been verified against a live Plaid production account. Statements remain
> the supported path.

Plaid supplies continuous transaction observations. Bank statements and
balance assertions remain the reconciliation evidence; a feed alone does not
prove completeness.

## 1. Create and configure Plaid

1. Create a Plaid dashboard team and start with Sandbox. Eligible new US and
   Canadian teams can apply for the live Trial plan.
2. Use the default popup OAuth flow. It does not send an OAuth redirect URI
   to Plaid; the bank returns to the open Link window. Allow popups for the
   local helper. No local TLS certificate or public callback is required for
   this desktop flow.
3. Put the dashboard credentials in the workspace `.env` (gitignored), or export
   them in the shell that launches the tools:

   ```bash
   export PLAID_CLIENT_ID="..."
   export PLAID_SECRET="..."
   export PLAID_ENV="sandbox"  # change to production after Trial approval
   ```

Never put live credentials or access tokens in git. Long-lived Item tokens are
stored in the workspace's gitignored `secrets/plaid/items.json` with mode `0600`.

## 2. Connect institutions

Run this once for each institution/login:

```bash
books plaid-connect
```

The command opens Plaid Link at `http://localhost:8767/`. Bank authorization
happens in Plaid; the local helper exchanges the short-lived public token and
stores the resulting access token. The helper binds only to localhost and rejects foreign Host headers.
Keep its terminal session and Link window open until the connection is saved.
The default popup mode works with Sandbox and production API environments.
The `--redirect-uri` option names the local helper address; in popup mode it
is not sent as the OAuth redirect URI.

To exercise redirect re-entry in Sandbox only:

```bash
books plaid-connect --oauth-mode redirect
```

For that mode, allowlist `http://localhost:8767/` in the Plaid dashboard.
HTTP redirect mode is refused in production: Plaid requires HTTPS OAuth
redirects outside Sandbox. The default popup flow avoids that restriction.
A future mobile/webview integration would need its own HTTPS redirect flow.

List the connected accounts without displaying access tokens:

```bash
books plaid-accounts
```

Register each account with its returned `account_id`:

```bash
books add-account operating-checking --account Assets:Bank:Operating-Checking \
    --ingestion plaid --feed-account-id the-returned-plaid-account-id
```

For an account already registered for statements, add `--replace`. The same
fields can be edited in the workspace's sources file (`sources.toml` by
default).

Do not infer mappings from names alone. Verify the institution, account type,
mask, and one known transaction before accepting a mapping. Keep the existing
statement configuration as the fallback and reconciliation path.

Plain `books fetch` selects only providers explicitly configured on active
accounts. Merely creating `secrets/plaid/items.json` does not make Plaid the
default. Use `--provider plaid` during the coverage spike; switch each accepted
account to `ingestion = "plaid"` only after its checks pass.

## 3. Fetch and review

```bash
books fetch --provider plaid
books propose
```

`fetch` uses `/transactions/sync`, persists its cursor only after the raw event
batch and current snapshot are safely written, and retains added, modified, and
removed events under `inbox/plaid/raw/`. Plaid reports money-out as positive;
the adapter normalizes it to the money-in-positive convention.

If Plaid later modifies or removes a transaction that is already in the
ledger, `books propose` stops rather than silently ignoring the revision. View
the queue:

```bash
books plaid-revisions
```

After reviewing the raw evidence and correcting the ledger when necessary,
resolve the item explicitly:

```bash
books plaid-revisions --resolve TRANSACTION_ID
```

Resolving a transaction ID acknowledges every queued modification/removal for
that ID and reports how many events were cleared. Review all listed cursors for
the ID before resolving it.

## Coverage acceptance test

Before switching an account to Plaid, verify it. For each source, confirm account discovery, at least one known posted
transaction, correct sign, stable transaction ID across two fetches, and a
usable current balance. Plaid Statements can supplement depository-account
evidence where supported, but manual statement upload remains required for
unsupported institutions and credit cards.

Official references:

- <https://plaid.com/docs/account/billing/>
- <https://plaid.com/docs/transactions/>
- <https://plaid.com/docs/transactions/sync-migration/>
- <https://plaid.com/docs/statements/>

## Connection troubleshooting (reviewed October 22, 2026)

No project-specific Plaid error code was available for this review. Distinguish
provider access from application errors before changing credentials or reconnecting.

- **HTTP redirect rejected in production:** fixed in Books by using popup OAuth
  without `redirect_uri`. HTTP localhost redirects are Sandbox-only.
- **OAuth or institution not enabled:** check the plan and institution access in
  the dashboard. The free Trial plan is for eligible new US/Canadian teams;
  it includes Transactions and immediate access to almost all US OAuth banks.
  It permits up to 10 Items (institution connections, not ten bank accounts).
  Existing Production/Limited Production teams have different eligibility.
- **Trial cap reached:** removing an Item does not restore a slot. Duplicate
  connections consume slots too. Do not reconnect repeatedly to debug. A paid
  plan needs an explicit purchasing decision; Books never upgrades it for you.
- **Popup never opens or Link cannot load:** allow popups for the local helper,
  disable blocking of the Plaid CDN, and retry with the terminal session open.
  Mobile/app redirects are not covered by this desktop helper.
- **Exchange fails:** the helper displays the error and permits a retry within
  the same session. Invalid requests no longer terminate the connection server.
- **Sync revisions:** inspect `books plaid-revisions` before resolving anything.
  A changed transaction already in the ledger needs review, not reconnection.

The local smoke test uses a fictional API transport and a real HTTP helper:

```bash
PYTHONPATH=. .venv/bin/python scripts/check-plaid-connect.py
```

It verifies production popup configuration, local exchange, invalid nonce
recovery, foreign Host rejection, and mode-0600 token storage. It does not
certify real bank OAuth or the user's plan eligibility.

Current official sources:

- [OAuth: popup and redirect requirements](https://plaid.com/docs/link/oauth/)
- [Trial eligibility, limits, and OAuth coverage](https://support.plaid.com/hc/en-us/articles/39994173227159-What-is-the-Plaid-Trial-plan)
- [Pricing and billing](https://plaid.com/docs/account/billing/)
