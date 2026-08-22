"""Stripe ToolsetManifest — pure metadata, no client import.

Output schemas come from the Pydantic models so the contract cannot drift from
what the tools return.
"""

from __future__ import annotations

from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from loom.toolsets.stripe.models import (
    StripeCharge,
    StripeCustomer,
    StripeEvent,
    StripeInvoice,
    StripePaymentIntent,
    StripeRefund,
)

STRIPE_MANIFEST = ToolsetManifest(
    id="stripe",
    version="1.0.0",
    summary="Stripe — customers, payments, invoices, refunds, and events.",
    description=(
        "Read and write a Stripe account. Amounts are always in the smallest "
        "currency unit: 1000 is £10.00 and ¥1000 is ¥1000, so dividing by 100 "
        "is wrong for zero-decimal currencies.\n\n"
        "Every write takes an `idempotency_key` and none of them invents one. "
        "Derive it from something the run already has — a payment intent id, "
        "an invoice number, ctx.run_id — so a retried step sends the same key "
        "and Stripe replays its original response instead of charging again. "
        "Reusing a key with different parameters is refused outright.\n\n"
        "Only `status == 'succeeded'` on a payment means the money moved; "
        "filing a receipt on any other status files one for a payment that did "
        "not happen. `StripePaymentIntent.settled` says it in one field.\n\n"
        "A declined card is classified non-retryable: it will decline again, "
        "and the decline `code` is the actionable part."
    ),
    base_url="https://api.stripe.com/v1",
    auth=AuthSpec(
        client="loom.toolsets.stripe.client:StripeClient",
        # This client reads environment variables and no CredentialStore, so
        # `credential` is empty and no `provider` is declared: an OAuth flow
        # here would store a token the client never looks up. Adding a store
        # path is a change to the client, not to this manifest.
        kind="bearer",
        fields=(
            AuthField(name="STRIPE_API_KEY", arg="api_key", label="Secret API key",
                      example="sk_live_…"),
            AuthField(name="STRIPE_ACCOUNT", arg="account", label="Connected account id",
                      secret=False, required=False, example="acct_…"),
        ),
    ),
    tools_module="loom.toolsets.stripe.tools",
    opaque_ids={r"\bcus_[A-Za-z0-9]{10,}\b": "customer"},
    egress_hosts=["api.stripe.com"],
    rate_limits={
        "page": "100 objects maximum per request; reads follow pages for you",
        "events": "kept for 30 days, so a missed webhook is recoverable by reading",
        "idempotency": "a key's response is replayed for 24 hours",
    },
    groups={
        "customers": [
            OperationSpec(
                id="customers.find_by_email",
                function="stripe_find_customers",
                summary="Find customers by exact email address.",
                description=(
                    "The resolver. Every write takes a cus_… id, and an email "
                    "passed where an id belongs matches nothing and returns an "
                    "empty list rather than an error. Stripe matches email "
                    "exactly and case-sensitively and does not enforce "
                    "uniqueness, so this returns a list and choosing between "
                    "results is the workflow's decision."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                resolves="customer",
                output_schema=StripeCustomer.model_json_schema(),
            ),
            OperationSpec(
                id="customers.get",
                function="stripe_get_customer",
                summary="Fetch one customer by id.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=StripeCustomer.model_json_schema(),
            ),
            OperationSpec(
                id="customers.create",
                function="stripe_create_customer",
                summary="Create a customer.",
                description=(
                    "Takes an idempotency_key. Retried safely because of it — "
                    "the same key replays Stripe's original response instead "
                    "of creating a second customer."
                ),
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=StripeCustomer.model_json_schema(),
            ),
            OperationSpec(
                id="customers.update",
                function="stripe_update_customer",
                summary="Change a customer; only the fields passed are touched.",
                description=(
                    "Needs no idempotency key: setting the same fields twice "
                    "reaches the same end state."
                ),
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=StripeCustomer.model_json_schema(),
            ),
        ],
        "payments": [
            OperationSpec(
                id="payments.get",
                function="stripe_get_payment_intent",
                summary="Fetch one payment by id.",
                description=(
                    "Check .settled before acting: only status == 'succeeded' "
                    "means the money moved."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=StripePaymentIntent.model_json_schema(),
            ),
            OperationSpec(
                id="payments.list",
                function="stripe_list_payment_intents",
                summary="List payments, newest first.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=StripePaymentIntent.model_json_schema(),
            ),
            OperationSpec(
                id="charges.list",
                function="stripe_list_charges",
                summary="List charges, newest first.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=StripeCharge.model_json_schema(),
            ),
        ],
        "invoices": [
            OperationSpec(
                id="invoices.get",
                function="stripe_get_invoice",
                summary="Fetch one invoice by id.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=StripeInvoice.model_json_schema(),
            ),
            OperationSpec(
                id="invoices.list",
                function="stripe_list_invoices",
                summary="List invoices, newest first, optionally by status.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=StripeInvoice.model_json_schema(),
            ),
        ],
        "refunds": [
            OperationSpec(
                id="refunds.create",
                function="stripe_create_refund",
                summary="Refund a payment, in whole or in part.",
                description=(
                    "Moves money back to a customer. Takes exactly one of "
                    "payment_intent_id or charge_id, and an idempotency_key "
                    "without which a retried step refunds twice. amount_cents "
                    "is the smallest currency unit; zero refunds the full "
                    "amount."
                ),
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema=StripeRefund.model_json_schema(),
            ),
        ],
        "events": [
            OperationSpec(
                id="events.get",
                function="stripe_get_event",
                summary="Re-read one event from Stripe by id.",
                description=(
                    "The half of webhook handling that does not rest on "
                    "trusting the request body: verify the signature, then "
                    "read the event back and act on what Stripe says."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=StripeEvent.model_json_schema(),
            ),
            OperationSpec(
                id="events.list",
                function="stripe_list_events",
                summary="List recent events, newest first.",
                description=(
                    "The recovery path when an endpoint was down: read what "
                    "was missed rather than waiting for a redelivery."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=StripeEvent.model_json_schema(),
            ),
        ],
    },
)
