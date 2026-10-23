# Statement formats

Statements are the dependable import path and the only source of verified
balances. Each account source lists the formats it accepts
(`statement_extensions`); the Import page and dropzones enforce that list.

| Format | Extensions | Notes |
| --- | --- | --- |
| CSV | `.csv` | The app requires a validated mapping for generic CSVs. The legacy CLI parser also recognizes common headers; prefer an explicit mapping. The `paypal` preset handles PayPal activity exports, including separate fees. Select it under Accounts → Statement parser. |
| OFX family | `.ofx`, `.qfx`, `.qbo` | Quicken, QuickBooks Web Connect, and OFX downloads. Most US banks and card issuers offer one of these. Preferred when available: stable transaction IDs and a ledger balance. |
| MT940 | `.mt940`, `.sta`, `.swift` | SWIFT bank statements, common outside the US. |
| PDF | `.pdf` | Only with an institution parser preset (`quickbooks-checking` today). |

When a bank offers several formats, choose OFX/QFX/QBO first, then CSV. Use
PDF only when nothing machine-readable exists.

## Guided bank CSV mapping

Upload the original file, then choose **Map CSV** beside it in the inbox.
Select comma, semicolon, or tab, and read the columns. Generic CSV files cannot
be prepared in the app until a mapping has been validated. Match the date,
description, and amount columns (or unsigned debit and credit columns).
Choose ISO, month/day/year, or day/month/year explicitly. Choose the decimal
separator and whether a positive amount means money in or money out. If you
map a running balance, choose newest-first or oldest-first row order, including
within the same day. This determines which balance closes the statement.

**Preview all rows** validates the complete file, while the table displays
only the first 20 transactions. Check dates, direction, and amounts, then
choose **Use this mapping** and prepare proposals. Account-level transaction
inversion and balance-sign settings still apply; the UI preview includes
transaction inversion. Changing fields or source bytes requires a new preview.

Mappings are stored in `inbox/.csv-mappings/<source-sha256>.json`. They apply
to the exact source bytes and survive a rename within that inbox. They do not
automatically apply to a different month's export. The original CSV is kept
as evidence. Mapping semantics are retained in its manifest; reinterpretation
after evidence retention fails rather than replacing the evidence.

Malformed rows, duplicate headers/references, invalid dates, fractional cents,
nonfinite numbers, conflicting amount columns, and unexpected mapped currencies
are rejected. CSV files must use UTF-8. With no currency column, the account's
currency is assumed. Unknown PDF layouts still require a dedicated parser.
PayPal's parser handles its fees separately; generic mapping is not offered
for PayPal-configured accounts.

Anonymous transaction IDs depend on source bytes and row position. Identical
uploads are safe to retry. Overlapping exports without stable bank reference
IDs still require review; they cannot prove transaction identity across files.

## Sign conventions

Amounts are bank-normal: money into the account is positive. If a card export
reports purchases as positive, set `invert = true` on that account source and
verify one known purchase before importing. The ending balance sign is set
separately with `statement_balance_sign`: `ledger` keeps the reported sign,
`invert` turns a positive card balance into a negative liability. Neither is
guessed from the account name.

## PDF safety model

1. Extract independent candidates with Poppler `pdftotext` (layout, default,
   and raw modes) plus `pypdf` fallbacks.
2. Preserve whitespace and characters; never apply prose cleanup or OCR repair
   to financial data.
3. Let the institution parser evaluate every candidate.
4. Accept a result only when running balances and the statement's
   beginning-to-ending balance equation reconcile exactly to the cent.
5. Detect image-only statements and fail with an explicit OCR-required error.

## Evidence

Every accepted statement is retained under its SHA-256 name in
`ledger/evidence/statements/` with a JSON manifest. When the statement reports
an ending balance, `books propose` creates the next-day balance assertion with
an `evidence:` link. For dates after an account's
`statement_evidence_required_after`, an assertion counts as verified only if
the link, manifest, and retained source exist, the source hash matches, and the
account, date, and amount agree.

## Adding a PDF preset

A new institution parser needs a structural fixture (no real account numbers or
descriptions) and must pass the reconciliation rule above. Register it in
`bookkeeping/statements.py` and in the app's `PDF_PRESETS`.

## Imports from other accounting systems

Statement imports already work without Xero: bank/card CSV, OFX/QFX/QBO,
MT940, PayPal activity CSV, and the supported PDF preset. This imports activity
for a registered account; it does not recreate a complete accounting company.
QBO means QuickBooks **Web Connect bank activity**, not a QuickBooks Online
company backup.

The accounting-history workflow supports Xero through its existing backup and
conversion tools, plus mapped journal-history imports from QuickBooks Online
and Wave. CSV/XLSX journal files are supported; Wave accounting ZIPs allow
explicit selection of a CSV/XLSX member. Native column names are not assumed.
The operator must supply stable journal grouping, map accounts, and enter source
report totals. See [the file migration contract](FILE_MIGRATIONS.md).

QuickBooks and Wave file migrations cover the supplied ledger history only.
Open invoices, bills, contacts, attachments and other company records are not
recreated. Verification checks the supplied export and resulting postings;
it does not prove export completeness. Use a source report for the same date
range, accounting basis and currency. Foreign-currency conversion is refused.
