from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    key: str
    path: str
    response_key: str
    entity_id_field: str | None
    supports_attachments: bool = False
    attachment_entity_type: str | None = None
    pagination_mode: str = "page"
    page_size: int = 100
    optional: bool = False


ENDPOINTS = (
    EndpointSpec(
        key="journals",
        path="/Journals",
        response_key="Journals",
        entity_id_field="JournalID",
        pagination_mode="offset",
        page_size=100,
        optional=True,
    ),
    EndpointSpec(
        key="invoices",
        path="/Invoices",
        response_key="Invoices",
        entity_id_field="InvoiceID",
        supports_attachments=True,
        attachment_entity_type="Invoices",
    ),
    EndpointSpec(
        key="bank_transactions",
        path="/BankTransactions",
        response_key="BankTransactions",
        entity_id_field="BankTransactionID",
        supports_attachments=True,
        attachment_entity_type="BankTransactions",
    ),
    EndpointSpec(
        key="receipts",
        path="/Receipts",
        response_key="Receipts",
        entity_id_field="ReceiptID",
        supports_attachments=True,
        attachment_entity_type="Receipts",
        optional=True,
    ),
    EndpointSpec(
        key="expense_claims",
        path="/ExpenseClaims",
        response_key="ExpenseClaims",
        entity_id_field="ExpenseClaimID",
        pagination_mode="none",
        optional=True,
    ),
    EndpointSpec(
        key="contacts",
        path="/Contacts",
        response_key="Contacts",
        entity_id_field="ContactID",
    ),
    EndpointSpec(
        key="accounts",
        path="/Accounts",
        response_key="Accounts",
        entity_id_field="AccountID",
        pagination_mode="none",
    ),
)

ENDPOINT_MAP = {endpoint.key: endpoint for endpoint in ENDPOINTS}
