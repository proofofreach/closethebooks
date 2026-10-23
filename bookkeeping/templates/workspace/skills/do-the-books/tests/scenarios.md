# Fixed scenarios

1. “Post reviewed 2027 proposals; main includes `years/*.beancount`.” Expected: create/write the 2027 file without adding an explicit include; run ledger validation.
2. “Post reviewed 2027 proposals; main explicitly includes `years/2027.beancount`.” Expected: reuse that include; do not duplicate it.
3. “Post reviewed 2027 proposals; main only includes `years/2026.beancount`.” Expected: create the 2027 file and add its missing include; run ledger validation.
