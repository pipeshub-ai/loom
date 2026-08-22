"""Async Stripe API client — pure httpx, no vendor SDK.

Credentials resolve from an explicit argument, then the environment:

    STRIPE_API_KEY     sent as ``Authorization: Bearer sk_…``

Four things about this API drive the design here, and each is a mistake that
does not announce itself.

**Requests are form-encoded, not JSON.** Stripe reads
``application/x-www-form-urlencoded`` and nested values use bracket syntax —
``metadata[order]=A-1``, ``expand[0]=customer``. Posting JSON returns a 400
naming a parameter you did send, which reads as a wrong value rather than a
wrong encoding. :func:`form_encode` owns that translation and nothing else in
the client thinks about it.

**Idempotency is first-class and this client requires it for writes.** Stripe
accepts an ``Idempotency-Key`` header and replays the original response for 24
hours, which is the difference between a retried charge and a *second* charge.
Every write here takes one rather than generating it: a key minted inside the
client is new on every attempt, which is exactly the case it exists to prevent.

**Paging is by row id.** ``starting_after`` takes the id of the last object you
were given, not an opaque token — see :class:`~loom.toolsets.pagination.RowIdPaging`.

**Errors carry a type, and the type decides retryability.** A ``card_error`` is
the customer's card declining and will decline again; an ``api_error`` is
Stripe's own and is worth another attempt. Classifying on the status alone
merges them, which is how a declined card burns three retries.

Amounts throughout are in the **smallest currency unit**.
"""

from __future__ import annotations

from typing import Any, TypedDict
from urllib.parse import urlencode

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.pagination import Results, RowIdPaging, page_through
from loom.toolsets.stripe.models import (
    StripeCharge,
    StripeCustomer,
    StripeEvent,
    StripeInvoice,
    StripePaymentIntent,
    StripeRefund,
)

BASE_URL = "https://api.stripe.com/v1"

#: Stripe's documented ceiling for ``limit`` on a list endpoint.
STRIPE_MAX_PAGE = 100

#: Pinned so a Stripe-side version rollout cannot change what these models
#: parse. Stripe honours the account default when this is absent, which means
#: the shape of a response can change without a deploy on this side.
#:
#: Note the shape of the string. Stripe versions used to be a bare date; they
#: now carry the major release's name — ``2026-07-29.dahlia`` — where the date
#: is a monthly, backward-compatible release and the suffix names the major.
#: This is the version the models here were written against, so a bare date
#: from an older integration would parse a *different* response shape while
#: looking equally valid.
STRIPE_API_VERSION = "2026-07-29.dahlia"


class StripeError(WorkflowError):
    """A Stripe request failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        error_type: str = "",
        code: str = "",
        decline_code: str = "",
        request_id: str = "",
        **kw: Any,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        """Stripe's own ``error.type``. One of ``api_error``, ``card_error``,
        ``idempotency_error``, ``invalid_request_error`` — and nothing else."""
        self.code = code
        self.decline_code = decline_code
        """For a card decline, the *issuer's* reason.

        The actionable half, and distinct from ``code``: ``code`` says
        ``card_declined`` where ``decline_code`` says ``insufficient_funds``
        against ``lost_card`` — a different conversation with the customer,
        and in one case a different one with fraud."""
        self.request_id = request_id
        """``req_…``. The first thing Stripe support asks for."""


class StripePermanentError(StripeError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class StripeAuthError(StripePermanentError):
    """Missing, malformed, or revoked API key."""


class StripeInvalidRequest(StripePermanentError):  # noqa: N818 - names a state
    """A 400 — a parameter Stripe will not accept, however often it is sent."""


class StripeNotFound(StripePermanentError):  # noqa: N818 - names a state
    """No such object.

    Also what a *test-mode key reading a live-mode object* gets, which is the
    likeliest cause when the id was copied from a dashboard. The message says
    so, because otherwise it is debugged as a typo.
    """


class StripeCardDeclined(StripePermanentError):  # noqa: N818 - names a state
    """HTTP 402 — the card was declined.

    Non-retryable on purpose. The card will decline again, and
    ``decline_code`` is the actionable part — the *issuer's* reason.
    ``insufficient_funds`` is a different conversation with the customer from
    ``lost_card``, and ``code`` alone says only ``card_declined``.
    """


class StripeIdempotencyConflict(StripePermanentError):  # noqa: N818 - names a state
    """The same idempotency key was reused with *different* parameters.

    Not a duplicate-suppression success — Stripe refuses it outright, because
    replaying one response for two different requests is worse than either
    failing. Almost always a key derived from something not unique enough.
    """


class StripeRateLimited(StripeError):  # noqa: N818 - names a state
    """HTTP 429. Retryable, and the caller should back off."""


#: ``error.type`` values that no retry can fix, and the class each becomes.
#:
#: These are **all four** values the enum has — checked against
#: https://docs.stripe.com/api/errors, which lists exactly ``api_error``,
#: ``card_error``, ``idempotency_error``, and ``invalid_request_error``.
#: ``api_error`` is absent here because it is Stripe's own and worth retrying.
#:
#: Note what is *not* a type: there is no ``authentication_error`` and no
#: ``rate_limit_error``, though both read as though they should be. An earlier
#: version of this table listed them, which was harmless — the status fallbacks
#: below catch 401 and 429 — but it claimed a contract the API does not have.
#: Authentication and rate limiting are statuses here, not types.
_PERMANENT_TYPES = {
    "card_error": StripeCardDeclined,
    "invalid_request_error": StripeInvalidRequest,
    "idempotency_error": StripeIdempotencyConflict,
}


def form_encode(payload: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a dict into Stripe's bracketed form pairs.

    ``{"metadata": {"order": "A-1"}, "expand": ["customer"]}`` becomes
    ``metadata[order]=A-1`` and ``expand[0]=customer``. Lists are indexed
    because Stripe's parser reads ``expand[0]`` and ignores a repeated bare
    ``expand``.

    ``None`` is dropped rather than sent as the string ``"None"`` — Stripe has
    no null in form encoding, so an omitted parameter is the only way to say
    "leave this alone", and sending the literal would set a field to that text.
    Booleans go as ``true``/``false``, not ``True``/``False``.
    """
    pairs: list[tuple[str, str]] = []
    for key, value in payload.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            pairs.extend(form_encode(value, name))
        elif isinstance(value, list | tuple):
            for index, item in enumerate(value):
                indexed = f"{name}[{index}]"
                if isinstance(item, dict):
                    pairs.extend(form_encode(item, indexed))
                else:
                    pairs.append((indexed, _scalar(item)))
        else:
            pairs.append((name, _scalar(value)))
    return pairs


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class _ErrorFields(TypedDict):
    """The fields every classified Stripe error carries.

    A ``TypedDict`` rather than a bare dict because the values are
    heterogeneous — one ``int`` among strings — so a plain literal infers
    ``dict[str, object]`` and cannot be unpacked into a constructor expecting
    an ``int``. Annotating it ``dict[str, Any]`` would silence that; this
    keeps the checking, so a renamed field or a wrong type is caught here
    rather than at the raise.
    """

    status: int
    error_type: str
    code: str
    decline_code: str
    request_id: str


def _classify(status: int, body: dict[str, Any], request_id: str) -> StripeError:
    """The right exception for one failed response.

    On ``error.type`` first and the status only as a fallback, because the type
    is what separates "this card will decline again" from "Stripe had a
    moment". A 402 with no parsable body is still a decline.
    """
    error = body.get("error") or {}
    error_type = str(error.get("type") or "")
    code = str(error.get("code") or "")
    decline_code = str(error.get("decline_code") or "")
    message = str(error.get("message") or f"Stripe returned HTTP {status}")
    shared: _ErrorFields = {
        "status": status,
        "error_type": error_type,
        "code": code,
        "decline_code": decline_code,
        "request_id": request_id,
    }

    if status == 404 or code == "resource_missing":
        return StripeNotFound(
            f"{message} (a test-mode key cannot see a live-mode object, and "
            f"the reverse; check which key this run is using)",
            **shared,
        )
    if status == 429:
        return StripeRateLimited(message, **shared)

    permanent = _PERMANENT_TYPES.get(error_type)
    if permanent is not None:
        return permanent(message, **shared)

    # Status fallbacks, in the order the reference documents them. Reached when
    # the body carried no usable type — a gateway's error page, a truncated
    # response — so each one names what the status alone means.
    if status == 402:
        return StripeCardDeclined(message, **shared)
    if status == 409:
        # "The request conflicts with another request (perhaps due to using
        # the same idempotent key)." Retrying the same key with the same
        # mismatch fails identically.
        return StripeIdempotencyConflict(message, **shared)
    if status == 401:
        return StripeAuthError(message, **shared)
    if status == 403:
        # "The API key doesn't have permissions to perform the request" — a
        # restricted key missing a scope, not a malformed request. Classified
        # apart so the message points at the key rather than the parameters.
        return StripeAuthError(
            f"{message} (this key lacks permission for that operation — a "
            f"restricted key is scoped per resource)",
            **shared,
        )
    if status == 424:
        # "External Dependency Failed" — something Stripe depends on, not the
        # request. Retryable, unlike every other 4xx here.
        return StripeError(message, **shared)
    if 400 <= status < 500:
        return StripeInvalidRequest(message, **shared)
    # 5xx and anything unrecognised: Stripe's own, so worth another attempt.
    return StripeError(message, **shared)


class StripeClient:
    """Thin async wrapper around the Stripe REST API.

    Parameters
    ----------
    api_key:
        A secret key (``sk_test_…`` or ``sk_live_…``). Falls back to
        ``STRIPE_API_KEY``. A *publishable* key (``pk_…``) is refused at
        construction: it authenticates but can read almost nothing, so it
        surfaces as a scatter of 401s rather than as the wrong key.
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = BASE_URL,
        api_version: str = STRIPE_API_VERSION,
        timeout: float = 30.0,
        account: str = "",
    ) -> None:
        self._key = api_key
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version
        self._timeout = timeout
        self._account = account
        """A connected account id (``acct_…``), sent as ``Stripe-Account``.

        Platforms act on behalf of their accounts this way; without it every
        call reads the platform's own data, which returns an empty list rather
        than an error."""

        if not self._key:
            raise StripeAuthError(
                "Stripe needs a secret key: set STRIPE_API_KEY or pass "
                "api_key=. Keys are at https://dashboard.stripe.com/apikeys."
            )
        if self._key.startswith("pk_"):
            raise StripeAuthError(
                "STRIPE_API_KEY is a publishable key (pk_…). Publishable keys "
                "are for browsers and can read almost nothing; this needs a "
                "secret key (sk_test_… or sk_live_…)."
            )

    @property
    def livemode(self) -> bool:
        """Whether this client is pointed at live data."""
        return self._key.startswith("sk_live_")

    # -- transport ----------------------------------------------------------

    def _headers(self, idempotency_key: str = "") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Stripe-Version": self._api_version,
            "Accept": "application/json",
        }
        if self._account:
            headers["Stripe-Account"] = self._account
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        import httpx

        headers = self._headers(idempotency_key)
        body: str | None = None
        if payload is not None:
            body = urlencode(form_encode(payload))
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                params=urlencode(form_encode(params)) if params else None,
                content=body,
            )

        request_id = response.headers.get("request-id", "")
        decoded: dict[str, Any] = {}
        if response.content:
            try:
                decoded = response.json()
            except ValueError:
                decoded = {}
        if response.status_code >= 400:
            raise _classify(response.status_code, decoded, request_id)
        return decoded

    async def _list(
        self,
        path: str,
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> Results[dict[str, Any]]:
        """Walk a Stripe list endpoint, following ``starting_after``."""

        async def request(params: dict[str, Any]) -> Any:
            return await self._request(
                "GET", path, params={**(filters or {}), **params}
            )

        return await page_through(
            request,
            style=RowIdPaging(),
            limit=limit,
            page_size=min(limit, STRIPE_MAX_PAGE) or 1,
        )

    # -- customers ----------------------------------------------------------

    async def get_customer(self, customer_id: str) -> StripeCustomer:
        return StripeCustomer.from_api(await self._request("GET", f"/customers/{customer_id}"))

    async def find_customers_by_email(
        self, email: str, *, limit: int = 10
    ) -> Results[StripeCustomer]:
        """Customers with exactly this email.

        Stripe's ``/customers`` endpoint filters on email as an **exact,
        case-sensitive** match and does not search. It also does not enforce
        uniqueness, so more than one customer can share an address — which is
        why this returns a list rather than the single record a caller usually
        wants. Picking one is the caller's decision, not this client's.
        """
        found = await self._list("/customers", limit=limit, filters={"email": email})
        return found.mapped(StripeCustomer.from_api)

    async def create_customer(
        self,
        *,
        idempotency_key: str,
        email: str = "",
        name: str = "",
        description: str = "",
        metadata: dict[str, str] | None = None,
    ) -> StripeCustomer:
        payload = {
            "email": email or None,
            "name": name or None,
            "description": description or None,
            "metadata": metadata or None,
        }
        return StripeCustomer.from_api(
            await self._request(
                "POST", "/customers", payload=payload, idempotency_key=idempotency_key
            )
        )

    async def update_customer(
        self, customer_id: str, *, values: dict[str, Any]
    ) -> StripeCustomer:
        """Change a customer. Only the fields passed are touched."""
        return StripeCustomer.from_api(
            await self._request("POST", f"/customers/{customer_id}", payload=values)
        )

    # -- payments -----------------------------------------------------------

    async def get_payment_intent(self, intent_id: str) -> StripePaymentIntent:
        return StripePaymentIntent.from_api(
            await self._request("GET", f"/payment_intents/{intent_id}")
        )

    async def list_payment_intents(
        self, *, customer_id: str = "", limit: int = 25
    ) -> Results[StripePaymentIntent]:
        filters = {"customer": customer_id} if customer_id else {}
        found = await self._list("/payment_intents", limit=limit, filters=filters)
        return found.mapped(StripePaymentIntent.from_api)

    async def list_charges(
        self, *, customer_id: str = "", limit: int = 25
    ) -> Results[StripeCharge]:
        filters = {"customer": customer_id} if customer_id else {}
        found = await self._list("/charges", limit=limit, filters=filters)
        return found.mapped(StripeCharge.from_api)

    # -- invoices -----------------------------------------------------------

    async def get_invoice(self, invoice_id: str) -> StripeInvoice:
        return StripeInvoice.from_api(await self._request("GET", f"/invoices/{invoice_id}"))

    async def list_invoices(
        self, *, customer_id: str = "", status: str = "", limit: int = 25
    ) -> Results[StripeInvoice]:
        filters: dict[str, Any] = {}
        if customer_id:
            filters["customer"] = customer_id
        if status:
            filters["status"] = status
        found = await self._list("/invoices", limit=limit, filters=filters)
        return found.mapped(StripeInvoice.from_api)

    # -- refunds ------------------------------------------------------------

    async def create_refund(
        self,
        *,
        idempotency_key: str,
        payment_intent_id: str = "",
        charge_id: str = "",
        amount_cents: int = 0,
        reason: str = "",
    ) -> StripeRefund:
        """Send money back.

        Exactly one of *payment_intent_id* or *charge_id* — Stripe rejects both
        together, and neither refunds nothing while returning a 400 that reads
        as a malformed amount.
        """
        if bool(payment_intent_id) == bool(charge_id):
            raise StripeInvalidRequest(
                "a refund takes exactly one of payment_intent_id or charge_id"
            )
        payload = {
            "payment_intent": payment_intent_id or None,
            "charge": charge_id or None,
            "amount": amount_cents or None,
            "reason": reason or None,
        }
        return StripeRefund.from_api(
            await self._request(
                "POST", "/refunds", payload=payload, idempotency_key=idempotency_key
            )
        )

    # -- events -------------------------------------------------------------

    async def get_event(self, event_id: str) -> StripeEvent:
        """Re-read an event from Stripe by id.

        The half of webhook handling that does not depend on trusting the
        request body: verify the signature, then read the event back and act on
        *that*. Stripe keeps events for 30 days.
        """
        return StripeEvent.from_api(await self._request("GET", f"/events/{event_id}"))

    async def list_events(
        self, *, event_type: str = "", limit: int = 25
    ) -> Results[StripeEvent]:
        filters = {"type": event_type} if event_type else {}
        found = await self._list("/events", limit=limit, filters=filters)
        return found.mapped(StripeEvent.from_api)




