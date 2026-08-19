"""QuickBooks Online step functions for use inside LOOM workflows.

Each is a ``@step``, so it journals, retries per its own policy, and can be
called with ``ctx.step(...)``::

    from loom.toolsets.quickbooks.tools import (
        quickbooks_find_customer, quickbooks_create_sales_receipt,
    )

**QuickBooks has no idempotency key**, which is the fact that shapes every
write here. A timeout after the company file accepted a receipt is
indistinguishable from a failure, so a retry files it twice — and a duplicated
sales receipt is a real accounting problem, not a tidy-up.

Two consequences:

* Creating a receipt or a customer is **not retried**. Journaling covers
  replay; nothing covers the attempt.
* ``quickbooks_find_sales_receipts`` exists to stand in for a key: stamp an
  external id into ``PrivateNote`` and look for it before writing. That is a
  check the *workflow* performs, deliberately, rather than something hidden in
  a client.

Updates and lookups do retry: naming the same record twice reaches the same
end state.

**Amounts are decimal here** — ``42.00`` — where Stripe uses minor units
(``4200``). A workflow bridging the two converts once, and the field names on
both sides are what make the difference visible.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.pagination import Results
from loom.toolsets.quickbooks.client import get_default_client
from loom.toolsets.quickbooks.models import (
    QuickBooksCustomer,
    QuickBooksInvoice,
    QuickBooksItem,
    QuickBooksPayment,
    QuickBooksSalesReceipt,
)

#: Reads. A validation fault and a stale SyncToken are classified
#: non-retryable in the client, so this budget goes only on Intuit's own
#: failures and on throttling.
_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Creates. QuickBooks has no idempotency key, so a timeout after the company
#: file accepted the record is indistinguishable from a failure — and a retry
#: files it twice. Journaling covers replay; nothing covers the attempt.
#:
#: Spelled out rather than left to a bare ``@step``, which retries **three
#: times** by default. A docstring saying "not retried" over a decorator that
#: retries is worse than no docstring, and `verify_effect_profile` is what
#: catches the disagreement.
_NO_RETRY = Retry(max_attempts=1)

#: Updates. Setting the same fields twice reaches the same end state — but a
#: *stale* SyncToken fails identically however often it is sent, which is why
#: that one is non-retryable rather than covered by this.
_UPDATE = Retry(max_attempts=2, initial_delay=1.0)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def quickbooks_find_customer(email: str) -> QuickBooksCustomer | None:
    """Find one customer by email address, or ``None``.

    The resolver. Every QuickBooks write takes a numeric ``Id``, and an email
    passed where an id belongs matches nothing and reports no error.

    Args:
        email: The exact address to match.
    """
    return await get_default_client().find_customer_by_email(email)


@step(retry=_READ)
async def quickbooks_get_customer(customer_id: str) -> QuickBooksCustomer:
    """Fetch one customer by id.

    Returns the record's current ``sync_token``, which any later update must
    send back — QuickBooks rejects a stale one rather than merging.

    Args:
        customer_id: The numeric QuickBooks id.
    """
    return await get_default_client().get_customer(customer_id)


@step(retry=_NO_RETRY)
async def quickbooks_create_customer(
    display_name: str,
    email: str = "",
    company_name: str = "",
    given_name: str = "",
    family_name: str = "",
) -> QuickBooksCustomer:
    """Create a customer.

    **Not retried.** QuickBooks has no idempotency key, so a timeout after it
    accepted the record is indistinguishable from a failure and a retry creates
    a second customer.

    ``display_name`` must be unique across the company file — QuickBooks
    rejects a duplicate with a validation fault rather than returning the
    existing record, so call ``quickbooks_find_customer`` first.

    Args:
        display_name: What the customer is called. Unique across the company.
        email: Their primary email address.
        company_name: Their company, if different from the display name.
        given_name: First name.
        family_name: Last name.
    """
    return await get_default_client().create_customer(
        display_name=display_name,
        email=email,
        company_name=company_name,
        given_name=given_name,
        family_name=family_name,
    )


@step(retry=_UPDATE)
async def quickbooks_update_customer(
    customer_id: str, sync_token: str, values: dict[str, object]
) -> QuickBooksCustomer:
    """Change a customer. Only the fields passed are touched.

    Args:
        customer_id: The numeric QuickBooks id.
        sync_token: The value the record currently has — read it first with
            ``quickbooks_get_customer``. A stale token is rejected, not merged.
        values: QuickBooks field names to new values, e.g.
            ``{"CompanyName": "Acme"}``.
    """
    return await get_default_client().update_customer(customer_id, sync_token, values)


# ---------------------------------------------------------------------------
# Sales receipts
# ---------------------------------------------------------------------------


@step(retry=_NO_RETRY)
async def quickbooks_create_sales_receipt(
    customer_id: str,
    amount: float,
    description: str = "",
    item_id: str = "",
    currency: str = "",
    txn_date: str = "",
    private_note: str = "",
) -> QuickBooksSalesReceipt:
    """Record money already received.

    **Not retried**, for the reason above — a duplicated receipt is an
    accounting problem. Check ``quickbooks_find_sales_receipts`` for
    *private_note* first if the caller might run twice.

    A *receipt*, not an invoice: an invoice asks for money and leaves a
    receivable open, which against a customer who has already paid is wrong in
    the ledger rather than merely untidy.

    Args:
        customer_id: The numeric QuickBooks customer id.
        amount: The total, as a **decimal** in the company's currency —
            ``42.00``, not ``4200``. Stripe uses minor units; this does not.
        description: Line description shown on the receipt.
        item_id: The product or service this is for. Resolve a name to an id
            with ``quickbooks_find_items``.
        currency: Three-letter code. Empty uses the company default.
        txn_date: ``YYYY-MM-DD``. Empty uses today in the company's timezone.
        private_note: Not shown to the customer. Put an external id here — it
            is what stands in for an idempotency key on a later run.
    """
    return await get_default_client().create_sales_receipt(
        customer_id=customer_id,
        amount=amount,
        description=description,
        item_id=item_id,
        currency=currency,
        txn_date=txn_date,
        private_note=private_note,
    )


@step(retry=_READ)
async def quickbooks_find_sales_receipts(
    private_note: str = "", customer_id: str = "", limit: int = 25
) -> Results[QuickBooksSalesReceipt]:
    """Find receipts, optionally by the note an earlier run stamped.

    The idempotency check QuickBooks does not provide: write an external id
    into ``PrivateNote`` when creating, and look for it before creating again.

    Args:
        private_note: The exact note text to match — an external id.
        customer_id: Narrow to one customer.
        limit: Most receipts to return, following pages as needed.
    """
    return await get_default_client().find_sales_receipts(
        private_note=private_note, customer_id=customer_id, limit=limit
    )


# ---------------------------------------------------------------------------
# Invoices, payments, items
# ---------------------------------------------------------------------------


@step(retry=_READ)
async def quickbooks_find_invoices(
    customer_id: str = "", unpaid_only: bool = False, limit: int = 25
) -> Results[QuickBooksInvoice]:
    """List invoices, optionally for one customer or only the unpaid ones.

    Args:
        customer_id: The numeric QuickBooks customer id, or empty for all.
        unpaid_only: Only invoices with an outstanding balance.
        limit: Most invoices to return, following pages as needed.
    """
    return await get_default_client().find_invoices(
        customer_id=customer_id, unpaid_only=unpaid_only, limit=limit
    )


@step(retry=_READ)
async def quickbooks_find_payments(
    customer_id: str = "", limit: int = 25
) -> Results[QuickBooksPayment]:
    """List payments received, optionally for one customer.

    Args:
        customer_id: The numeric QuickBooks customer id, or empty for all.
        limit: Most payments to return, following pages as needed.
    """
    return await get_default_client().find_payments(customer_id=customer_id, limit=limit)


@step(retry=_READ)
async def quickbooks_find_items(name: str = "", limit: int = 25) -> Results[QuickBooksItem]:
    """Products and services.

    The resolver for a receipt line: it references an item by id, and a name
    passed there matches nothing.

    Args:
        name: Exact item name to match, or empty for all.
        limit: Most items to return, following pages as needed.
    """
    return await get_default_client().find_items(name=name, limit=limit)
