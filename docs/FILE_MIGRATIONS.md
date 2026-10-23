# File-based ledger-history migrations

Books can migrate journal history from QuickBooks Online and Wave export files
into a **new workspace**. The importer does not infer a vendor schema. The
operator selects the table, maps exact column names, maps each source account,
and supplies independent debit and credit totals from the source report.

This is a one-time ledger-history migration, not a complete accounting-platform
migration. It does not create open invoices or bills, payment allocations,
contacts, receipt attachments, inventory, payroll, tax configuration, bank
feeds, or reconciliation status. Account mapping covers accounts present in the
selected journal rows, not unused accounts in a separate source chart. Register
bank and card sources separately after migration. Set their import cutover dates
after the imported activity to avoid overlapping history.

## Export evidence

Intuit documents exporting QuickBooks Online reports to Excel or CSV. Report
layouts differ between report experiences. Use a Journal report containing
complete debit and credit lines for each transaction; retain its date range,
accounting basis, currency, and debit/credit totals. The documentation does not
establish a fixed import schema. See [Intuit: run and export reports](https://quickbooks.intuit.com/learn-support/en-us/help-article/report-management/run-reports-quickbooks-online/L7aILHhbl_US_en_US).

Wave documents an accounting export containing four CSV or XLS files: accounting
entries/transactions, bill items, customers, and vendors. It also documents a
separate receipt export. Only journal rows in the selected accounting table are
imported here; the other files are retained when included in an uploaded ZIP.
Choose CSV for direct use. Legacy XLS must be saved as CSV or XLSX first. See
[Wave: download your account data](https://support.waveapps.com/hc/en-us/articles/4411360860692-Download-your-account-data).

These primary sources establish export availability, not exact column names,
journal identifiers, or guaranteed interchangeability. No live account or
vendor API is contacted by this migration. The selected files are the evidence.
If a source report requires flattening, retain the unmodified report separately
and upload a reviewed, values-only copy. Never invent balancing entries to make
an incomplete report importable.

## Supported files

- UTF-8 CSV, with optional BOM; explicit comma, semicolon, or tab separator.
- XLSX using standard spreadsheet OOXML, inline/shared strings, numeric values,
  and date strings. Select the exact worksheet. Formulas and error/boolean cells
  in that worksheet are rejected. No spreadsheet library is required.
- Wave ZIP containing CSV or XLSX accounting data. Select one member explicitly.
  Other members remain in the original ZIP and appear as unimported evidence.
  No archive is extracted onto the filesystem.

One migration imports one selected table. Multiple exports must first be
combined into one reviewed table. Multiple worksheets and other ZIP members are
not silently merged. XLS, XLSM, encrypted workbooks/archives, PDF journals, and
nested ZIP archives are not accepted. XLSX XML parts must use UTF-8.

Limits: 20 MB uploaded file; 20 MB per archive member; 100 MB total uncompressed
archive content; 256 archive members; compression ratio at most 200:1; 100,000
nonblank data rows; 128 columns; header row within the first 100 rows. Archives
with absolute/traversing paths, duplicate names, symbolic links, or unsupported
compression are rejected. The HTML account-mapping step supports at most 2,000
source accounts.

## Explicit mapping contract

Each row must contain a complete posting. Required column mappings:

| Field | Meaning |
| --- | --- |
| `journal` | ID shared by all lines of one journal; globally unique across journals |
| `date` | Date repeated on every line of that journal |
| `account` | Exact source account name or ID, mapped to a Beancount account |
| `debit` | Nonnegative debit amount; blank means zero |
| `credit` | Nonnegative credit amount; blank means zero |

`currency` and `description` columns are optional. Without a currency column,
the operator must explicitly confirm the workspace currency. When mapped, every
row's currency must equal that currency. Only one three-letter workspace
currency is supported. There is no exchange-rate inference, currency conversion,
or support for mixed-currency journals.

Every mapped field uses a distinct, exact header name. Headers must be unique
and nonempty. A selected header row excludes the preceding preamble; the report
records the excluded row count. Completely blank rows are ignored. Every other
row after the header is validated. Subtotals, footers, opening-balance labels,
and missing identifiers are not silently skipped or filled down.

Amounts use a decimal point, no thousands separators, currency symbols,
exponents, or negative values. Maximum precision is 20 integer digits and 8
fractional digits. Exactly one side of each posting must be positive. A journal
must have at least two postings, consistent dates, and **exactly** equal total
debits and credits. No rounding or suspense postings are added. Identical split
lines can be valid and are retained separately; the importer does not deduplicate
rows. Independent source totals are needed to detect unintended duplicated input.

Date formats: `iso` (`YYYY-MM-DD`), `mdy` (`MM/DD/YYYY`), `dmy` (`DD/MM/YYYY`),
`excel1900`, or `excel1904`. For XLSX numeric serial dates, the selected Excel
date system must match `workbookPr.date1904`; an absent flag means 1900. Excel's
fictitious 1900 leap day is rejected. CSV serial dates require the operator to
select their source date system.

Map every source account to one distinct, valid Beancount account under
`Assets`, `Liabilities`, `Equity`, `Income`, or `Expenses`. There is no automatic
account classification or many-to-one merging. Review account types against the
source chart. Existing account currency restrictions must permit the import.

## Preview and apply

1. Upload retains the original bytes and SHA-256 under a random stage ID.
2. Table/column mapping and account mapping are separate review steps.
3. Preview validates the entire selected table, all journals, and source totals.
   It compares the **parsed candidate ledger's** debit/credit totals, both overall
   and by mapped account, with the source rows. It also validates the proposed
   include tree with Beancount. Preview never writes under the live ledger.
4. Review shows the period, counts, account totals, excluded preamble, unimported
   files/sheets, coverage limits, and downloadable source/report/candidate.
5. Apply requires `confirm=apply` and the exact preview digest. It re-parses the
   source and rebuilds the proposed ledger. Changes to source bytes, mapping,
   candidate, planned files, workspace configuration, source registry, or ledger
   files invalidate the approval.

The source debit and credit totals are entered by the operator, with a reference
to the source report, date range, basis, and currency. Matching them proves that
the candidate agrees with those supplied totals. It does **not** independently
prove completeness of the upstream account, validity of the accounting basis,
source statement reconciliation, or accuracy of the user's account mapping.

Preview and apply refuse existing transactions, registered account sources,
invalid ledgers, plugins, nonstandard accounting options, unsafe include paths,
and existing financial directives such as balances or pads. Empty workspace
`Open`, `Custom`, and `Query` directives are preserved. Supported options are
`title`, `operating_currency`, and `documents`. A normal `accounts.beancount`
include is required. Custom existing ledger structures may need a fresh workspace.

Apply preserves the working ledger structure:

- Existing chart opening dates extend to the earliest imported date.
- Missing mapped accounts are added to `accounts.beancount`.
- Fava settings and saved queries remain included.
- The main ledger includes `years/*.beancount`, so later year-file writes work.
- Imported journals live in `ledger/migrations/<stage-id>.beancount`, included
  from the main ledger. Each transaction retains source system, source journal,
  migration ID, source SHA-256, and source row/description metadata.

The downloadable `candidate.beancount` is a self-contained review artifact;
it is **not** substituted for the live include-based ledger.

## Retention, failure, and recovery

Each stage lives in `imports/file-migrations/<stage-id>/` and retains:

- `source.csv`, `source.xlsx`, or `source.zip`: original uploaded bytes.
- `upload.json`: original name, provider, size, upload time, and SHA-256.
- `draft.json`, `mapping.json`: entered mapping and validated mapping.
- `candidate.beancount`: proposed self-contained review artifact.
- `ledger-plan.json`: exact content of each planned live ledger file.
- `report.json`: validation state, totals, mappings, evidence hashes, original
  workspace signatures, planned ledger hashes, and limitations.
- `before/`: original ledger/config/source files copied before apply.

Stage directories are private (`0700`); generated evidence and replaced files
use `0600`. Evidence contains financial data. Preserve it with the workspace's
backup policy; do not assume raw exports are suitable for a public repository.
No automatic deletion or cloud upload occurs.

Normal validation errors leave the live ledger unchanged. A failed re-preview
invalidates its previous approval. Apply saves backups and an `applying` report
before writing. It writes the history and chart adjustments, then atomically
replaces the main file to activate the history include. A caught write/path
failure restores files already changed, including removal of a newly created
history file. A rollback failure leaves a blocked, review-required state and
retains evidence. A successful apply cannot be repeated.

This is not a filesystem-wide transaction. The main replacement is atomic;
individual files are flushed with `fsync`, but directory entries are not
explicitly synced. A process kill or machine failure can interrupt the sequence.
A per-workspace advisory lock serializes this importer, but unrelated editors and
other bookkeeping writers do not share it. Do not run them during apply.

An `applying` stage is terminal to configure/preview. If every live planned file
matches its expected hash, the report recognizes completion. Otherwise **do not
retry preview, delete evidence, or start another import to bypass the blocker**.
Use the following manual recovery review:

1. Preserve the current ledger and the entire stage directory before recovery.
   Compare current file hashes with `workspace_signature` (before) and
   `ledger_files` (planned) in the report. Inspect `ledger-plan.json` and `before/`.
2. If the main ledger still matches its original hash, the new history include
   did not commit. Confirm each changed chart file matches its planned or original
   hash, and that no later edits exist. Restore only the changed ledger files
   from `before/`. Remove only this stage's unreferenced migration history file,
   after verifying its exact path and planned hash. Do not delete other history
   files or the evidence directory.
3. If the main ledger matches the planned hash, or any file changed after the
   interrupted apply, preserve the ledger and evidence and obtain review. Do not
   restore the old ledger over later work. A report left at `applying` can represent
   a committed migration whose ledger was subsequently edited.
4. Validate the recovered ledger with `books check`. Keep the interrupted stage
   unchanged for audit. Only after review establishes a clean, uncommitted empty
   workspace may a new upload be used for a new migration.

## Blueprint integration and HTTP API

The app factory registers `bookkeeping.app.migrations.bp`:

```python
from .migrations import bp as file_migrations_bp
app.register_blueprint(file_migrations_bp)
```

The blueprint name is `file_migrations`. It uses `books_state`, the existing
loopback/Host guard, CSRF token, templates, and theme. No additional dependency
is required; the file lock uses `fcntl` on macOS/Linux.

| Endpoint | Method and path |
| --- | --- |
| `file_migrations.index` | GET `/migrations` |
| `file_migrations.start` | GET/POST `/migrate/files/<provider>` |
| `file_migrations.review` | GET `/migrate/files/<provider>/<stage_id>` |
| `file_migrations.configure` | POST `.../<stage_id>/configure` |
| `file_migrations.preview` | POST `.../<stage_id>/preview` |
| `file_migrations.apply` | POST `.../<stage_id>/apply` |
| `file_migrations.source` | GET `.../<stage_id>/source` |
| `file_migrations.report` | GET `.../<stage_id>/report` |
| `file_migrations.candidate` | GET `.../<stage_id>/candidate` |

Providers are exactly `quickbooks` and `wave`. Upload uses multipart field
`file`. Every POST requires form field `csrf` or header `X-Books-Token` from the
running app. HTML preview uses POST/303/GET so refresh is safe. Mapping errors
retain entered values where possible. JSON preview accepts this contract:

```json
{
  "selection": {"header_row": 1, "delimiter": ","},
  "fields": {
    "journal": "Entry", "date": "When", "account": "Ledger",
    "debit": "Dr", "credit": "Cr", "currency": "CCY", "description": "Memo"
  },
  "accounts": {"Checking": "Assets:Bank:Checking", "Sales": "Income:Sales"},
  "date_format": "iso",
  "currency": "USD",
  "source_debit": "100.00",
  "source_credit": "100.00",
  "totals_reference": "Journal report, all dates through 2024-12-31, accrual, USD"
}
```

For ZIP, add exact `selection.member`; for XLSX, add exact `selection.sheet`.
These are illustrative headers, not QuickBooks or Wave native-schema claims.
JSON preview returns `status: ready` and `digest`, or HTTP 400 with `error`.
Apply JSON is `{"confirm":"apply","digest":"<preview digest>"}` and returns
`status: applied` on success. Unknown providers/stages and cross-provider stage
access return 404. Apply is never a GET operation.

## Repeatable validation

The E2E scenarios in `tests/test_file_migration_e2e.py` were written before the
implementation and before the corresponding review fixes. They exercise the real
HTTP blueprint, temporary workspace files, archive parsing, and Beancount loader.
No private finance files or external accounts are used.

```sh
.venv/bin/pytest -q tests/test_file_migration_e2e.py tests/test_xero_migration.py \
  --junitxml=/tmp/file-migration-e2e.xml
```

Coverage includes preview/apply/replay, CSV/XLSX/Wave ZIP, source totals, account
and currency validation, journal balance, legitimate duplicate splits, malicious
archives, formulas, Excel date systems, stale/tampered evidence, existing ledger
and sources, application restart, failed re-preview, interrupted apply evidence,
write-failure rollback, malformed requests/drafts, HTML refresh/error recovery,
and later account/year writes with preserved queries. The JUnit XML is the
repeatable validation artifact. Success cases also retain source/report/ledger
files in pytest's fictional temporary workspaces.
