# Changes

- Clarified year include coverage: an existing `years/*.beancount` glob already includes new years. Reason: prevent duplicate-file errors after file migration. Verified against the real future-account/year migration E2E scenario and existing include semantics. Kept; this is a narrow wording correction, not a procedure rewrite.
