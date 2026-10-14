# BooksBot

Your books, done by AI: quickly, with almost no effort, and proven against
your bank statements.

Drop in your statements and tell your coding agent "do the books". It
categorizes, posts, and validates every transaction, and you approve one diff.
Deterministic tools handle parsing, deduplication, and checks, so every
balance is verified against its statement, not guessed. The books live in a
plain-text [Beancount](https://beancount.github.io/) ledger in a folder you
own, tracked in git. A local web app tells you within seconds whether the
books are current and what to do next.

- **No subscription, no server.** Everything runs on your machine. Bank feeds
  (Plaid) are optional. Statement files are the dependable path.
- **Evidence over reassurance.** A balance counts as verified only when a
  statement backs it. The retained statement is SHA-addressed and linked from
  the ledger.
- **Reviewed changes only.** The app never posts transactions. Your agent
  proposes, you approve the diff, and git keeps the audit trail.

## Install

Requires Python 3.11+. Poppler (`pdftotext`) is optional and improves PDF
statement parsing.

```bash
pipx install .                          # from a checkout (PyPI: `booksbot`, not yet published)
books demo ~/books-demo                  # a fictional company to explore
books --workspace ~/books-demo serve     # opens http://127.0.0.1:5170
```

## Start your own books

```bash
books init ~/acme-books --name "Acme Studio LLC" --currency USD --start-date 2026-01-01
cd ~/acme-books
books add-account operating-checking --account Assets:Bank:Operating-Checking
books add-account business-card --account Liabilities:Cards:Business-Card
books serve
```

`books init` creates the workspace:

```
acme-books/
  books.toml           company, currency, account roles, invoice settings
  sources.toml         bank and card accounts: formats, cadence, lifecycle
  ledger/              main.beancount, accounts, years/, documents/, evidence/
  inbox/               statement uploads (gitignored)
  AGENTS.md, CLAUDE.md, .claude/skills/   bookkeeping agent instructions
```

Every company-specific fact lives in `books.toml`. Commands find the workspace
from `--workspace`, then `$BOOKS_WORKSPACE`, then the nearest parent folder
that contains `books.toml`.

## The monthly cycle

1. Open the app. **Overview** shows book health, then the next action:
   usually "Upload the <account> statement covering <dates>", with a dropzone.
2. Upload statements (CSV, OFX/QFX/QBO, MT940, or a supported PDF).
   **Import** then prepares proposals: deduplicated against the ledger, with
   categories learned from your reviewed history, and statement balances to
   verify.
3. Tell your coding agent **"do the books"**. It categorizes, posts to the
   ledger, runs `books check`, and shows you the diff. Approve it to commit.

## Using the app

| Page | What it does |
| --- | --- |
| Overview | Book health, next actions, a 12-month coverage strip per account, and a financial snapshot |
| Import | Upload statements, prepare proposals, and review suggested categories |
| Accounts | Add or edit bank and card accounts, formats, statement cadence, and lifecycle |
| Invoices | Invoice status from the ledger. Creates the invoice document and its receivable entry |
| Ask | Read-only questions about the books, answered from the ledger by your local Claude or Codex CLI |
| Settings | Company details, invoice defaults, and the one-time move from Xero |
| Reports | [Fava](https://beancount.github.io/fava/): statements, journal, documents, and queries |

The app listens only on 127.0.0.1 and rejects requests that lack a per-session
token. It writes only to the inbox, proposals, statement evidence, invoice
documents, account registrations, and settings.

## CLI

```bash
books init DIR --name NAME     # create a workspace
books demo [DIR]               # create a fictional demo workspace
books serve                    # local web app
books status [--json]          # coverage, verification, close obligations
books check                    # validate the ledger (also the git pre-commit hook)
books query "SELECT ..."       # Beancount query language
books add-account KEY --account ACCOUNT [--formats csv,ofx] [--preset P]
books propose --statement KEY=FILE [...]
books fetch                    # pull connected Plaid/Teller feeds
books plaid-connect | plaid-accounts | plaid-revisions
books invoice --client NAME --item TEXT --amount N [...]
books report [--year YYYY]     # income, balance, cash flow, trial balance
books agent-files [--force]    # refresh the bookkeeping agent instructions
```

## Moving from Xero

Start from a new workspace and open **Settings → Move from Xero** (or the link
on the first-run overview). The page walks through six steps:

1. Create a free Xero app (Mobile or desktop, so no secret is needed) with the
   redirect URI the page shows.
2. Sign in to Xero and choose the organisation. Access is read-only.
3. Back up the organisation through the API: contacts, invoices, bank
   transactions, chart of accounts, and every attachment. The backup is
   resumable when Xero's daily API limit pauses it.
4. Upload Xero's General Ledger Detail export (.xlsx). Xero's API does not
   expose the full general ledger.
5. Convert and verify. Every account total is compared with Xero to the cent,
   and attachments are filed next to their transactions.
6. Register the bank and card accounts. Recently active ones start active;
   imports begin the day after the Xero history ends.

Conversion is refused once the ledger has its own transactions or registered
accounts, so it can never overwrite real bookkeeping. The same steps exist on
the command line: `xero-backup`, `books xero-merge-gl`, `books xero-convert`,
and `books xero-verify`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,xero]"
.venv/bin/pytest -q
```

`PRODUCT.md` defines the product and `DESIGN.md` the interface system. See
`docs/` for statement formats, Plaid setup, and Teller.

## License

Apache License 2.0. See `LICENSE`.
