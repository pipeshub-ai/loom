"""One request helper for every Google API call.

Gmail and Calendar differ in their resources, not in how they are called: same
bearer token, same error envelope, same pagination idiom. Putting that here
means a fix to token invalidation or error classification lands in both.

Tests inject an ``httpx`` transport rather than patching module internals, so
what they exercise is the real request construction — URL, params, headers, and
body — right up to the socket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loom.toolsets.google.errors import GoogleAuthError, classify
from loom.toolsets.pagination import (
    Results,
    TokenPaging,
    page_through,
)

if TYPE_CHECKING:
    import httpx

    from loom.toolsets.google.auth import GoogleAuth

__all__ = ["GoogleSession"]


class GoogleSession:
    """Authenticated JSON calls against a Google API base URL."""

    def __init__(
        self,
        auth: GoogleAuth,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Perform one call and return the decoded body (``None`` for 204).

        A 401 is retried exactly once with a freshly minted token. That is not a
        retry policy in disguise — it is the one case where the same request,
        sent again immediately, legitimately succeeds: the cached token expired
        sooner than its stated lifetime.
        """
        import httpx

        url = f"{self._base_url}/{path.lstrip('/')}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}

        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await self._send(client, method, url, clean, json)
            if response.status_code == 401:
                self._auth.invalidate()
                response = await self._send(client, method, url, clean, json)

            if response.status_code >= 400:
                raise classify(response.status_code, _body(response), url)
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        params: dict[str, Any],
        json: Any,
    ) -> httpx.Response:
        headers = await self._auth.headers()
        if json is not None:
            headers["Content-Type"] = "application/json"
        return await client.request(
            method, url, headers=headers, params=params, json=json
        )

    # -- convenience ---------------------------------------------------------

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: Any = None, **params: Any) -> Any:
        return await self.request("POST", path, params=params, json=json)

    async def put(self, path: str, json: Any = None, **params: Any) -> Any:
        return await self.request("PUT", path, params=params, json=json)

    async def patch(self, path: str, json: Any = None, **params: Any) -> Any:
        return await self.request("PATCH", path, params=params, json=json)

    async def delete(self, path: str, **params: Any) -> Any:
        return await self.request("DELETE", path, params=params)

    async def paginate(
        self,
        path: str,
        *,
        items_key: str,
        limit: int,
        params: dict[str, Any] | None = None,
    ) -> Results:
        """Follow ``nextPageToken`` until ``limit`` items are collected.

        Both APIs cap a page well below what a caller may ask for — Gmail at
        500, Calendar at 2500 — so "give me 600 messages" is several requests or
        it is silently 500. Doing it here keeps that off every call site.

        Returns :class:`Results` rather than a list, so a caller can tell a
        complete answer from a truncated one. Google, Jira, and Confluence page
        in three different dialects and agree on this one return type; that is
        what lets the coding agent learn the rule once.
        """

        return await page_through(
            lambda asked: self.request("GET", path, params={**(params or {}), **asked}),
            style=TokenPaging(items=items_key),
            limit=limit,
            page_size=500,
        )


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def require_auth(auth: GoogleAuth | None) -> GoogleAuth:
    """Return ``auth``, or raise with the setup instructions if it is missing."""
    if auth is None:
        raise GoogleAuthError("No Google credentials configured.")
    return auth
