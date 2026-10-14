# Statement formats

Statements are the dependable import path and the only source of verified
balances. Each account source lists the formats it accepts
(`statement_extensions`); the Import page and dropzones enforce that list.

| Format | Extensions | Notes |
| --- | --- | --- |
| CSV | `.csv` | Column names are matched by alias (date, description/payee, amount or debit/credit, balance). The `paypal` preset handles PayPal activity exports. |
| OFX family | `.ofx`, `.qfx`, `.qbo` | Quicken, QuickBooks Web Connect, and OFX downloads. Most US banks and card issuers offer one of these. Preferred when available: stable transaction IDs and a ledger balance. |
| MT940 | `.mt940`, `.sta`, `.swift` | SWIFT bank statements, common outside the US. |
| PDF | `.pdf` | Only with an institution parser preset (`quickbooks-checking` today). |

When a bank offers several formats, choose OFX/QFX/QBO first, then CSV. Use
PDF only when nothing machine-readable exists.

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
