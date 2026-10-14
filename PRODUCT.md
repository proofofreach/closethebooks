# Books: Product Definition

## Register

product

## Product register

| Field | Definition |
| --- | --- |
| Product | Books (distribution `xeroxd-books`) |
| Category | Local-first, AI-assisted bookkeeping workspace and app |
| Primary user | The technical owner-operator of a small business (solo LLC, freelancer, small studio) who already uses a coding agent |
| Operating model | One workspace per company (`books.toml`), plain-text Beancount ledger, deterministic Python tools, git audit trail, a local web app, and Fava for detailed reports |
| Primary job | Know whether the books are current and what action to take next |
| Secondary jobs | Review financial position, answer ledger questions, inspect documents, and prepare annual reports |
| Source of truth | Files under `ledger/`, validated by `bean-check` and balance assertions |
| Product boundary | The app reports, explains, and prepares. It writes only the inbox, proposals, statement evidence, invoice documents, account registrations (including their `open` directive), and settings. Posting transactions goes through the reviewed bookkeeping workflow. |

## Product promise

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
   invoice documents, account setup). Transactions reach the ledger only
   through the reviewed `do-the-books` workflow. Ask is read-only.
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

The Import page turns statements into proposals and hands off to the agent.
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

Calm, exact, candid, and quietly technical. Copy uses plain accounting terms,
specific dates, and direct verbs. The product behaves like a careful operator:
it distinguishes verified facts from estimates, flags gaps without alarmism,
and avoids congratulatory or promotional language.

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
