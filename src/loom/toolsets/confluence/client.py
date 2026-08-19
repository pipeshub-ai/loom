"""Lazy httpx-based Confluence Cloud REST API v2 client.

No ``confluence`` pip package required — pure httpx over the REST API.
Credentials are read from environment variables at first use:

    CONFLUENCE_URL          https://yourorg.atlassian.net
    CONFLUENCE_EMAIL        your@email.com
    CONFLUENCE_API_TOKEN    your-api-token

Uses the same Atlassian account credentials as Jira.
All public methods return typed Pydantic models from ``models.py``.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from loom.connectors.credentials import current_credential_store, resolve_bearer_token
from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.confluence.models import (
    ConfluenceComment,
    ConfluencePage,
    ConfluenceSpace,
    ConfluenceUser,
    CreatedPage,
    PageBody,
    SearchResult,
)
from loom.toolsets.pagination import (
    CursorPaging,
    OffsetPaging,
    Results,
    page_through,
)

#: Confluence caps a page here whatever ``limit`` asks for.
CONFLUENCE_PAGE_CAP = 250


class ConfluenceError(WorkflowError):
    """A Confluence request failed. Retryable unless a subclass says otherwise."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class ConfluencePermanentError(ConfluenceError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class ConfluenceAuthError(ConfluencePermanentError):
    """Missing, malformed, expired, or revoked credentials."""


class ConfluenceNotFound(ConfluencePermanentError):  # noqa: N818 - names a state
    """No such page or space — or the token cannot see it.

    Confluence answers 404 for content the credential lacks permission on, so
    this means "not visible to you" as often as "absent".
    """


class ConfluenceRateLimited(ConfluenceError):  # noqa: N818 - names a state
    """Quota spent. Retryable, and the caller should back off."""

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


def _to_space(s: dict[str, Any]) -> ConfluenceSpace:
    """One v2 space row, flattened."""
    description = s.get("description")
    return ConfluenceSpace(
        id=s["id"],
        key=s.get("key", ""),
        name=s.get("name", ""),
        type=s.get("type", ""),
        status=s.get("status", ""),
        description=(
            description.get("plain", {}).get("value", "")
            if isinstance(description, dict)
            else str(description or "")
        ),
    )


class ConfluenceClient:
    """Thin async wrapper around the Confluence Cloud REST API v2.

    Parameters
    ----------
    base_url:
        Base URL (e.g. ``https://myorg.atlassian.net``).
        Falls back to ``CONFLUENCE_URL`` env var.
    email:
        Atlassian account email. Falls back to ``CONFLUENCE_EMAIL``.
    api_token:
        Atlassian API token. Falls back to ``CONFLUENCE_API_TOKEN``.
    """

    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        *,
        credential_name: str = "confluence",
    ) -> None:
        self._base_url = (
            base_url or os.environ.get("CONFLUENCE_URL", "")
        ).rstrip("/")
        self._email = email or os.environ.get("CONFLUENCE_EMAIL", "")
        self._token = api_token or os.environ.get("CONFLUENCE_API_TOKEN", "")
        self._credential_name = credential_name

        if not self._base_url:
            msg = "CONFLUENCE_URL is required (env var or base_url argument)"
            raise ValueError(msg)
        # See JiraClient.__init__: deferred to _headers() at first call when
        # a CredentialStore is bound, since checking it needs an await.
        if (not self._email or not self._token) and current_credential_store() is None:
            msg = "CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN are required"
            raise ValueError(msg)

    async def _headers(self) -> dict[str, str]:
        """Basic auth from email/token, or a CredentialStore-issued bearer token.

        See ``JiraClient._headers`` — same reasoning, same fallback order.
        """
        token = await resolve_bearer_token(self._credential_name)
        if token:
            return {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        if self._email and self._token:
            credentials = base64.b64encode(f"{self._email}:{self._token}".encode()).decode()
            return {
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        raise ValueError(
            "CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN are required, or "
            f"connect a '{self._credential_name}' credential via a CredentialStore"
        )

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    async def _request(
        self, method: str, url: str, *, params: Any = None, json: Any = None
    ) -> Any:
        """One place where a Confluence response becomes a value or an error.

        Every method used ``raise_for_status``, which discards the body — and
        the body is where Confluence says which page, which space, and which
        permission. It also made every 4xx look retryable, so a request for a
        page the token cannot see was sent three times to be refused three
        times.
        """
        import httpx

        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method, url, headers=headers, params=_clean(params), json=json
            )
        if resp.status_code >= 400:
            raise _classify(resp)
        if not resp.content:
            return {}
        return resp.json()

    def _v2(self, path: str) -> str:
        return f"{self._base_url}/wiki/api/v2/{path.lstrip('/')}"

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", self._v2(path), params=params)

    async def _get_v1(self, path: str, **params: Any) -> Any:
        """GET against the v1 REST API (for search/CQL)."""
        url = f"{self._base_url}/wiki/rest/api/{path.lstrip('/')}"
        return await self._request("GET", url, params=params)

    async def _post(self, path: str, json: dict[str, Any]) -> Any:
        return await self._request("POST", self._v2(path), json=json)

    async def _put(self, path: str, json: dict[str, Any]) -> Any:
        return await self._request("PUT", self._v2(path), json=json)

    async def _delete(self, path: str) -> None:
        await self._request("DELETE", self._v2(path))

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    async def search_pages(
        self,
        cql: str,
        limit: int = 20,
    ) -> Results[Any]:
        """Search content using CQL (Confluence Query Language).

        Uses the v1 search endpoint since v2 has no CQL search — and v1 pages
        by ``start``/``limit`` with a server-side ceiling, so a large ``limit``
        comes back short and says nothing about it.

        The result is a list; check ``.complete`` to find out whether ``limit``
        cut it off.
        """

        return await page_through(
            lambda params: self._get_v1("search", cql=cql, **params),
            # v1 pages by offset and signals more with a next link; different
            # Confluence versions send one or the other, so both are consulted.
            style=OffsetPaging(
                items="results",
                start_param="start",
                size_param="limit",
                total_field="totalSize",
                next_link=("_links", "next"),
            ),
            limit=limit,
            page_size=CONFLUENCE_PAGE_CAP,
            row=self._to_search_result,
        )

    def _to_search_result(self, item: dict[str, Any]) -> SearchResult:
        """One v1 search row, flattened. Per row, so it composes with paging."""
        content = item.get("content", {})
        excerpt = item.get("excerpt", "")
        return SearchResult(
            content_id=content.get("id", ""),
            title=content.get("title", ""),
            type=content.get("type", ""),
            space_key=(
                content.get("space", {}).get("key", "") if "space" in content else ""
            ),
            excerpt=excerpt.replace("<@hl>", "").replace("</@hl>", "")[:200],
            url=f"{self._base_url}/wiki{content.get('_links', {}).get('webui', '')}",
            last_modified=content.get("history", {})
            .get("lastUpdated", {})
            .get("when", ""),
        )

    async def get_page(self, page_id: str) -> ConfluencePage:
        """Fetch a page by its ID."""
        data = await self._get(f"pages/{page_id}")
        return _flatten_page(data, self._base_url)

    async def create_page(
        self,
        space_id: str,
        title: str,
        body: str,
        *,
        parent_id: str | None = None,
    ) -> CreatedPage:
        """Create a new page in a space.

        Parameters
        ----------
        space_id:
            The space ID (not key). Get this via ``list_spaces()``.
        title:
            Page title.
        body:
            HTML body content (storage format).
        parent_id:
            Optional parent page ID for nesting.
        """
        payload: dict[str, Any] = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {
                "representation": "storage",
                "value": body,
            },
        }
        if parent_id:
            payload["parentId"] = parent_id

        data = await self._post("pages", payload)
        return CreatedPage(
            id=data["id"],
            title=data.get("title", title),
            version=data.get("version", {}).get("number", 1),
            url=f"{self._base_url}/wiki{data.get('_links', {}).get('webui', '')}",
        )

    async def update_page(
        self,
        page_id: str,
        title: str,
        body: str,
        *,
        version: int | None = None,
    ) -> CreatedPage:
        """Update an existing page.

        If *version* is not provided, the current version is fetched
        and incremented automatically.
        """
        if version is None:
            current = await self._get(f"pages/{page_id}")
            version = current.get("version", {}).get("number", 1) + 1

        payload = {
            "id": page_id,
            "status": "current",
            "title": title,
            "body": {
                "representation": "storage",
                "value": body,
            },
            "version": {"number": version},
        }
        data = await self._put(f"pages/{page_id}", payload)
        return CreatedPage(
            id=data.get("id", page_id),
            title=data.get("title", title),
            version=data.get("version", {}).get("number", version),
            url=f"{self._base_url}/wiki{data.get('_links', {}).get('webui', '')}",
        )

    async def delete_page(self, page_id: str) -> None:
        """Delete a page by ID."""
        await self._delete(f"pages/{page_id}")

    async def get_page_body(self, page_id: str) -> PageBody:
        """Fetch the rendered body of a page."""
        data = await self._get(
            f"pages/{page_id}",
            **{"body-format": "storage"},
        )
        body_val = (
            data.get("body", {}).get("storage", {}).get("value", "")
        )
        return PageBody(
            page_id=page_id,
            title=data.get("title", ""),
            body=body_val,
            representation="storage",
        )

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    async def get_page_comments(
        self, page_id: str, limit: int = 25
    ) -> Results[Any]:
        """Fetch footer comments on a page, following every page of them."""

        return await page_through(
            lambda params: self._get(f"pages/{page_id}/footer-comments", **params),
            style=CursorPaging(),
            limit=limit,
            page_size=CONFLUENCE_PAGE_CAP,
            row=lambda c: ConfluenceComment(
                id=c["id"],
                body=c.get("body", {}).get("storage", {}).get("value", ""),
                author_id=c.get("authorId", ""),
                created_at=c.get("createdAt", ""),
                page_id=page_id,
            ),
        )

    async def add_comment(
        self, page_id: str, body: str
    ) -> ConfluenceComment:
        """Add a footer comment to a page."""
        payload = {
            "pageId": page_id,
            "body": {
                "representation": "storage",
                "value": f"<p>{body}</p>",
            },
        }
        data = await self._post("footer-comments", payload)
        return ConfluenceComment(
            id=data["id"],
            body=body,
            author_id=data.get("authorId", ""),
            created_at=data.get("createdAt", ""),
            page_id=page_id,
        )

    # ------------------------------------------------------------------
    # Spaces
    # ------------------------------------------------------------------

    async def list_spaces(self, limit: int = 25) -> Results[Any]:
        """List accessible spaces, following every page."""

        return await page_through(
            lambda params: self._get("spaces", **params),
            style=CursorPaging(),
            limit=limit,
            page_size=CONFLUENCE_PAGE_CAP,
            row=_to_space,
        )



    async def get_space(self, space_id: str) -> ConfluenceSpace:
        """Get a space by its ID."""
        s = await self._get(f"spaces/{space_id}")
        return ConfluenceSpace(
            id=s["id"],
            key=s.get("key", ""),
            name=s.get("name", ""),
            type=s.get("type", ""),
            status=s.get("status", ""),
            description=(
                s.get("description", {}).get("plain", {}).get(
                    "value", ""
                )
                if isinstance(s.get("description"), dict)
                else str(s.get("description", ""))
            ),
        )

    # ------------------------------------------------------------------
    # Current user
    # ------------------------------------------------------------------

    async def get_myself(self) -> ConfluenceUser:
        """Return the authenticated user's profile."""
        data = await self._get_v1("user/current")
        return ConfluenceUser(
            account_id=data.get("accountId", ""),
            display_name=data.get("displayName", ""),
            email=data.get("email", ""),
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _flatten_page(
    raw: dict[str, Any], base_url: str
) -> ConfluencePage:
    """Flatten a Confluence v2 page response into a typed model."""
    return ConfluencePage(
        id=raw.get("id", ""),
        title=raw.get("title", ""),
        status=raw.get("status", ""),
        space_id=raw.get("spaceId", ""),
        parent_id=raw.get("parentId", ""),
        author_id=raw.get("authorId", ""),
        version=raw.get("version", {}).get("number", 1),
        created_at=raw.get("createdAt", ""),
        url=f"{base_url}/wiki{raw.get('_links', {}).get('webui', '')}",
    )


def _clean(params: Any) -> Any:
    """Drop unset filters — httpx would otherwise send the string ``"None"``."""
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if v is not None}


def _classify(response: Any) -> ConfluenceError:
    """Turn a failed response into the narrowest error that fits.

    Two body shapes, because this client speaks two APIs: v2 answers
    ``{"errors": [{"title": ..., "detail": ...}]}`` and v1 answers
    ``{"message": ...}``. Reading only one of them would leave CQL search —
    the v1 half — reporting every failure as a bare status code.
    """
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    parts: list[str] = []
    for entry in body.get("errors") or []:
        if isinstance(entry, dict):
            parts.append(
                "; ".join(
                    str(entry[k]) for k in ("title", "detail") if entry.get(k)
                )
            )
    for key in ("message", "reason"):
        if body.get(key):
            parts.append(str(body[key]))

    detail = "; ".join(p for p in parts if p) or (response.text or "").strip()[:300]
    message = f"Confluence {status}: {detail or 'request failed'}"

    if status == 429:
        return ConfluenceRateLimited(
            message,
            status=status,
            retry_after=float(response.headers.get("Retry-After", 0) or 0),
        )
    if status == 401:
        return ConfluenceAuthError(message, status=status)
    if status == 404:
        return ConfluenceNotFound(message, status=status)
    if 400 <= status < 500:
        return ConfluencePermanentError(message, status=status)
    return ConfluenceError(message, status=status)


# Module-level singleton
_default_client: ConfluenceClient | None = None


def get_default_client() -> ConfluenceClient:
    """Return (or create) the module-level client from env vars."""
    global _default_client
    if _default_client is None:
        _default_client = ConfluenceClient()
    return _default_client
