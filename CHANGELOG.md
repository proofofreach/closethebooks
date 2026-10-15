# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-10-15 — Early access

First release as a standalone, company-neutral product, named
CloseTheBooks (distribution `closethebooks`, command `books`). Tested on
Python 3.11–3.14 on Linux, macOS, and Windows. Plaid bank feeds are
experimental.

### Added

- Workspaces: every company-specific fact lives in `books.toml`.
  `books init` creates a workspace and `books demo` a fictional one.
- Local web app (`books serve`): book health and next actions, a 12-month
  coverage strip per account, financial snapshot, statement import and
  proposals, accounts, invoices, read-only Ask, settings, and detailed
  reports through Fava at `/reports/`.
- Guided move from Xero: connect, back up (resumable, with in-place change
  fetching and a progress meter), merge the General Ledger export, convert,
  verify to the cent, and register bank and card accounts.
- Bookkeeping agent instructions and skills installed into each workspace.
- Commands: `init`, `demo`, `serve`, `add-account`, `check`, `query`,
  `status`, `propose`, `fetch`, `invoice`, `report`, `agent-files`, and the
  Xero migration commands.

### Changed

- Reports, status, proposals, and statement evidence follow the workspace's
  currency and account roles instead of fixed account names.
- Statement requests start after the last verified balance.
- Xero sign-in uses Xero's granular read scopes.

### Fixed

- Invoice numbers already recorded in the ledger are never reused.
- Windows: dates no longer use the Unix-only `%-d` directive, and
  `--statement KEY=C:\path` keeps the drive letter.
- Attachments found after the records phase now reach the attachments
  manifest that conversion reads.
