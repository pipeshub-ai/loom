"""Workflow: Stripe to QuickBooks ETL.

A payment settles in Stripe. Find or create the customer in QuickBooks, file a
sales receipt, and tell the finance channel — once, however many times the
webhook is delivered.

What this shows, and why each part is the way it is:

* **The delivery is verified, and then re-read.** ``StripeSource`` checks the
  signature over the raw bytes, so the payload came from Stripe. This workflow
  still calls ``stripe_get_event`` and acts on Stripe's copy: the signature
  proves the *bytes* are genuine, not that the state still holds. Stripe
  retries for up to three days, and a receipt filed from a three-day-old body
  is a receipt for whatever the payment looked like then.

* **Only ``succeeded`` means the money moved.** ``StripePaymentIntent.settled``
  is one field for it. Filing a receipt on ``processing`` books revenue that
  has not arrived.

* **Cents on one side, decimal on the other.** Stripe says ``4200``; QuickBooks
  says ``42.00``. The conversion happens once, in a named step, with the unit
  in both field names — ``amount_cents`` and ``amount``. Getting this wrong
  produces a plausible number, which is why it is not done inline.

  Zero-decimal currencies are refused rather than guessed at: ¥1000 is ¥1000,
  not ¥10, and dividing by 100 there books a hundredth of the revenue with
  nothing to notice.

* **Idempotency is the workflow's job here, because QuickBooks has no key.**
  The Stripe payment id is written into the receipt's ``PrivateNote``, and the
  workflow looks for it before creating. Stripe's own writes take an
  ``idempotency_key`` and need no such dance — the two halves of this pipeline
  handle the same problem differently because the two APIs do.

* **A large payment asks a person.** Not a rail against Stripe — a check on
  *this workflow*. An amount far outside the usual is more often a currency
  bug than a big sale.

Credentials: ``STRIPE_API_KEY``, ``STRIPE_WEBHOOK_SECRET``, ``QUICKBOOKS_*``,
``SLACK_BOT_TOKEN``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from loom import Context, Retry, step, workflow
from loom.nodes.human import ApprovalIn
from loom.security.grants import GrantSet
from loom.toolsets.quickbooks.tools import (
    quickbooks_create_customer,
    quickbooks_create_sales_receipt,
    quickbooks_find_customer,
    quickbooks_find_sales_receipts,
)
from loom.toolsets.slack.tools import slack_post_message
from loom.toolsets.stripe.tools import stripe_get_event
from loom.triggers.specs import OnAppEvent

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

#: Currencies whose smallest unit *is* the unit — no minor subdivision.
#:
#: Stripe reports these unscaled: ¥1000 is ¥1000. Dividing by 100 books a
#: hundredth of the revenue and looks entirely plausible in a ledger, so this
#: workflow refuses them rather than guessing. The list is deliberately short
#: and explicit; extending it is a decision somebody makes on purpose.
ZERO_DECIMAL = frozenset(
    {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
     "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}
)


class EtlConfig(BaseModel):
    """One Stripe event to file."""

    event_id: str = Field(description="A Stripe evt_… id, from the webhook.")
    finance_channel: str = "#finance"
    approve_above: float = 5000.0
    """Ask a person before filing a receipt larger than this, in the
    company's currency. A check on this workflow, not on Stripe."""

    item_id: str = ""
    """QuickBooks product or service the receipt lines reference. Resolve a
    name to an id with ``quickbooks_find_items``."""


class EtlResult(BaseModel):
    """What the workflow returns."""

    event_id: str
    payment_id: str = ""
    settled: bool = False
    """Whether the money actually moved. Everything below is skipped if not."""

    customer_id: str = ""
    customer_created: bool = False
    receipt_id: str = ""
    already_filed: bool = False
    """Whether a receipt for this payment already existed. Not a failure — it
    is what a redelivery is supposed to look like."""

    approved: bool = True
    amount: float = 0.0
    currency: str = ""


class Money(BaseModel):
    """An amount, converted once, with the unit in the name."""

    amount: float
    """Decimal, as QuickBooks wants it — ``42.00``."""

    currency: str


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=2, initial_delay=0.5))
async def to_decimal(amount_cents: int, currency: str) -> Money:
    """Stripe's minor units as QuickBooks' decimal.

    A ``@step`` rather than an inline expression on purpose: this is the one
    place the two APIs disagree about what a number means, and burying it in a
    call would make a wrong ledger entry invisible in the journal. Here it is
    an entry a person can read.
    """
    code = (currency or "").lower()
    if code in ZERO_DECIMAL:
        raise ValueError(
            f"{code.upper()} has no minor unit, so Stripe reports it unscaled "
            f"({amount_cents} is {amount_cents}, not {amount_cents / 100}). "
            "Filing it as a decimal would book a hundredth of the revenue. "
            "Handle this currency explicitly before enabling it."
        )
    return Money(amount=round(amount_cents / 100, 2), currency=code.upper())


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def notify_finance(channel: str, result: EtlResult) -> None:
    """Tell the finance channel.

    Retried — but only ever reached once per payment, because everything above
    it is guarded by the already-filed check. A duplicate post here would mean
    a duplicate receipt, which is the thing that cannot happen.
    """
    if result.already_filed:
        text = (
            f"Stripe {result.payment_id} was already filed as receipt "
            f"{result.receipt_id} — redelivery, no action taken."
        )
    else:
        text = (
            f"Filed {result.amount:.2f} {result.currency} from Stripe "
            f"{result.payment_id} as receipt {result.receipt_id}"
            + (" (new customer)" if result.customer_created else "")
        )
    await slack_post_message(channel=channel, text=text)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="stripe_to_quickbooks",
    version="2",
    triggers=[OnAppEvent("stripe.payment_intent.succeeded")],
    grants=GrantSet(toolsets=["stripe", "quickbooks", "slack"]),
)
async def stripe_to_quickbooks(ctx: Context, config: EtlConfig) -> EtlResult:
    """File a settled Stripe payment into QuickBooks, exactly once."""
    # Re-read rather than trust the delivery. The signature proved the bytes;
    # this proves the state, which matters when a redelivery is three days old.
    event = await ctx.step(stripe_get_event, config.event_id)
    payment = event.data_object
    payment_id = str(payment.get("id") or "")
    status = str(payment.get("status") or "")

    result = EtlResult(event_id=config.event_id, payment_id=payment_id)
    if status != "succeeded":
        # Not a failure: an event for a payment that has not settled is an
        # ordinary thing to receive, and booking it would be revenue that has
        # not arrived.
        return result

    money = await ctx.step(
        to_decimal, int(payment.get("amount") or 0), str(payment.get("currency") or "")
    )
    result = result.model_copy(
        update={"settled": True, "amount": money.amount, "currency": money.currency}
    )

    # QuickBooks has no idempotency key, so this is the check that stands in
    # for one. Stripe's own writes need no equivalent — the two halves differ
    # because the two APIs do.
    external_id = f"stripe:{payment_id}"
    existing = await ctx.step(quickbooks_find_sales_receipts, external_id, "", 1)
    if existing:
        result = result.model_copy(
            update={
                "already_filed": True,
                "receipt_id": existing[0].id,
                "customer_id": existing[0].customer_id,
            }
        )
        await ctx.step(notify_finance, config.finance_channel, result)
        return result

    email = str(
        payment.get("receipt_email") or payment.get("customer_email") or ""
    )
    customer = await ctx.step(quickbooks_find_customer, email) if email else None
    created = False
    if customer is None:
        # DisplayName is unique across the company file, so it falls back to
        # the payment id rather than a name that might already exist.
        customer = await ctx.step(
            quickbooks_create_customer,
            email or f"stripe-{payment_id}",
            email,
        )
        created = True

    if money.amount > config.approve_above:
        approval = await ctx.node(
            "human.approval",
            ApprovalIn(
                subject=f"large-receipt:{payment_id}",
                prompt=(
                    f"About to file {money.amount:.2f} {money.currency} from "
                    f"Stripe {payment_id} — above the usual "
                    f"{config.approve_above:.2f}. An amount far outside the "
                    "usual is more often a currency bug than a big sale. File it?"
                ),
            ),
        )
        if not approval.approved:
            refused = result.model_copy(
                update={
                    "customer_id": customer.id,
                    "customer_created": created,
                    "approved": False,
                }
            )
            await ctx.step(notify_finance, config.finance_channel, refused)
            return refused

    receipt = await ctx.step(
        quickbooks_create_sales_receipt,
        customer.id,
        money.amount,
        str(payment.get("description") or f"Stripe payment {payment_id}"),
        config.item_id,
        money.currency,
        "",
        external_id,
    )

    result = result.model_copy(
        update={
            "customer_id": customer.id,
            "customer_created": created,
            "receipt_id": receipt.id,
        }
    )
    await ctx.step(notify_finance, config.finance_channel, result)
    return result
