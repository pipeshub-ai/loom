"""Typed rows for the QuickBooks Online toolset.

Every model carries ``sync_token`` where QuickBooks has one, because an update
without the *current* token is rejected — see
:class:`~loom.toolsets.quickbooks.client.QuickBooksStaleObject`. Dropping it
from the read makes the write impossible, so it travels on the model rather
than being fetched again at the point of use.

**Amounts here are decimal, not minor units.** QuickBooks says ``42.00`` where
Stripe says ``4200``. A workflow bridging the two converts once, deliberately;
the field names on both sides are what make the difference visible.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "QuickBooksCustomer",
    "QuickBooksInvoice",
    "QuickBooksItem",
    "QuickBooksPayment",
    "QuickBooksSalesReceipt",
]


def _decimal(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class QuickBooksRecord(BaseModel):
    """What every QuickBooks entity carries."""

    id: str
    sync_token: str = ""
    """QuickBooks' optimistic-concurrency counter.

    An update must send the value the record currently has, and QuickBooks
    rejects a stale one rather than merging. It is carried on every read for
    that reason: fetching it separately at write time is a second round trip
    that can still lose the race."""


class QuickBooksCustomer(QuickBooksRecord):
    """A customer."""

    display_name: str = ""
    """QuickBooks requires this to be **unique across the company**, and
    rejects a duplicate with a validation fault rather than de-duplicating.
    That is why creating a customer means looking one up first."""

    email: str = ""
    company_name: str = ""
    given_name: str = ""
    family_name: str = ""
    active: bool = True
    balance: float = 0.0
    """Outstanding, in the company's currency, as a decimal."""

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> QuickBooksCustomer:
        email = (body.get("PrimaryEmailAddr") or {}).get("Address", "")
        return cls(
            id=str(body.get("Id", "")),
            sync_token=str(body.get("SyncToken", "")),
            display_name=body.get("DisplayName") or "",
            email=email or "",
            company_name=body.get("CompanyName") or "",
            given_name=body.get("GivenName") or "",
            family_name=body.get("FamilyName") or "",
            active=bool(body.get("Active", True)),
            balance=_decimal(body.get("Balance")),
        )


class QuickBooksItem(BaseModel):
    """A product or service that can appear on a line."""

    id: str
    name: str = ""
    item_type: str = ""
    unit_price: float = 0.0
    income_account_id: str = ""

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> QuickBooksItem:
        account = body.get("IncomeAccountRef") or {}
        return cls(
            id=str(body.get("Id", "")),
            name=body.get("Name") or "",
            item_type=body.get("Type") or "",
            unit_price=_decimal(body.get("UnitPrice")),
            income_account_id=str(account.get("value") or ""),
        )


class QuickBooksSalesReceipt(QuickBooksRecord):
    """A paid sale — money already received, so no invoice is outstanding.

    The right entity for a Stripe payment: an *invoice* asks for money, and a
    receipt records money that arrived. Filing a Stripe charge as an invoice
    leaves a receivable open against a customer who has already paid.
    """

    doc_number: str = ""
    customer_id: str = ""
    total: float = 0.0
    """Decimal, in the company's currency — ``42.00``, not ``4200``."""

    currency: str = ""
    txn_date: str = ""
    """``YYYY-MM-DD``. QuickBooks has no time component here."""

    private_note: str = ""
    """Not shown to the customer. Where an external id belongs, so a second run
    can tell whether this receipt already exists."""

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> QuickBooksSalesReceipt:
        customer = body.get("CustomerRef") or {}
        currency = body.get("CurrencyRef") or {}
        return cls(
            id=str(body.get("Id", "")),
            sync_token=str(body.get("SyncToken", "")),
            doc_number=body.get("DocNumber") or "",
            customer_id=str(customer.get("value") or ""),
            total=_decimal(body.get("TotalAmt")),
            currency=str(currency.get("value") or ""),
            txn_date=body.get("TxnDate") or "",
            private_note=body.get("PrivateNote") or "",
        )


class QuickBooksInvoice(QuickBooksRecord):
    """A request for payment."""

    doc_number: str = ""
    customer_id: str = ""
    total: float = 0.0
    balance: float = 0.0
    """What is still outstanding. Zero means paid."""

    currency: str = ""
    txn_date: str = ""
    due_date: str = ""

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> QuickBooksInvoice:
        customer = body.get("CustomerRef") or {}
        currency = body.get("CurrencyRef") or {}
        return cls(
            id=str(body.get("Id", "")),
            sync_token=str(body.get("SyncToken", "")),
            doc_number=body.get("DocNumber") or "",
            customer_id=str(customer.get("value") or ""),
            total=_decimal(body.get("TotalAmt")),
            balance=_decimal(body.get("Balance")),
            currency=str(currency.get("value") or ""),
            txn_date=body.get("TxnDate") or "",
            due_date=body.get("DueDate") or "",
        )


class QuickBooksPayment(QuickBooksRecord):
    """Money received against one or more invoices."""

    customer_id: str = ""
    total: float = 0.0
    currency: str = ""
    txn_date: str = ""
    unapplied: float = 0.0
    """Received but not yet matched to an invoice."""

    metadata: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> QuickBooksPayment:
        customer = body.get("CustomerRef") or {}
        currency = body.get("CurrencyRef") or {}
        return cls(
            id=str(body.get("Id", "")),
            sync_token=str(body.get("SyncToken", "")),
            customer_id=str(customer.get("value") or ""),
            total=_decimal(body.get("TotalAmt")),
            currency=str(currency.get("value") or ""),
            txn_date=body.get("TxnDate") or "",
            unapplied=_decimal(body.get("UnappliedAmt")),
        )
