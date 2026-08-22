"""QuickBooks Online ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from loom.toolsets.quickbooks.models import (
    QuickBooksCustomer,
    QuickBooksInvoice,
    QuickBooksItem,
    QuickBooksPayment,
    QuickBooksSalesReceipt,
)

QUICKBOOKS_MANIFEST = ToolsetManifest(
    id="quickbooks",
    version="1.0.0",
    summary="QuickBooks Online — customers, sales receipts, invoices, payments.",
    description=(
        "Read and write one QuickBooks company file. A realm id names the "
        "company and is part of every request path, so it is required at "
        "construction; sandbox and production are different hosts, and a "
        "sandbox token against production authenticates and then finds "
        "nothing.\n\n"
        "Amounts are decimal — 42.00, not 4200. Stripe uses minor units, so a "
        "workflow bridging the two converts once.\n\n"
        "Every update sends a SyncToken, which is the value the record "
        "currently has. QuickBooks rejects a stale one rather than merging, so "
        "a write is always preceded by a read — the models carry sync_token "
        "for that reason.\n\n"
        "There is no idempotency key. Creating a customer or a receipt is "
        "therefore not retried, and the way to make a create safe across runs "
        "is to stamp an external id into PrivateNote and look for it first "
        "with quickbooks_find_sales_receipts."
    ),
    base_url="https://quickbooks.api.intuit.com",
    auth=AuthSpec(
        client="loom.toolsets.quickbooks.client:QuickBooksClient",
        # This client reads environment variables and no CredentialStore, so
        # `credential` is empty and no `provider` is declared: an OAuth flow
        # here would store a token the client never looks up. Adding a store
        # path is a change to the client, not to this manifest.
        # Intuit rotates the refresh token on every exchange, and a realm id names
        # one company file, so the client owns the exchange.
        kind="oauth2",
        fields=(
            AuthField(name="QUICKBOOKS_REALM_ID", arg="realm_id", label="Realm (company) id",
                      secret=False),
            AuthField(name="QUICKBOOKS_CLIENT_ID",
                      arg="client_id", label="Client id", secret=False),
            AuthField(name="QUICKBOOKS_CLIENT_SECRET", arg="client_secret", label="Client secret"),
            AuthField(name="QUICKBOOKS_REFRESH_TOKEN", arg="refresh_token", label="Refresh token"),
            AuthField(name="QUICKBOOKS_ACCESS_TOKEN",
                      arg="access_token", label="Ready-made access token",
                      required=False),
            AuthField(name="QUICKBOOKS_ENVIRONMENT", label="Sandbox or production",
                      arg="environment", secret=False, required=False,
                      example="production"),
        ),
    ),
    tools_module="loom.toolsets.quickbooks.tools",
    egress_hosts=[
        "quickbooks.api.intuit.com",
        "sandbox-quickbooks.api.intuit.com",
        "oauth.platform.intuit.com",
    ],
    rate_limits={
        "page": "1000 rows maximum per query; reads follow pages for you",
        "refresh_token": "revoked after 100 days unused, and rotated on every refresh",
    },
    groups={
        "customers": [
            OperationSpec(
                id="customers.find_by_email",
                function="quickbooks_find_customer",
                summary="Find one customer by exact email address.",
                description=(
                    "The resolver. Every write takes a numeric Id, and an "
                    "email passed where an id belongs matches nothing and "
                    "reports no error."
                ),
                effect=EffectClass.READ,
                scopes=["com.intuit.quickbooks.accounting"],
                idempotent=True,
                resolves="customer",
                output_schema=QuickBooksCustomer.model_json_schema(),
            ),
            OperationSpec(
                id="customers.get",
                function="quickbooks_get_customer",
                summary="Fetch one customer, with its current SyncToken.",
                effect=EffectClass.READ,
                scopes=["com.intuit.quickbooks.accounting"],
                idempotent=True,
                output_schema=QuickBooksCustomer.model_json_schema(),
            ),
            OperationSpec(
                id="customers.create",
                function="quickbooks_create_customer",
                summary="Create a customer. Not retried — there is no idempotency key.",
                description=(
                    "DisplayName must be unique across the company file; "
                    "QuickBooks rejects a duplicate rather than returning the "
                    "existing record, so look one up first."
                ),
                effect=EffectClass.WRITE,
                scopes=["com.intuit.quickbooks.accounting"],
                idempotent=False,
                output_schema=QuickBooksCustomer.model_json_schema(),
            ),
            OperationSpec(
                id="customers.update",
                function="quickbooks_update_customer",
                summary="Sparse-update a customer; only the fields passed change.",
                description=(
                    "Takes the SyncToken the record currently has. A stale one "
                    "is rejected, not merged, and fails identically however "
                    "often it is sent — re-read and decide again."
                ),
                effect=EffectClass.WRITE,
                scopes=["com.intuit.quickbooks.accounting"],
                idempotent=True,
                output_schema=QuickBooksCustomer.model_json_schema(),
            ),
        ],
        "sales": [
            OperationSpec(
                id="sales_receipts.create",
                function="quickbooks_create_sales_receipt",
                summary="Record money already received. Not retried.",
                description=(
                    "A receipt, not an invoice: an invoice asks for money and "
                    "leaves a receivable open, which against a customer who "
                    "has already paid is wrong in the ledger. Amount is "
                    "decimal. Put an external id in private_note so a later "
                    "run can tell whether this was already filed."
                ),
                effect=EffectClass.WRITE,
                scopes=["com.intuit.quickbooks.accounting"],
                idempotent=False,
                output_schema=QuickBooksSalesReceipt.model_json_schema(),
            ),
            OperationSpec(
                id="sales_receipts.find",
                function="quickbooks_find_sales_receipts",
                summary="Find receipts, optionally by the note an earlier run stamped.",
                description=(
                    "The idempotency check QuickBooks does not provide. Look "
                    "for the external id before creating a second receipt."
                ),
                effect=EffectClass.READ,
                scopes=["com.intuit.quickbooks.accounting"],
                idempotent=True,
                pagination=True,
                output_schema=QuickBooksSalesReceipt.model_json_schema(),
            ),
        ],
        "ledger": [
            OperationSpec(
                id="invoices.find",
                function="quickbooks_find_invoices",
                summary="List invoices, optionally only the unpaid ones.",
                effect=EffectClass.READ,
                scopes=["com.intuit.quickbooks.accounting"],
                idempotent=True,
                pagination=True,
                output_schema=QuickBooksInvoice.model_json_schema(),
            ),
            OperationSpec(
                id="payments.find",
                function="quickbooks_find_payments",
                summary="List payments received.",
                effect=EffectClass.READ,
                scopes=["com.intuit.quickbooks.accounting"],
                idempotent=True,
                pagination=True,
                output_schema=QuickBooksPayment.model_json_schema(),
            ),
            OperationSpec(
                id="items.find",
                function="quickbooks_find_items",
                summary="Products and services, by name.",
                description=(
                    "The resolver for a receipt line: it references an item by "
                    "id, and a name passed there matches nothing."
                ),
                effect=EffectClass.READ,
                scopes=["com.intuit.quickbooks.accounting"],
                idempotent=True,
                pagination=True,
                resolves="item",
                output_schema=QuickBooksItem.model_json_schema(),
            ),
        ],
    },
)
