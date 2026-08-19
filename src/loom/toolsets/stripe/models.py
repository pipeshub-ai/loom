"""Typed rows for the Stripe toolset.

Every model carries Stripe's own field names where they are unambiguous and
renames only where the API's name would mislead a reader — ``amount`` is in the
*smallest currency unit*, so it is ``amount_cents`` here and the docstring says
so once rather than in every call site that divides by a hundred.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "StripeCharge",
    "StripeCustomer",
    "StripeEvent",
    "StripeInvoice",
    "StripePaymentIntent",
    "StripeRefund",
]


def _moment(value: Any) -> datetime | None:
    """A Stripe unix timestamp as an aware datetime."""
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(float(value), tz=UTC)


class StripeCustomer(BaseModel):
    """A customer record."""

    id: str
    email: str = ""
    name: str = ""
    description: str = ""
    currency: str = ""
    created: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    livemode: bool = False
    """Whether this came from the live account rather than test mode.

    Carried because a workflow that syncs test-mode customers into a production
    ledger is a mistake nothing else here would catch."""

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> StripeCustomer:
        return cls(
            id=str(body.get("id", "")),
            email=body.get("email") or "",
            name=body.get("name") or "",
            description=body.get("description") or "",
            currency=body.get("currency") or "",
            created=_moment(body.get("created")),
            metadata={k: str(v) for k, v in (body.get("metadata") or {}).items()},
            livemode=bool(body.get("livemode")),
        )


class StripeCharge(BaseModel):
    """A completed (or attempted) charge."""

    id: str
    amount_cents: int = 0
    """The **smallest currency unit** — 1000 is £10.00, and ¥1000 is ¥1000.

    Stripe calls this ``amount``; the name here is the whole warning, because
    dividing by 100 for a zero-decimal currency is a real bug that produces a
    plausible number."""

    currency: str = ""
    paid: bool = False
    refunded: bool = False
    status: str = ""
    """``succeeded``, ``pending``, or ``failed``."""

    customer_id: str = ""
    description: str = ""
    receipt_url: str = ""
    created: datetime | None = None
    livemode: bool = False

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> StripeCharge:
        customer = body.get("customer")
        return cls(
            id=str(body.get("id", "")),
            amount_cents=int(body.get("amount") or 0),
            currency=body.get("currency") or "",
            paid=bool(body.get("paid")),
            refunded=bool(body.get("refunded")),
            status=body.get("status") or "",
            customer_id=customer if isinstance(customer, str) else "",
            description=body.get("description") or "",
            receipt_url=body.get("receipt_url") or "",
            created=_moment(body.get("created")),
            livemode=bool(body.get("livemode")),
        )


class StripePaymentIntent(BaseModel):
    """A payment, from creation through to settlement."""

    id: str
    amount_cents: int = 0
    """Smallest currency unit — see :class:`StripeCharge`."""

    amount_received_cents: int = 0
    currency: str = ""
    status: str = ""
    """``requires_payment_method``, ``processing``, ``succeeded``, ``canceled``…

    Only ``succeeded`` means the money moved. A workflow that files a receipt
    on any other status has filed a receipt for a payment that did not
    happen."""

    customer_id: str = ""
    description: str = ""
    latest_charge_id: str = ""
    created: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    livemode: bool = False

    @property
    def settled(self) -> bool:
        """Whether the money actually moved."""
        return self.status == "succeeded"

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> StripePaymentIntent:
        customer = body.get("customer")
        charge = body.get("latest_charge")
        return cls(
            id=str(body.get("id", "")),
            amount_cents=int(body.get("amount") or 0),
            amount_received_cents=int(body.get("amount_received") or 0),
            currency=body.get("currency") or "",
            status=body.get("status") or "",
            customer_id=customer if isinstance(customer, str) else "",
            description=body.get("description") or "",
            latest_charge_id=charge if isinstance(charge, str) else "",
            created=_moment(body.get("created")),
            metadata={k: str(v) for k, v in (body.get("metadata") or {}).items()},
            livemode=bool(body.get("livemode")),
        )


class StripeInvoice(BaseModel):
    """An invoice."""

    id: str
    number: str = ""
    customer_id: str = ""
    status: str = ""
    """``draft``, ``open``, ``paid``, ``uncollectible``, or ``void``."""

    total_cents: int = 0
    amount_paid_cents: int = 0
    currency: str = ""
    hosted_invoice_url: str = ""
    created: datetime | None = None
    livemode: bool = False

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> StripeInvoice:
        customer = body.get("customer")
        return cls(
            id=str(body.get("id", "")),
            number=body.get("number") or "",
            customer_id=customer if isinstance(customer, str) else "",
            status=body.get("status") or "",
            total_cents=int(body.get("total") or 0),
            amount_paid_cents=int(body.get("amount_paid") or 0),
            currency=body.get("currency") or "",
            hosted_invoice_url=body.get("hosted_invoice_url") or "",
            created=_moment(body.get("created")),
            livemode=bool(body.get("livemode")),
        )


class StripeRefund(BaseModel):
    """Money sent back."""

    id: str
    amount_cents: int = 0
    currency: str = ""
    status: str = ""
    """``pending``, ``succeeded``, ``failed``, or ``canceled``."""

    charge_id: str = ""
    payment_intent_id: str = ""
    reason: str = ""
    created: datetime | None = None

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> StripeRefund:
        charge = body.get("charge")
        intent = body.get("payment_intent")
        return cls(
            id=str(body.get("id", "")),
            amount_cents=int(body.get("amount") or 0),
            currency=body.get("currency") or "",
            status=body.get("status") or "",
            charge_id=charge if isinstance(charge, str) else "",
            payment_intent_id=intent if isinstance(intent, str) else "",
            reason=body.get("reason") or "",
            created=_moment(body.get("created")),
        )


class StripeEvent(BaseModel):
    """One thing that happened, as Stripe records it.

    The same shape a webhook delivers and the API returns, which is what lets a
    workflow verify a delivery by *re-reading it from Stripe* rather than
    trusting the body it was handed.
    """

    id: str
    type: str = ""
    """``payment_intent.succeeded``, ``customer.created``, and so on."""

    created: datetime | None = None
    livemode: bool = False
    api_version: str = ""
    data_object: dict[str, Any] = Field(default_factory=dict)
    """The object the event is about — ``data.object`` in Stripe's envelope."""

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> StripeEvent:
        return cls(
            id=str(body.get("id", "")),
            type=body.get("type") or "",
            created=_moment(body.get("created")),
            livemode=bool(body.get("livemode")),
            api_version=body.get("api_version") or "",
            data_object=dict((body.get("data") or {}).get("object") or {}),
        )
