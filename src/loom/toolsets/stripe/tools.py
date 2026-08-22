"""Stripe step functions for use inside LOOM workflows.

Each is a ``@step``, so it journals, retries per its own policy, and can be
called with ``ctx.step(...)``::

    from loom.toolsets.stripe.tools import stripe_find_customers, stripe_create_refund

    found = await ctx.step(stripe_find_customers, email="ada@example.com")

**Every write takes an ``idempotency_key`` and none of them invents one.** That
is the whole point of the parameter: a key minted inside the tool would be new
on each attempt, which is precisely the case Stripe's idempotency exists to
prevent. Derive it from something the *run* already has — a payment intent id,
an invoice number, ``ctx.run_id`` — so a retried step and a replayed run send
the same key and Stripe replays its original answer instead of charging again.

Because the key makes them safe, the writes here *do* retry. That is the
opposite of the rule for Slack and Gmail, and it is the same reasoning: retry
what a duplicate cannot hurt, and Stripe's idempotency is what makes it not
hurt.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.factory import client_for
from loom.toolsets.pagination import Results
from loom.toolsets.stripe.client import StripeClient
from loom.toolsets.stripe.models import (
    StripeCharge,
    StripeCustomer,
    StripeEvent,
    StripeInvoice,
    StripePaymentIntent,
    StripeRefund,
)

#: Reads. A declined card and a bad parameter are classified non-retryable in
#: the client, so this budget is spent only on Stripe's own failures.
_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Writes. Safe to retry **only** because every one of them carries an
#: idempotency key — Stripe replays the original response rather than acting
#: twice.
_WRITE = Retry(max_attempts=3, initial_delay=1.0)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def stripe_find_customers(email: str, limit: int = 10) -> Results[StripeCustomer]:
    """Find customers by email address.

    The resolver: every Stripe write takes a ``cus_…`` id, and an email passed
    where an id belongs matches nothing and returns an empty list rather than
    an error.

    Stripe matches email **exactly and case-sensitively**, and does not enforce
    uniqueness — so this returns a list. More than one result is a real state,
    not a bug, and choosing between them is the workflow's decision.

    Args:
        email: The exact address to match.
        limit: Most customers to return.
    """
    client = await client_for("stripe", StripeClient)
    return await client.find_customers_by_email(email, limit=limit)


@step(retry=_READ)
async def stripe_get_customer(customer_id: str) -> StripeCustomer:
    """Fetch one customer by id.

    Args:
        customer_id: A ``cus_…`` id.
    """
    return await (await client_for("stripe", StripeClient)).get_customer(customer_id)


@step(retry=_WRITE)
async def stripe_create_customer(
    idempotency_key: str,
    email: str = "",
    name: str = "",
    description: str = "",
    metadata: dict[str, str] | None = None,
) -> StripeCustomer:
    """Create a customer.

    Args:
        idempotency_key: Something stable for this customer in this run — an
            order id, or ``f"{ctx.run_id}:customer"``. Reusing it replays
            Stripe's original response instead of creating a second customer.
            Reusing it with *different* parameters is refused outright.
        email: Their email address.
        name: Their name.
        description: Free text shown in the dashboard.
        metadata: Your own key-value pairs, carried on the record.
    """
    return await (await client_for("stripe", StripeClient)).create_customer(
        idempotency_key=idempotency_key,
        email=email,
        name=name,
        description=description,
        metadata=metadata,
    )


@step(retry=_WRITE)
async def stripe_update_customer(
    customer_id: str, values: dict[str, object]
) -> StripeCustomer:
    """Change a customer. Only the fields passed are touched.

    Needs no idempotency key: setting the same fields twice reaches the same
    end state, which is what makes an update different from a create.

    Args:
        customer_id: A ``cus_…`` id.
        values: Stripe field names to new values, e.g. ``{"name": "Ada"}``.
    """
    client = await client_for("stripe", StripeClient)
    return await client.update_customer(customer_id, values=values)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def stripe_get_payment_intent(intent_id: str) -> StripePaymentIntent:
    """Fetch one payment by id.

    Check ``.settled`` before acting on it: only ``status == "succeeded"``
    means the money moved, and filing a receipt on any other status files a
    receipt for a payment that did not happen.

    Args:
        intent_id: A ``pi_…`` id.
    """
    return await (await client_for("stripe", StripeClient)).get_payment_intent(intent_id)


@step(retry=_READ)
async def stripe_list_payment_intents(
    customer_id: str = "", limit: int = 25
) -> Results[StripePaymentIntent]:
    """List payments, newest first, optionally for one customer.

    Args:
        customer_id: A ``cus_…`` id, or empty for the whole account.
        limit: Most payments to return, following pages as needed.
    """
    return await (await client_for("stripe", StripeClient)).list_payment_intents(
        customer_id=customer_id, limit=limit
    )


@step(retry=_READ)
async def stripe_list_charges(customer_id: str = "", limit: int = 25) -> Results[StripeCharge]:
    """List charges, newest first, optionally for one customer.

    Args:
        customer_id: A ``cus_…`` id, or empty for the whole account.
        limit: Most charges to return, following pages as needed.
    """
    client = await client_for("stripe", StripeClient)
    return await client.list_charges(customer_id=customer_id, limit=limit)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def stripe_get_invoice(invoice_id: str) -> StripeInvoice:
    """Fetch one invoice by id.

    Args:
        invoice_id: An ``in_…`` id.
    """
    return await (await client_for("stripe", StripeClient)).get_invoice(invoice_id)


@step(retry=_READ)
async def stripe_list_invoices(
    customer_id: str = "", status: str = "", limit: int = 25
) -> Results[StripeInvoice]:
    """List invoices, newest first.

    Args:
        customer_id: A ``cus_…`` id, or empty for the whole account.
        status: ``draft``, ``open``, ``paid``, ``uncollectible``, or ``void``.
            Empty returns every status.
        limit: Most invoices to return, following pages as needed.
    """
    return await (await client_for("stripe", StripeClient)).list_invoices(
        customer_id=customer_id, status=status, limit=limit
    )


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


@step(retry=_WRITE)
async def stripe_create_refund(
    idempotency_key: str,
    payment_intent_id: str = "",
    charge_id: str = "",
    amount_cents: int = 0,
    reason: str = "",
) -> StripeRefund:
    """Refund a payment, in whole or in part.

    Args:
        idempotency_key: Something stable for this refund — the payment intent
            id is usually right. Without a stable key a retried step refunds
            twice.
        payment_intent_id: A ``pi_…`` id. Exactly one of this or *charge_id*.
        charge_id: A ``ch_…`` id. Exactly one of this or *payment_intent_id*.
        amount_cents: How much, in the **smallest currency unit** — 1000 is
            £10.00 and ¥1000 is ¥1000. Zero refunds the full amount.
        reason: ``duplicate``, ``fraudulent``, or ``requested_by_customer``.
    """
    return await (await client_for("stripe", StripeClient)).create_refund(
        idempotency_key=idempotency_key,
        payment_intent_id=payment_intent_id,
        charge_id=charge_id,
        amount_cents=amount_cents,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def stripe_get_event(event_id: str) -> StripeEvent:
    """Re-read an event from Stripe by id.

    The half of webhook handling that does not rest on trusting the request
    body: verify the signature, then read the event back and act on what Stripe
    says rather than on what arrived. Events are kept for 30 days.

    Args:
        event_id: An ``evt_…`` id.
    """
    return await (await client_for("stripe", StripeClient)).get_event(event_id)


@step(retry=_READ)
async def stripe_list_events(event_type: str = "", limit: int = 25) -> Results[StripeEvent]:
    """List recent events, newest first.

    The recovery path when a webhook endpoint was down: read what was missed
    rather than waiting for a redelivery that only covers some failures.

    Args:
        event_type: e.g. ``payment_intent.succeeded``. Empty returns all types.
        limit: Most events to return, following pages as needed.
    """
    client = await client_for("stripe", StripeClient)
    return await client.list_events(event_type=event_type, limit=limit)
