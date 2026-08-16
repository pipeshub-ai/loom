"""Async HubSpot CRM API client — pure httpx, no vendor SDK.

Credentials resolve from an argument, then the environment:

    HUBSPOT_ACCESS_TOKEN    a private app token, or an OAuth access token

Both are ``Authorization: Bearer …``, so unlike ClickUp there is one shape to
get right. Legacy ``hapikey`` query-parameter keys are deliberately not
supported.

**Two caps decide the design here**, and both silently truncate rather than
erroring:

- **Search returns at most 10,000 results per query.** Paging past that is a
  400, so a naive loop turns a large query into an error at the very end. This
  client stops at the cap and reports ``complete=False`` — which is the whole
  reason ``Results`` carries coverage at all.
- **Properties are opt-in.** A response contains only what ``properties=``
  asked for, and HubSpot's default for a contact is little more than a name and
  an email. That reads like a contact with no company rather than like an
  under-specified request, so a default field list is declared per object type.

The path version is a constructor argument. HubSpot has begun publishing dated
versions (``/crm/objects/2026-03/…``) alongside ``v3``, and a host that wants
one changes a string rather than waiting for a release.
"""

from __future__ import annotations

import os
from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.hubspot.models import (
    HubSpotAccount,
    HubSpotCompany,
    HubSpotContact,
    HubSpotDeal,
    HubSpotObject,
    HubSpotOwner,
)
from loom.toolsets.pagination import Results, TokenPaging, page_through

BASE_URL = "https://api.hubapi.com"
DEFAULT_VERSION = "v3"

#: HubSpot's page ceilings: 100 for a list, 200 for a search.
LIST_PAGE_CAP = 100
SEARCH_PAGE_CAP = 200

#: Total results any one search query can page through. Past this HubSpot
#: returns 400, so the loop stops here and says the answer is partial.
SEARCH_TOTAL_CAP = 10_000

#: What is worth asking for, per object type. Omitting `properties` returns
#: HubSpot's own sparse default, which reads as missing data.
DEFAULT_PROPERTIES: dict[str, list[str]] = {
    "contacts": [
        "email", "firstname", "lastname", "company", "phone",
        "lifecyclestage", "hubspot_owner_id", "createdate",
    ],
    "companies": [
        "name", "domain", "industry", "city", "country", "hubspot_owner_id",
    ],
    "deals": [
        "dealname", "dealstage", "pipeline", "amount", "closedate",
        "hubspot_owner_id", "hs_is_closed",
    ],
    "tickets": [
        "subject", "content", "hs_pipeline_stage", "hs_ticket_priority",
        "hubspot_owner_id",
    ],
}


class HubSpotError(WorkflowError):
    """A HubSpot request failed. Retryable unless a subclass says otherwise."""

    def __init__(
        self, message: str, *, status: int = 0, category: str = "", **kw: Any
    ) -> None:
        super().__init__(message)
        self.status = status
        self.category = category
        """HubSpot's own ``category`` — ``VALIDATION_ERROR``, ``OBJECT_NOT_FOUND``
        — which says more about the failure than the status does."""


class HubSpotPermanentError(HubSpotError, NonRetryableError):
    """Fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class HubSpotAuthError(HubSpotPermanentError):
    """Missing, malformed, or revoked credentials, or a missing scope."""


class HubSpotRateLimited(HubSpotError):  # noqa: N818 - names a state
    """Too many requests. Retryable, and the caller should back off.

    Search is limited to five requests a second per account — far tighter than
    the general limit, and easy to reach when paging.
    """

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class HubSpotClient:
    """Thin async wrapper around the HubSpot CRM API."""

    def __init__(
        self,
        access_token: str | None = None,
        *,
        base_url: str = BASE_URL,
        version: str = DEFAULT_VERSION,
        timeout: float = 30.0,
    ) -> None:
        self._token = access_token or os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
        self._base_url = base_url.rstrip("/")
        self._version = version
        self._timeout = timeout

        if not self._token:
            raise HubSpotAuthError(
                "HubSpot needs a token: set HUBSPOT_ACCESS_TOKEN (a private "
                "app access token) or pass access_token=."
            )

    # -- transport ----------------------------------------------------------

    async def _request(
        self, method: str, path: str, *, params: Any = None, json: Any = None
    ) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(
                method,
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                params=_clean(params),
                json=json,
            )
        if response.status_code >= 400:
            raise _classify(response)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def _objects(self, object_type: str) -> str:
        return f"/crm/{self._version}/objects/{object_type}"

    def _properties(self, object_type: str, properties: list[str] | None) -> str:
        chosen = properties or DEFAULT_PROPERTIES.get(object_type) or []
        return ",".join(chosen)

    # -- generic objects ----------------------------------------------------

    async def list_objects(
        self,
        object_type: str,
        *,
        limit: int = 50,
        properties: list[str] | None = None,
        archived: bool = False,
    ) -> Results[HubSpotObject]:
        async def request(params: dict[str, Any]) -> Any:
            return await self._request(
                "GET",
                self._objects(object_type),
                params={
                    **params,
                    "properties": self._properties(object_type, properties) or None,
                    "archived": archived,
                },
            )

        return await page_through(
            request,
            style=_LIST_PAGING,
            limit=limit,
            page_size=LIST_PAGE_CAP,
            row=lambda raw: HubSpotObject.from_api(raw, object_type),
        )

    async def search_objects(
        self,
        object_type: str,
        *,
        query: str = "",
        filters: list[dict[str, Any]] | None = None,
        sorts: list[dict[str, str]] | None = None,
        properties: list[str] | None = None,
        limit: int = 50,
    ) -> Results[HubSpotObject]:
        """Search one object type.

        Stops at HubSpot's 10,000-result ceiling and reports the answer as
        partial rather than paging into the 400 that lies just past it.
        """
        chosen = self._properties(object_type, properties)
        body: dict[str, Any] = {
            "limit": min(limit, SEARCH_PAGE_CAP),
            "properties": chosen.split(",") if chosen else [],
        }
        if query:
            body["query"] = query
        if filters:
            # One group: HubSpot ANDs within a group and ORs between groups,
            # and a caller passing a flat list means "all of these".
            body["filterGroups"] = [{"filters": filters}]
        if sorts:
            body["sorts"] = sorts

        async def request(params: dict[str, Any]) -> Any:
            payload = dict(body)
            if params.get("after"):
                payload["after"] = params["after"]
            payload["limit"] = params.get("limit", body["limit"])
            return await self._request(
                "POST", f"{self._objects(object_type)}/search", json=payload
            )

        return await page_through(
            request,
            style=_SEARCH_PAGING,
            limit=min(limit, SEARCH_TOTAL_CAP),
            page_size=min(limit, SEARCH_PAGE_CAP),
            row=lambda raw: HubSpotObject.from_api(raw, object_type),
        )

    async def get_object(
        self,
        object_type: str,
        object_id: str,
        *,
        properties: list[str] | None = None,
        id_property: str = "",
    ) -> HubSpotObject:
        body = await self._request(
            "GET",
            f"{self._objects(object_type)}/{object_id}",
            params={
                "properties": self._properties(object_type, properties) or None,
                "idProperty": id_property or None,
            },
        )
        return HubSpotObject.from_api(body, object_type)

    async def create_object(
        self, object_type: str, properties: dict[str, Any]
    ) -> HubSpotObject:
        body = await self._request(
            "POST", self._objects(object_type), json={"properties": properties}
        )
        return HubSpotObject.from_api(body, object_type)

    async def update_object(
        self, object_type: str, object_id: str, properties: dict[str, Any]
    ) -> HubSpotObject:
        body = await self._request(
            "PATCH",
            f"{self._objects(object_type)}/{object_id}",
            json={"properties": properties},
        )
        return HubSpotObject.from_api(body, object_type)

    async def archive_object(self, object_type: str, object_id: str) -> bool:
        await self._request("DELETE", f"{self._objects(object_type)}/{object_id}")
        return True

    # -- typed conveniences -------------------------------------------------

    async def find_contacts(
        self, query: str = "", *, limit: int = 20
    ) -> list[HubSpotContact]:
        rows = await self.search_objects("contacts", query=query, limit=limit)
        return [HubSpotContact.from_api(_raw(r)) for r in rows]

    async def get_contact_by_email(self, email: str) -> HubSpotContact:
        """Fetch a contact by email rather than by record id.

        HubSpot's ``idProperty`` route, and the one a workflow starting from an
        inbox actually needs — searching for an address returns a list a caller
        then has to disambiguate.
        """
        found = await self.get_object("contacts", email, id_property="email")
        return HubSpotContact.from_api(_raw(found))

    async def create_contact(self, properties: dict[str, Any]) -> HubSpotContact:
        made = await self.create_object("contacts", properties)
        return HubSpotContact.from_api(_raw(made))

    async def find_companies(
        self, query: str = "", *, limit: int = 20
    ) -> list[HubSpotCompany]:
        rows = await self.search_objects("companies", query=query, limit=limit)
        return [HubSpotCompany.from_api(_raw(r)) for r in rows]

    async def find_deals(
        self, query: str = "", *, limit: int = 20
    ) -> list[HubSpotDeal]:
        rows = await self.search_objects("deals", query=query, limit=limit)
        return [HubSpotDeal.from_api(_raw(r)) for r in rows]

    async def create_deal(self, properties: dict[str, Any]) -> HubSpotDeal:
        made = await self.create_object("deals", properties)
        return HubSpotDeal.from_api(_raw(made))

    # -- associations and owners -------------------------------------------

    async def get_associations(
        self, object_type: str, object_id: str, to_object_type: str
    ) -> list[str]:
        """Ids of the records associated with one record.

        The join a CRM workflow lives on: the deals for a contact, the contacts
        at a company.
        """
        body = await self._request(
            "GET",
            f"/crm/{self._version}/objects/{object_type}/{object_id}"
            f"/associations/{to_object_type}",
        )
        return [
            str(row.get("id") or row.get("toObjectId") or "")
            for row in (body.get("results") or [])
        ]

    async def account_info(self) -> HubSpotAccount:
        """The account this token belongs to.

        The connectivity check every other toolset here spells ``whoami``.
        Named for what it returns: a private app token authenticates an app
        against a portal, so there is no user to name.
        """
        return HubSpotAccount.from_api(
            await self._request("GET", "/account-info/v3/details")
        )

    async def list_owners(self, *, email: str = "", limit: int = 100) -> list[HubSpotOwner]:
        """Owners, optionally narrowed by email.

        The join between a person's name and the ``hubspot_owner_id`` every
        assignment takes.
        """
        body = await self._request(
            "GET",
            "/crm/v3/owners",
            params={"email": email or None, "limit": min(limit, LIST_PAGE_CAP)},
        )
        return [HubSpotOwner.from_api(o) for o in (body.get("results") or [])]


def _raw(obj: HubSpotObject) -> dict[str, Any]:
    """Re-wrap a flattened object so a typed model can read it.

    The typed models parse HubSpot's own shape, so the generic surface hands
    them back that shape rather than growing a second constructor per model.
    """
    return {
        "id": obj.id,
        "properties": obj.properties,
        "createdAt": obj.created_at,
        "updatedAt": obj.updated_at,
        "archived": obj.archived,
    }


#: List paging: an opaque `after`, nested at `paging.next.after`.
_LIST_PAGING = TokenPaging(
    items="results",
    size_param="limit",
    token_param="after",
    token_field=("paging", "next", "after"),
)

#: Search paging: the same token, but the size is carried in the POST body, so
#: the style's `limit` parameter is echoed rather than sent as a query arg.
_SEARCH_PAGING = TokenPaging(
    items="results",
    size_param="limit",
    token_param="after",
    token_field=("paging", "next", "after"),
    total_field="total",
)


def _clean(params: Any) -> Any:
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if v is not None}


def _classify(response: Any) -> HubSpotError:
    """Turn a failed response into the narrowest error that fits."""
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    detail = body.get("message") if isinstance(body, dict) else None
    category = str(body.get("category", "") if isinstance(body, dict) else "")
    message = f"HubSpot {status}: {detail or response.text[:200] or 'request failed'}"
    if category:
        message += f" ({category})"

    if status == 429:
        return HubSpotRateLimited(
            message,
            status=status,
            category=category,
            retry_after=float(response.headers.get("Retry-After", 0) or 0),
        )
    if status in (401, 403):
        return HubSpotAuthError(message, status=status, category=category)
    if 400 <= status < 500:
        return HubSpotPermanentError(message, status=status, category=category)
    return HubSpotError(message, status=status, category=category)


_default_client: HubSpotClient | None = None


def get_default_client() -> HubSpotClient:
    """Return (or create) the module-level client from the environment."""
    global _default_client
    if _default_client is None:
        _default_client = HubSpotClient()
    return _default_client
