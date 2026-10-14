---
name: make-invoice
description: Create a customer invoice for this workspace's company — generate the invoice document and record the receivable in the Beancount ledger. Use when the user wants to invoice a client, bill someone, or check open or unpaid invoices.
---

# Make an invoice

The company, currency, invoice prefix, and payment terms come from
`books.toml`. The receivable and sales income accounts come from its `[chart]`.

## Workflow

1. **Collect** only what is missing: the client name and address, line items
   with amounts, the invoice date (default today), and payment terms (default
   from `books.toml`). For a returning client, reuse the address and wording
   from their earlier invoices in `ledger/documents/` under the sales income
   account path.

2. **Generate.**
   ```bash
   books invoice --client "..." --address "line" --address "line" \
       --item "Description" --amount 0.00 [--item ... --amount ...] \
       [--date YYYY-MM-DD] [--due-days N]
   ```
   The command numbers the invoice (`<prefix>-YYYYMMDD-n`), writes the HTML
   document, and prints the ledger entry. The owner can also use the Invoices
   page in `books serve`. Open the document for the owner; they print it to
   PDF and send it. Never send it yourself.

3. **Record the receivable.** Append the printed entry to
   `ledger/years/<year>.beancount`. Run `books check`, show the diff, and
   commit when the owner approves.

4. **When payment arrives.** The deposit imported by do-the-books posts
   against the receivable account, not sales income.

## Open invoices

`books query "SELECT date, payee, narration, position WHERE account = '<receivable account>'"`
A nonzero receivable balance means unpaid invoices. List them with their age
when the owner asks what is outstanding.
