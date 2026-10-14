# Bookkeeping workspace

This folder holds one company's books. Read `books.toml` first: it names the
company, its currency, and the ledger accounts with special roles (cash and
card prefixes, receivables, sales income, transfer clearing, uncategorized).
Never assume those values; read them.

- The ledger is plain-text Beancount. The main file is `[paths] ledger` in
  `books.toml` (default `ledger/main.beancount`); year files are under
  `ledger/years/`.
- Account sources (bank and card accounts, statement formats, cadence) live in
  the file named by `[paths] sources` (default `sources.toml`).
- Use the `books` CLI for deterministic work. Never do arithmetic by hand.
  - `books status`: coverage, staleness, and close obligations.
  - `books check`: validate the ledger. It must pass before any commit.
  - `books query "<BQL>"`: answer questions from ledger data.
  - `books propose --statement <key>=<file>`: turn statements and feeds into
    proposals at `[paths] proposals`.
  - `books report --year <year>`: write the standard report set.
- Git history is the audit trail. Show the diff and commit only after the owner
  approves.

Workflows: `.claude/skills/do-the-books/SKILL.md` (import, categorize, close)
and `.claude/skills/make-invoice/SKILL.md` (invoices and receivables). Other
coding agents should read those files directly.
