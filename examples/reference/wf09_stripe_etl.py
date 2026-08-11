"""Workflow: Stripe to QuickBooks ETL."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from workflow_builder import Context, OnError, Retry, step, workflow

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class StripePayment(BaseModel):
    """Incoming Stripe payment event."""

    payment_id: str
    customer_email: str
    customer_name: str
    amount_cents: int
    currency: str = "usd"
    description: str = ""
    idempotency_key: str = ""


class QuickBooksCustomer(BaseModel):
    """A customer record in QuickBooks."""

    customer_id: str
    display_name: str
    email: str
    is_new: bool = False


class SalesReceipt(BaseModel):
    """A QuickBooks sales receipt."""

    receipt_id: str
    customer_id: str
    amount_cents: int
    currency: str
    memo: str = ""


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(
    retry=Retry(max_attempts=3, delay=2.0),
    on_error=OnError.CONTINUE,
    fallback=None,
)
async def lookup_customer(
    email: str,
    qb_api_key: str,
) -> QuickBooksCustomer | None:
    """Look up a customer in QuickBooks by email.

    Args:
        email: Customer email to search for.
        qb_api_key: QuickBooks API key.

    Returns:
        Customer record if found, None otherwise.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://quickbooks.example.com/api/customers",
            params={"email": email},
            headers={"Authorization": f"Bearer {qb_api_key}"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()

    if not data.get("customers"):
        return None

    cust = data["customers"][0]
    return QuickBooksCustomer(
        customer_id=cust["id"],
        display_name=cust["display_name"],
        email=email,
    )


@step(retry=Retry(max_attempts=2, delay=1.0))
async def create_customer(
    name: str,
    email: str,
    qb_api_key: str,
) -> QuickBooksCustomer:
    """Create a new customer in QuickBooks.

    Args:
        name: Customer display name.
        email: Customer email.
        qb_api_key: QuickBooks API key.

    Returns:
        Newly created customer record.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://quickbooks.example.com/api/customers",
            headers={"Authorization": f"Bearer {qb_api_key}"},
            json={
                "display_name": name,
                "email": email,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return QuickBooksCustomer(
        customer_id=data.get("id", "new-cust-001"),
        display_name=name,
        email=email,
        is_new=True,
    )


@step(retry=Retry(max_attempts=3, delay=2.0))
async def create_sales_receipt(
    customer_id: str,
    amount_cents: int,
    currency: str,
    description: str,
    idempotency_key: str,
    qb_api_key: str,
) -> SalesReceipt:
    """Create a sales receipt in QuickBooks.

    Uses an idempotency key to prevent duplicate receipts
    on retry.

    Args:
        customer_id: QuickBooks customer ID.
        amount_cents: Payment amount in cents.
        currency: Three-letter currency code.
        description: Line item description.
        idempotency_key: Unique key for idempotent creation.
        qb_api_key: QuickBooks API key.

    Returns:
        The created sales receipt.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://quickbooks.example.com/api/receipts",
            headers={
                "Authorization": f"Bearer {qb_api_key}",
                "Idempotency-Key": idempotency_key,
            },
            json={
                "customer_id": customer_id,
                "amount": amount_cents,
                "currency": currency,
                "memo": description,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return SalesReceipt(
        receipt_id=data.get("id", "rcpt-001"),
        customer_id=customer_id,
        amount_cents=amount_cents,
        currency=currency,
        memo=description,
    )


@step(retry=Retry(max_attempts=2, delay=1.0))
async def notify_finance(
    receipt: SalesReceipt,
    customer: QuickBooksCustomer,
    payment_id: str,
) -> bool:
    """Send a Slack notification to the finance channel.

    Args:
        receipt: The created sales receipt.
        customer: The customer record.
        payment_id: Original Stripe payment ID.

    Returns:
        True if notification was sent.
    """
    amount = f"${receipt.amount_cents / 100:.2f}"
    new_tag = " (NEW)" if customer.is_new else ""
    message = (
        f"Payment {payment_id} processed: {amount} "
        f"{receipt.currency.upper()} from "
        f"{customer.display_name}{new_tag} "
        f"-> Receipt {receipt.receipt_id}"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            "https://slack.example.com/api/chat.postMessage",
            json={
                "channel": "#finance",
                "text": message,
            },
        )
    return True


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="stripe_to_quickbooks", version="1")
async def stripe_to_quickbooks(
    ctx: Context,
    payment: StripePayment,
) -> dict:
    """Sync a Stripe payment into QuickBooks.

    Pipeline: lookup customer -> create if missing -> receipt -> notify.
    Demonstrates conditional branching and idempotent effects.
    """
    qb_api_key = "qb-api-key-placeholder"

    # Look up existing customer (returns None if not found)
    customer = await ctx.step(
        lookup_customer, payment.customer_email, qb_api_key,
    )

    # Create customer if not found
    if customer is None:
        customer = await ctx.step(
            create_customer,
            payment.customer_name,
            payment.customer_email,
            qb_api_key,
        )

    # Create the sales receipt with idempotency key
    idem_key = payment.idempotency_key or payment.payment_id
    receipt = await ctx.step(
        create_sales_receipt,
        customer.customer_id,
        payment.amount_cents,
        payment.currency,
        payment.description,
        idem_key,
        qb_api_key,
    )

    # Notify finance team
    await ctx.step(
        notify_finance, receipt, customer, payment.payment_id,
    )

    return {
        "payment_id": payment.payment_id,
        "customer_id": customer.customer_id,
        "customer_is_new": customer.is_new,
        "receipt_id": receipt.receipt_id,
        "amount": f"${payment.amount_cents / 100:.2f}",
    }
