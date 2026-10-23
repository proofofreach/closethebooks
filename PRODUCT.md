# CloseTheBooks: Product Definition

## Register

product

## Product register

| Field | Definition |
| --- | --- |
| Product | CloseTheBooks (distribution `closethebooks`, command `books`) |
| Category | AI bookkeeping: an AI agent does the books; deterministic tools prove them |
| Primary user | The technical owner-operator of a small business (solo LLC, freelancer, small studio) who already uses a coding agent |
| Operating model | One workspace per company (`books.toml`), plain-text Beancount ledger, deterministic Python tools, git audit trail, a local web app, and Fava for detailed reports |
| Primary job | Know whether the books are current and what action to take next |
| Secondary jobs | Review financial position, answer ledger questions, inspect documents, and prepare annual reports |
| Source of truth | Files under `ledger/`, validated by `bean-check` and balance assertions |
| Product boundary | The app reports, explains, and prepares. It writes only the inbox, proposals, statement evidence, invoice documents, account registrations (including their `open` directive), and settings. Posting transactions goes through the reviewed bookkeeping workflow. |

## Product promise

Your books, done: quickly, with almost no effort, by AI. Drop in statements,
say "do the books", approve the diff. Every number the AI posts is checked
against the statement it came from, so speed never costs accuracy.

Open the dashboard and understand book health within five seconds. If work is
required, show the next concrete action. If the books are current, say so
plainly and show when they were last verified.

## User context

The user understands files, git, command-line tools, and accounting basics.
They operate the business alone and review the books periodically, not all day.
The interface must support quick reorientation after weeks away. It should
expose evidence (dates, balances, flags, and source coverage) without requiring
the user to remember the ingestion pipeline.

## Product principles

1. **Health before metrics.** Lead with currency, coverage, and exceptions;
   place financial summaries after the next action.
2. **Evidence over reassurance.** Show the last transaction, last verified
   balance, staleness, and unresolved count behind every status.
3. **One clear next step.** Translate stale or incomplete state into a concrete
   instruction such as downloading a named account's statement.
4. **Prepare, never post.** The app may stage work (statements, proposals,
   invoice documents, account setup). Recurring transactions reach the ledger through the reviewed `do-the-books`
   workflow. A one-time migration may initialize an empty ledger only after
   source verification, a coverage review, and explicit acceptance. Ask is read-only.
5. **Local and inspectable.** Keep data and behavior understandable from files;
   avoid UI state that obscures ledger state.
6. **Failure must be actionable.** Name what failed, what remains safe, and how
   to retry. Never expose raw transport errors as the final explanation.
7. **Company-neutral.** Nothing company-specific lives in the product.
   Names, currency, account roles, and numbering come from `books.toml`.

## Core experience

The dashboard answers these questions in order:

1. Are the books current?
2. What needs attention now?
3. Through what date is each active account covered and verified?
4. What is the current financial snapshot?
5. Where can I inspect detail or ask a question?

The Import page starts with files: recurring bank statements or one-time
accounting history. It turns statements into proposals and hands off to the agent.
CSV mapping makes column meanings, date order, number format, and amount direction
explicit. Plaid and Teller are optional advanced feeds, not onboarding prerequisites.
Accounts registers bank and card sources. Invoices shows status from the
ledger and creates invoice documents. The Ask page answers read-only financial
questions from ledger data. It must
make its read-only scope, working state, source engine, stop behavior, and
recovery path clear.

## Success criteria

- The user can identify overall book health and the next action within five
  seconds of opening the dashboard.
- Every active account exposes transaction recency and balance-verification
  recency; status never relies on color alone.
- Keyboard and screen-reader users can operate every control and understand
  dynamic Ask states.
- Desktop and 390 px layouts preserve content order, readable values, and
  usable controls without hidden actions.
- Light and dark Fava themes remain legible without product-specific overrides.

## Brand personality

Fast, effortless, and trustworthy. Marketing leads with the outcome: books
done in minutes by AI. Inside the product the voice stays calm and exact,
because confidence about money comes from evidence, not excitement. Copy uses
plain accounting terms, specific dates, and direct verbs; it distinguishes
verified facts from estimates and flags gaps without alarmism. Speed is shown
(fewer steps, one clear next action), not claimed with hype.

## Anti-references

- A generic SaaS analytics dashboard with a wall of equally weighted KPI cards.
- A consumer-finance app that uses celebratory color, gamification, or vague
  "financial wellness" scores.
- An accounting suite that hides provenance behind automation or implies that
  an unverified feed is reconciled.
- A chat-first product that makes the user ask for status the dashboard already
  knows.
- A terminal imitation that sacrifices scanning, accessibility, or responsive
  behavior to look technical.

## Deliberate exclusions

This product does not provide payroll, sales-tax calculation, bills workflow,
mobile-native access, or autonomous ledger edits. Bank feeds are optional;
statement import remains the dependable recovery path.
