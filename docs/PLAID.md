# Plaid bank-feed setup

Plaid supplies continuous transaction observations. Bank statements and
balance assertions remain the reconciliation evidence; a feed alone does not
prove completeness.

## 1. Create and configure Plaid

1. Create a Plaid dashboard team and start with Sandbox. Eligible new US and
   Canadian teams can apply for the live Trial plan.
2. Add `http://localhost:8767/` as an allowed redirect URI in the Plaid
   dashboard. Chase, Bank of America, Capital One, and American Express use
   OAuth, so the redirect must match exactly.
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
stores the resulting access token. The helper binds only to localhost.

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
