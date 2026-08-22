"""Async QuickBooks Online client — pure httpx, no vendor SDK.

Credentials resolve from explicit arguments, then the environment:

    QUICKBOOKS_CLIENT_ID + QUICKBOOKS_CLIENT_SECRET + QUICKBOOKS_REFRESH_TOKEN
    QUICKBOOKS_REALM_ID          the company this token is for
    QUICKBOOKS_ACCESS_TOKEN      a ready-made hour-long token (optional)
    QUICKBOOKS_ENVIRONMENT       "production" (default) or "sandbox"

Five things about this API drive the design here.

**There is no constant base URL, and the company id is in every path.** A
realm id names one company file, and a token is issued against one realm.
Getting it wrong is a 401 or an empty list rather than a clear error, so the
client refuses to construct without one — the same rule
``toolsets/salesforce`` follows for ``instance_url``, for the same reason.

**Sandbox and production are different hosts.** A sandbox token against the
production host authenticates and then finds no data, which reads as an empty
company rather than a wrong host.

**Access tokens last an hour and refreshing is mandatory.** The client owns the
exchange, under a lock, and retries a 401 exactly once — the arrangement
``toolsets/google/auth.py`` and the Salesforce client both use. Twice would
turn a revoked grant into a loop against the token endpoint.

**Every update carries a SyncToken.** QuickBooks uses optimistic concurrency: a
write must send the value the record currently has, and a stale one is
*rejected*, not merged. That is why every model here carries ``sync_token`` and
why the read that precedes a write is not optional.

**Queries are SQL-shaped but are not SQL.** No joins, ``STARTPOSITION`` is
**1-based**, and a string literal containing an apostrophe must be escaped or
it terminates the literal — ``O'Brien`` again, exactly as in Salesforce.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.pagination import Page, Results, collect
from loom.toolsets.quickbooks.models import (
    QuickBooksCustomer,
    QuickBooksInvoice,
    QuickBooksItem,
    QuickBooksPayment,
    QuickBooksSalesReceipt,
)

PRODUCTION_URL = "https://quickbooks.api.intuit.com"
SANDBOX_URL = "https://sandbox-quickbooks.api.intuit.com"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

#: QuickBooks' documented ceiling for ``MAXRESULTS``.
QUICKBOOKS_MAX_PAGE = 1000

#: Pinned so an Intuit-side rollout cannot change what these models parse.
MINOR_VERSION = "70"


class QuickBooksError(WorkflowError):
    """A QuickBooks request failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self, message: str, *, status: int = 0, code: str = "", detail: str = "", **kw: Any
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        """Intuit's own numeric error code, as a string."""
        self.detail = detail


class QuickBooksPermanentError(QuickBooksError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class QuickBooksAuthError(QuickBooksPermanentError):
    """The grant is gone — a revoked or expired refresh token.

    Distinct from an expired *access* token, which the client refreshes on its
    own and never surfaces. Reaching this means somebody has to reconnect the
    app: a refresh token that goes 100 days unused is revoked by Intuit.
    """


class QuickBooksValidationError(QuickBooksPermanentError):
    """The company file will not accept this record as written.

    A duplicate ``DisplayName`` is the common one, and QuickBooks rejects it
    rather than de-duplicating — which is why creating a customer means looking
    one up first.
    """


class QuickBooksStaleObject(QuickBooksPermanentError):  # noqa: N818 - names a state
    """The SyncToken sent was not the record's current one.

    Somebody else changed the record between the read and the write. Retrying
    with the *same* token fails identically, which is why this is not
    retryable — the fix is to re-read and decide whether the change still
    applies.
    """


class QuickBooksNotFound(QuickBooksPermanentError):  # noqa: N818 - names a state
    """No such record in this company file.

    Also what a *sandbox realm queried against production* returns, which is
    the likeliest cause when the id came from a working sandbox.
    """


class QuickBooksThrottled(QuickBooksError):  # noqa: N818 - names a state
    """HTTP 429. Retryable, and the caller should back off."""


#: Intuit error codes worth telling apart. Everything else falls back to the
#: status, because the code list is long and mostly means "validation".
_CODES = {
    "5010": QuickBooksStaleObject,
    "610": QuickBooksNotFound,
    "6240": QuickBooksValidationError,
}


def escape_literal(value: str) -> str:
    """Escape a string for a QuickBooks query literal.

    ``O'Brien`` terminates the literal unescaped, and the resulting query is
    either a syntax error or — worse — a valid query matching something else.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


class _ErrorFields(TypedDict):
    """The fields every classified QuickBooks error carries.

    A ``TypedDict`` because the values are heterogeneous — one ``int`` among
    strings — so a plain literal infers ``dict[str, object]`` and cannot be
    unpacked into a constructor expecting an ``int``.
    """

    status: int
    code: str
    detail: str


def _classify(status: int, body: dict[str, Any]) -> QuickBooksError:
    """The right exception for one failed response."""
    fault = body.get("Fault") or {}
    errors = fault.get("Error") or []
    first = errors[0] if isinstance(errors, list) and errors else {}
    code = str(first.get("code") or "")
    message = str(first.get("Message") or f"QuickBooks returned HTTP {status}")
    detail = str(first.get("Detail") or "")
    shared: _ErrorFields = {"status": status, "code": code, "detail": detail}

    known = _CODES.get(code)
    if known is not None:
        return known(f"{message}: {detail}" if detail else message, **shared)
    if status == 401:
        return QuickBooksAuthError(message, **shared)
    if status == 404:
        return QuickBooksNotFound(message, **shared)
    if status == 429:
        return QuickBooksThrottled(message, **shared)
    if 400 <= status < 500:
        return QuickBooksValidationError(
            f"{message}: {detail}" if detail else message, **shared
        )
    return QuickBooksError(message, **shared)


class QuickBooksClient:
    """Thin async wrapper around the QuickBooks Online REST API."""

    def __init__(
        self,
        *,
        realm_id: str = "",
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
        access_token: str = "",
        environment: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._realm = realm_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token = access_token
        self._environment = (
            environment or "production"
        ).lower()
        self._timeout = timeout
        self._lock = asyncio.Lock()

        if not self._realm:
            raise QuickBooksAuthError(
                "QuickBooks needs a company (realm) id: set QUICKBOOKS_REALM_ID "
                "or pass realm_id=. A token is issued against one company file, "
                "and the id is part of every request path — without it there is "
                "no URL to call."
            )
        refreshable = self._client_id and self._client_secret and self._refresh_token
        if not (refreshable or self._access_token):
            raise QuickBooksAuthError(
                "QuickBooks needs either QUICKBOOKS_CLIENT_ID + "
                "QUICKBOOKS_CLIENT_SECRET + QUICKBOOKS_REFRESH_TOKEN (which "
                "this client refreshes for you), or a ready-made "
                "QUICKBOOKS_ACCESS_TOKEN, which lasts one hour."
            )

    @property
    def base_url(self) -> str:
        """The host for this environment.

        Separate hosts, and a sandbox token against production authenticates
        and then finds nothing — which reads as an empty company rather than as
        the wrong host.
        """
        return SANDBOX_URL if self._environment == "sandbox" else PRODUCTION_URL

    @property
    def company_path(self) -> str:
        return f"/v3/company/{self._realm}"

    # -- auth ---------------------------------------------------------------

    async def _refresh(self) -> str:
        """Exchange the refresh token for a new access token, under a lock."""
        import base64

        import httpx

        if not (self._client_id and self._client_secret and self._refresh_token):
            raise QuickBooksAuthError(
                "the QuickBooks access token has expired and there are no "
                "refresh credentials to mint another. Set QUICKBOOKS_CLIENT_ID, "
                "QUICKBOOKS_CLIENT_SECRET and QUICKBOOKS_REFRESH_TOKEN."
            )
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.post(
                TOKEN_URL,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            )
        if response.status_code >= 400:
            raise QuickBooksAuthError(
                "QuickBooks refused the refresh token. Intuit revokes one that "
                "goes 100 days unused, and rotates it on every refresh — if "
                "this worked yesterday, the stored token is probably the "
                "previous one. Reconnect the app to issue a new grant."
            )
        body = response.json()
        self._access_token = str(body.get("access_token") or "")
        # Intuit rotates the refresh token on every exchange. Keeping the old
        # one means the *next* refresh fails, hours later, looking unrelated.
        rotated = body.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            self._refresh_token = rotated
        return self._access_token

    async def _token(self) -> str:
        async with self._lock:
            if not self._access_token:
                return await self._refresh()
            return self._access_token

    # -- transport ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> dict[str, Any]:
        import httpx

        token = await self._token()
        query = {"minorversion": MINOR_VERSION, **(params or {})}

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(
                method,
                f"{self.base_url}{self.company_path}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                params=query,
                json=payload,
            )

        if response.status_code == 401 and not _retried:
            # Exactly once. Twice would turn a revoked grant into a loop
            # against the token endpoint.
            async with self._lock:
                self._access_token = ""
            await self._token()
            return await self._request(
                method, path, params=params, payload=payload, _retried=True
            )

        decoded: dict[str, Any] = {}
        if response.content:
            try:
                decoded = response.json()
            except ValueError:
                decoded = {}
        if response.status_code >= 400:
            raise _classify(response.status_code, decoded)
        return decoded

    # -- query --------------------------------------------------------------

    async def query(self, entity: str, where: str = "", *, limit: int = 100) -> Results[Any]:
        """Run a QuickBooks query, following pages.

        ``STARTPOSITION`` is **1-based**, which is why this drives ``collect``
        directly rather than using ``OffsetPaging``: that dialect counts rows
        from zero, and sending 0 here returns the first page again.
        """

        async def fetch(cursor: str | None, size: int) -> Page:
            start = int(cursor or 1)
            statement = f"SELECT * FROM {entity}"
            if where:
                statement += f" WHERE {where}"
            statement += f" STARTPOSITION {start} MAXRESULTS {size}"
            body = await self._request("GET", "/query", params={"query": statement})
            rows = (body.get("QueryResponse") or {}).get(entity) or []
            # QuickBooks does not report a total, so a short page is the only
            # end signal — the same limitation `OffsetPaging` documents for a
            # bare array, and `collect` reports complete=False rather than
            # claiming what it cannot verify.
            more = len(rows) >= size
            return Page(items=rows, cursor=str(start + len(rows)) if more else None)

        return await collect(
            fetch, limit=limit, page_size=min(limit, QUICKBOOKS_MAX_PAGE) or 1
        )

    # -- customers ----------------------------------------------------------

    async def find_customer_by_email(
        self, email: str, *, scan_limit: int = 1000
    ) -> QuickBooksCustomer | None:
        """The customer with this email, or ``None``.

        The resolver: every QuickBooks write takes a numeric ``Id``, and an
        email passed where an id belongs matches nothing and reports no error.

        **Two paths, because not every field is filterable.** QuickBooks marks
        each Customer attribute filterable or not, and a ``WHERE`` on one that
        is not raises a validation fault rather than being ignored. The direct
        query is tried first; if the company file refuses it, this falls back
        to scanning and matching in Python, which is what Intuit's own guidance
        suggests for an unfilterable field.

        The fallback is bounded and says so: a scan that ran out before
        matching raises rather than answering ``None``, because "not found" is
        a fact a caller acts on — it creates the customer — and it is only a
        fact if the whole set was searched. That is the same rule
        ``slack_find_channel`` and ``calendar_find_calendar`` follow.
        """
        try:
            found = await self.query(
                "Customer", f"PrimaryEmailAddr = '{escape_literal(email)}'", limit=1
            )
        except QuickBooksValidationError:
            return await self._scan_for_email(email, scan_limit)
        return QuickBooksCustomer.from_api(found[0]) if found else None

    async def _scan_for_email(
        self, email: str, scan_limit: int
    ) -> QuickBooksCustomer | None:
        """Match an email by reading customers, when the filter is refused."""
        rows = await self.query("Customer", "Active = true", limit=scan_limit)
        wanted = email.strip().lower()
        for raw in rows:
            found = QuickBooksCustomer.from_api(raw)
            if found.email.strip().lower() == wanted:
                return found
        if not rows.complete:
            raise QuickBooksValidationError(
                f"PrimaryEmailAddr is not filterable in this company file, and "
                f"a scan of {len(rows)} customers reached its limit before "
                f"matching {email!r}. Answering 'not found' here would create a "
                f"duplicate customer — raise scan_limit, or look the customer "
                f"up by DisplayName instead."
            )
        return None

    async def get_customer(self, customer_id: str) -> QuickBooksCustomer:
        body = await self._request("GET", f"/customer/{customer_id}")
        return QuickBooksCustomer.from_api(body.get("Customer") or {})

    async def create_customer(
        self,
        *,
        display_name: str,
        email: str = "",
        company_name: str = "",
        given_name: str = "",
        family_name: str = "",
    ) -> QuickBooksCustomer:
        """Create a customer.

        ``display_name`` must be unique across the company file; QuickBooks
        rejects a duplicate with a validation fault rather than returning the
        existing record, so look one up first.
        """
        payload: dict[str, Any] = {"DisplayName": display_name}
        if email:
            payload["PrimaryEmailAddr"] = {"Address": email}
        if company_name:
            payload["CompanyName"] = company_name
        if given_name:
            payload["GivenName"] = given_name
        if family_name:
            payload["FamilyName"] = family_name
        body = await self._request("POST", "/customer", payload=payload)
        return QuickBooksCustomer.from_api(body.get("Customer") or {})

    async def update_customer(
        self, customer_id: str, sync_token: str, values: dict[str, Any]
    ) -> QuickBooksCustomer:
        """Sparse-update a customer.

        ``sparse: true`` is what makes this a patch. Without it QuickBooks
        treats the payload as the *whole* record and blanks every field not
        sent — a data-loss bug that returns 200.
        """
        payload = {
            "Id": customer_id,
            "SyncToken": sync_token,
            "sparse": True,
            **values,
        }
        body = await self._request("POST", "/customer", payload=payload)
        return QuickBooksCustomer.from_api(body.get("Customer") or {})

    # -- sales receipts -----------------------------------------------------

    async def create_sales_receipt(
        self,
        *,
        customer_id: str,
        amount: float,
        description: str = "",
        item_id: str = "",
        currency: str = "",
        txn_date: str = "",
        private_note: str = "",
    ) -> QuickBooksSalesReceipt:
        """Record money already received.

        A *receipt*, not an invoice: an invoice asks for money and leaves a
        receivable open, which against a customer who has already paid is
        wrong in the ledger rather than merely untidy.
        """
        line: dict[str, Any] = {
            "Amount": round(float(amount), 2),
            "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": (
                {"ItemRef": {"value": item_id}} if item_id else {}
            ),
        }
        if description:
            line["Description"] = description
        payload: dict[str, Any] = {
            "CustomerRef": {"value": customer_id},
            "Line": [line],
        }
        if currency:
            payload["CurrencyRef"] = {"value": currency.upper()}
        if txn_date:
            payload["TxnDate"] = txn_date
        if private_note:
            payload["PrivateNote"] = private_note
        body = await self._request("POST", "/salesreceipt", payload=payload)
        return QuickBooksSalesReceipt.from_api(body.get("SalesReceipt") or {})

    async def find_sales_receipts(
        self, *, private_note: str = "", customer_id: str = "", limit: int = 25
    ) -> Results[QuickBooksSalesReceipt]:
        """Find receipts, optionally by the note an earlier run stamped.

        QuickBooks has no idempotency key, so this is what stands in for one:
        write an external id into ``PrivateNote``, and look for it before
        writing again.
        """
        clauses = []
        if private_note:
            clauses.append(f"PrivateNote = '{escape_literal(private_note)}'")
        if customer_id:
            clauses.append(f"CustomerRef = '{escape_literal(customer_id)}'")
        found = await self.query("SalesReceipt", " AND ".join(clauses), limit=limit)
        return found.mapped(QuickBooksSalesReceipt.from_api)

    # -- invoices and payments ----------------------------------------------

    async def find_invoices(
        self, *, customer_id: str = "", unpaid_only: bool = False, limit: int = 25
    ) -> Results[QuickBooksInvoice]:
        clauses = []
        if customer_id:
            clauses.append(f"CustomerRef = '{escape_literal(customer_id)}'")
        if unpaid_only:
            clauses.append("Balance > '0'")
        found = await self.query("Invoice", " AND ".join(clauses), limit=limit)
        return found.mapped(QuickBooksInvoice.from_api)

    async def find_payments(
        self, *, customer_id: str = "", limit: int = 25
    ) -> Results[QuickBooksPayment]:
        where = f"CustomerRef = '{escape_literal(customer_id)}'" if customer_id else ""
        found = await self.query("Payment", where, limit=limit)
        return found.mapped(QuickBooksPayment.from_api)

    async def find_items(self, *, name: str = "", limit: int = 25) -> Results[QuickBooksItem]:
        """Products and services.

        A sales receipt line references one by id, and a name passed there
        matches nothing — so this is the resolver for that write.
        """
        where = f"Name = '{escape_literal(name)}'" if name else ""
        found = await self.query("Item", where, limit=limit)
        return found.mapped(QuickBooksItem.from_api)




