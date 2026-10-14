---
name: do-the-books
description: Run the bookkeeping cycle for this workspace — fetch bank feeds, parse statement files in the inbox, categorize, append to the Beancount ledger, validate, and present the diff for review. Use when the user says "do the books", asks to import bank transactions, or wants to close a month.
---

# Do the books

You are the bookkeeper for the company named in `books.toml` (`[company]`).
Use its currency and the account roles under `[chart]`. Correctness comes from
deterministic tools; your job is judgment (categorization), never arithmetic.

Paths below are the defaults. If `books.toml` has a `[paths]` table, use its
values instead.

## Workflow

1. **Ingest.**
   - `books fetch` pulls connected Plaid (or Teller) feeds. If no feed is
     configured, say so and continue with statements.
   - If Plaid reports a change to a transaction already in the ledger, stop.
     Review `books plaid-revisions` and the raw sync evidence, correct the
     ledger if needed, then resolve the revision explicitly.
   - Statement files in `inbox/` are named `<account-key>--<name>.<ext>` when
     uploaded through the app. Pass each one as `--statement <key>=<path>`.
     Parser presets come from the sources file automatically.
   - Run `books propose --statement ...`. It writes deduplicated proposals to
     `proposed.beancount`, stores SHA-addressed evidence under
     `ledger/evidence/statements/`, and proposes balance assertions from
     statement ending balances.
   - If the stats show `unmapped_account`, stop and ask the owner to add the
     account (`books add-account` or the Accounts page).
   - A PDF import must pass its parser's balance reconciliation. Never copy
     figures by hand from a partial extraction.

2. **Categorize.** Review the proposals:
   - Learned suggestions carry `category-suggested`, `category-confidence`,
     and `category-evidence`. After you accept or correct one, add
     `category-review: "accepted"` and keep the suggestion metadata. A high
     score is evidence, not permission to post without review.
   - Replace the uncategorized accounts from `[chart]` with the right account
     from `ledger/accounts.beancount` when the description makes it clear.
     Check history with `books query "SELECT account, count(*) WHERE narration ~ 'PATTERN' GROUP BY account"`.
     Keep the `!` flag on anything uncertain; the owner decides those.
   - Never change amounts, dates, or `stmt-id` metadata.

3. **Append.** Move finalized transactions and generated balance assertions
   into `ledger/years/<year>.beancount` (by each entry's date), in date order.
   Create the year file and add its `include` to the main ledger file if it is
   missing. Never edit a generated balance amount or remove its `evidence:`
   link.

4. **Receipts.** For files in `inbox/receipts/`, read each one, then match it
   to a transaction by exact amount, date within 3 days, and payee. File a
   match under `ledger/documents/<Account/Path>/<date>.<short-name>.<ext>` and
   add `document: "../documents/..."` metadata to the transaction. File an
   unmatched receipt under the most likely account and flag it in the summary.

5. **Validate.** `books check` must pass, and `books status` must accept
   every balance assertion's statement evidence. If a balance assertion fails,
   do not force it. Look for missing or duplicate transactions or a wrong
   `statement_balance_sign`, and report what you found.

6. **Review.** Show `git diff --stat`, new evidence files, and remaining `!`
   entries. Commit only after the owner approves:
   `git add -A ledger && git commit -m "Books: <period> import (<n> transactions)"`.
   Then move processed inbox files to `inbox/archive/YYYY-MM/`, and push if a
   remote exists.

## Monthly close checklist

- `books status` shows every active account current and verified.
- No `!` entries older than 30 days.
- `books check` passes.
- The transfer clearing account from `[chart]` nets to zero.
- `books query "SELECT root(account, 1), sum(position) WHERE year = <year> GROUP BY root(account, 1)"` looks plausible.
- Committed, and pushed if a remote exists.

## Patterns

- **Transfers between own accounts.** Both statements show the move. Book each
  side against the transfer clearing account, never one bank account directly
  against another, or the second import double-counts it. A nonzero clearing
  balance after both statements arrive means a side is missing.
- **Opening balances.** When the owner adds an account mid-life, post its
  opening balance against `Equity:Opening-Balances` on the day before the
  first imported transaction, then add a balance assertion from a statement.
- **Invoice payments.** Deposits for invoiced work post against the
  receivable account in `[chart]`, not sales income. Income was recognized when
  the invoice was recorded.
- **Payments per payee for a year (1099 prep).**
  `books query "SELECT payee, sum(position) WHERE account ~ '^Expenses' AND year = <year> GROUP BY payee ORDER BY sum(position) DESC"`
