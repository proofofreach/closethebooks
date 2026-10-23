# Release audit

Audit date: October 22, 2026. Scope: the current working tree, including the
existing uncommitted product extraction. This audit does not publish a release.

## Verdict

The local application is a release candidate after the fixes below. The
ledger-sheet direction is coherent and should be preserved. Book health and
the next action lead the overview; account evidence precedes financial figures.
Navigation stays visible on narrow screens. Financial values identify currency
and period, and proposals remain separate from posted transactions.

This is not an unconditional production sign-off. Live Xero/Plaid/Teller
connections and real Claude/Codex answers need a credentialed integration
smoke test. Screen-reader operation and the full upstream Fava interface need
manual accessibility testing. No live financial service or paid AI provider
was invoked during this audit.

## Findings addressed

### P1: Local server protection bypass

`bookkeeping/app/__init__.py`: loopback source addresses did not prevent an
attacker-controlled hostname from reaching local financial data through DNS
rebinding. Fava's DispatcherMiddleware also bypassed Books' Flask guards.

Both applications now require a loopback source and a localhost/loopback Host.
Both send `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
`X-Frame-Options: SAMEORIGIN`, and a same-origin referrer policy. Foreign Host
requests to Books, static files, and Fava were rejected over real HTTP.
Upload redirects now use the fixed Import destination instead of form input.

### P1: Validation discarded Settings input

`bookkeeping/app/templates/settings.html`: a rejected invoice prefix reset
other fields to their saved values. The form now preserves every submitted
value. Validation summaries receive keyboard focus after reload.

### P1: Keyboard focus and contrast

`bookkeeping/app/static/app.css`: input focus used a translucent wash instead
of a solid indicator. The file picker was visually hidden without a visible
focus proxy. Both now have solid focus indicators. Input/button boundaries have dedicated
contrast tokens, and placeholders use the metadata colour. Muted financial basis,
month, and footer text now use the readable metadata colour in both themes.
Secondary buttons, Copy buttons, and period links now have 44px minimum height.

### P1: Ask state corruption

`bookkeeping/app/static/app.js`: valid JSON with the wrong shape could crash
history restoration; unavailable session storage could turn a successful
answer into a failure. Clearing or retrying during another answer could corrupt
conversation state. History is now validated, storage failures are nonfatal,
and conflicting actions are disabled or guarded while busy. Starter buttons
are disabled when no engine is available. Malformed JSON objects are rejected
by the server, and non-object history entries are filtered.

### P1: Base installation broke an advertised CLI

`xero_absolute_backup/reports.py`: importing the optional SDK unconditionally
made `xero-backup --help` crash after a base installation. Reports now use the
client module's optional SDK import. Base help works without the Xero extra;
real Xero operations still require `.[xero]`. README installation instructions
now use an executable command instead of a repository URL placeholder.

### P2: Invoice form and amount edge cases

`bookkeeping/app/static/app.css`: at 320px the line description had only a
small fraction of the row. It now occupies a full row below 480px; amount and
Remove sit below it. Removing a line preserves keyboard focus. Creation shows
a busy label and prevents a second click. Tiny positive amounts that round to
zero and unrepresentably large decimal amounts now return validation errors.
Payment terms are bounded at 0–3650 days; dates outside Python's supported
range return an actionable error. Zero-day terms were verified in the actual
created document.

### P2: Upload recovery and feedback

`bookkeeping/app/static/app.js`: overlapping dropzone uploads could race.
Uploads now run one at a time, expose busy state, check the size before sending,
and restore the picker after failure. Oversize plain-form submissions now show
an HTML recovery page; AJAX submissions still receive JSON. Unknown routes
show the Books error page. The footer now discloses Xero communication as well
as Ask communication.

## Verification

- Existing suite: 94 tests pass. Includes demo ledger validity, import
  deduplication/evidence, account setup, app routes, CSRF, local-peer protection,
  invoices, conversion verification, and migration overwrite refusal.
- Browser: ten routes at 1280, 768, 390, and 320px, forty route/viewport cases.
  No document overflow. Books form controls have labels. No JavaScript page
  errors in the repeatable run.
- Browser workflows: line addition/removal and total, invoice creation with
  zero-day terms, CSV upload followed by proposal preparation, rejected
  Settings input preservation, malformed stored chat history, mocked streamed
  answer, and disabled conversation clearing while working.
- Security over HTTP: foreign Host rejection on Books, Fava, and static files;
  malformed Ask JSON returns 400; absent CSRF returns 403; oversized plain
  upload returns an HTML 413 page; private responses disable caching.
- Distribution: wheel builds; fresh virtual environment installs dependencies;
  packaged demo initializes and passes `books check`; optional Xero CLI help
  runs without its extra. Wheel includes UI files and workspace skill templates
  and excludes private-data filename patterns. `pip check` reports no broken
  requirements.
- JavaScript syntax and `git diff --check` pass.
- Light, dark, desktop and mobile images are in `output/playwright/`.
  Browser output is in `output/playwright/release-check.log`.

The Impeccable detector found two alert accent-border warnings. These are the
existing documented validation/status treatment, not interchangeable content
cards. They were retained. No redesign was needed.

## Remaining release checks

1. Run a credentialed sandbox smoke test for Xero authorization, backup resume,
   general-ledger upload, conversion and account registration. Existing tests
   cover conversion/refusal logic, not the external authorization service.
2. Verify the installed Claude/Codex versions, permission boundaries, stop and
   timeout behavior with a real local CLI session. Browser streaming tests use
   a fictional response and do not certify the external tools' sandbox.
3. Verify optional Plaid/Teller feeds with sandbox credentials if those
   integrations are included in the announced release.
4. Run VoiceOver/NVDA through the core tasks. The automatic label check excludes
   upstream Fava controls; Fava was checked for loading and responsive overflow,
   not certified for WCAG conformance. This audit is not a WCAG certification.
5. Review the existing staged deletions and uncommitted extraction before
   making the release commit. This audit leaves staging and release publishing
   to the owner. Only the current Python/macOS environment was exercised;
   Python 3.11 and other operating systems were not tested here.

## Repeat the browser check

Use a disposable workspace. The script creates invoice documents and inbox
files there. It does not post transactions. Port 5178 must serve this demo.

```sh
.venv/bin/python -m bookkeeping demo /tmp/books-release-demo
.venv/bin/python -m bookkeeping --workspace /tmp/books-release-demo serve --no-browser --port 5178
```

In another terminal, with the Playwright CLI available:

```sh
mkdir -p output/playwright
playwright-cli -s=books-release open http://127.0.0.1:5178
playwright-cli -s=books-release run-code "$(cat scripts/release-browser-check.js)" > output/playwright/release-check.log
```

The result must contain `"passed":true`. The CLI can exit successfully while
printing `### Error`, so inspect the result rather than only the exit code.
The mock Ask check needs at least one installed assistant CLI so its composer
is enabled; it does not call that CLI. Screenshots and logs are ignored by git.


## Follow-up: dark default, Plaid, and import sources

- Books now defaults to dark, independent of OS preference. Settings offers
  Light and System options, saved per browser and applied before paint. Fava
  retains its own appearance setting; its UI has not been restyled.
- The Plaid helper previously always sent an HTTP OAuth redirect, incompatible
  with production redirect rules. It now uses desktop popup OAuth without a
  redirect URI by default. Optional HTTP redirect mode is Sandbox-only.
  Redirect re-entry reopens Link; token-exchange failures permit retry; foreign
  Host requests are rejected. No real Plaid connection was made.
- PayPal activity CSV is selectable in account setup. Bank, card, PayPal and
  other supported formats are explained on Import. The later file-first work
  below adds QuickBooks/Wave ledger-history adapters.
- Repeated identical statement uploads now reuse the existing account's inbox
  file. Different content with the same filename is still retained separately.
  This prevents renamed duplicates from conflicting during evidence preparation.
- Follow-up verification: 94 tests pass; forty route/viewport cases pass,
  plus theme persistence/System behavior, PayPal fees, and duplicate-upload
  assertions. The fictional-provider Plaid HTTP smoke test passes.

## File-first onboarding and migration implementation

The default path now uses files. Overview and Import distinguish recurring bank
statements from one-time accounting history. Plaid and Teller remain optional
advanced feeds. Migration choices include Xero, QuickBooks Online and Wave.
After a ledger has history, Overview asks for account registration instead of
suggesting another migration.

Generic bank CSV files must be mapped before the app prepares proposals. Mapping
requires date order, decimal format and amount direction (split debit/credit
columns already define direction). The complete file is validated. Ambiguous
day/month dates are flagged. Running balances require oldest/newest row order,
including within a date. Source bytes bind the saved mapping. Preview approval
also binds the account and its sign settings. Edited files or fields need a new
preview. PayPal keeps its specialized fee parser.

QuickBooks Online and Wave import mapped journal history from CSV/XLSX; Wave also
supports explicit member selection in an accounting ZIP. Every journal must
balance. Source debit/credit totals and per-account parsed postings must match.
Preview does not write the ledger. Apply requires an empty workspace and rechecks
the source, mapping, candidate and workspace. The include structure remains
connected for later account registrations and year files. Original files,
pre-migration ledger files and verification reports are retained. This covers
ledger history, not open invoices, contacts, bills or attachments. Real export
layouts can require an explicit values-only/filled-down copy; no vendor layout
is guessed. See `FILE_MIGRATIONS.md`.

Independent UX review found and closed ten issues, including unmapped CSV guesses,
silent sign defaults, preview focus and same-day balance ordering. A second review
accepted the flow. Independent migration review found a dropped-include blocker,
Excel date-system mismatch, interrupted-apply evidence loss and rejected valid
split lines. Corrections have dedicated end-to-end scenarios. A second safety review accepted
the implementation with no data-loss or unsafe-completion blockers. Interrupted
apply requires deliberate recovery from the retained files; do not discard its
evidence or assume a new preview can clear an interrupted state.

Verification artifacts (local, ignored by git):

- `output/playwright/file-first-tests.xml`: 155 passing tests, including 61 new
  migration HTTP-to-ledger scenarios. The duplicate-ZIP fixture emits one expected
  Python warning while verifying rejection.
- `output/playwright/csv-mapping-e2e.json`: 71 assertions through saved mapping,
  CLI proposals, retained evidence and Beancount validation.
- `output/playwright/release-check.log`: 52 route/viewport cases plus theme,
  invoice, upload, proposal, PayPal, Ask stream and foreign-Host checks.
- `output/playwright/file-import-check.log`: bank CSV upload, required mapping,
  preview, rejected stale approval, save and proposals; 1280/390/320 layouts.
- `output/playwright/csv-ambiguity-check.log`: dash-separated ambiguous dates
  display the chosen date and the ambiguity warning.
- `output/playwright/csv-preview-check.log`: focus moves to the validated preview.
- `output/playwright/migration-browser-check.log`: source upload, field/account
  mapping, review, apply, refresh and post-apply onboarding; 12 route/viewport cases.

Repeat with fresh disposable workspaces. Start a demo on port 5178 for
`scripts/release-browser-check.js`, `scripts/file-import-browser-check.js`, and
`scripts/csv-ambiguity-browser-check.js`, in that order. Start a separate empty
workspace on port 5179 for `scripts/migration-browser-check.js`. That script
explicitly applies fictional ledger history; never point it at real books.
Run each script with `playwright-cli run-code` as above and check `passed:true`.

Live Plaid/Xero accounts and representative real QuickBooks/Wave exports were
not used. Verification proves the declared input contract with synthetic files,
not completeness of any particular company's export. Legacy standalone CLI CSV
header guessing remains backward compatible; use saved mappings for unfamiliar
files. Python 3.11 and non-macOS operating systems remain untested here.

The clean wheel install also passed a separate smoke check, including Fava, all
new entry pages, a fictional migration, and later account/year writes. Its result
is `output/playwright/packaged-filefirst-smoke.json`. The wheel inventory contains
only the two application packages and distribution metadata.
