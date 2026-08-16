"""One request helper for every Google API call.

Gmail, Calendar, Drive and Meet differ in their resources, not in how they are
called: same bearer token, same error envelope, same pagination idiom. Putting
that here means a fix to token invalidation or error classification lands in all
four.

Tests inject an ``httpx`` transport rather than patching module internals, so
what they exercise is the real request construction — URL, params, headers, and
body — right up to the socket.

Two things are *not* uniform across the four, and both are parameters rather
than assumptions. The page-size parameter is spelled ``maxResults`` by Gmail and
Calendar and ``pageSize`` by Drive and Meet — sending the wrong one is not an
error, it is silently ignored, so every request asks for the default page size
and a caller wanting 600 rows gets 100. And Drive moves *bytes*, not JSON, in
both directions, which is why :meth:`GoogleSession.download` and
:meth:`GoogleSession.send_bytes` exist beside the JSON helpers.
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

__all__ = ["DEFAULT_TIMEOUT", "DEFAULT_TRANSFER_TIMEOUT", "GoogleSession"]

#: Default per-request timeout. Thirty seconds suits an API call and is far
#: too short for moving bytes, so transfers get their own — see
#: ``transfer_timeout`` on the clients that move them.
DEFAULT_TIMEOUT = 30.0

#: Default timeout for a request that carries a file. A large Drive export or
#: a Zoom recording legitimately takes minutes, and a caller who has to
#: subclass a client to say so does not have a configurable timeout.
DEFAULT_TRANSFER_TIMEOUT = 300.0



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

    async def download(self, path: str, **params: Any) -> bytes:
        """Perform one call and return the body as bytes rather than JSON.

        Drive file content and Docs exports come back as whatever the file is —
        a PDF, a spreadsheet, an image. Decoding that as JSON raises a
        ``ValueError`` several frames from the cause, so the two paths are
        separate methods rather than a flag.

        An error body *is* still JSON, so failures classify exactly as they do
        for every other call.
        """
        return await self._raw("GET", path, params=params)

    async def send_bytes(
        self,
        method: str,
        path: str,
        *,
        content: bytes,
        content_type: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send a pre-built body — a multipart upload, or raw file content.

        The body is assembled by the caller because only the caller knows the
        format: Drive's ``multipart`` upload is a MIME envelope of metadata plus
        content, and building it here would put Drive's wire format in the
        shared layer.
        """
        import httpx

        url = f"{self._base_url}/{path.lstrip('/')}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}

        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await self._send(
                client, method, url, clean, None,
                content=content, content_type=content_type,
            )
            if response.status_code == 401:
                self._auth.invalidate()
                response = await self._send(
                    client, method, url, clean, None,
                    content=content, content_type=content_type,
                )

            if response.status_code >= 400:
                raise classify(response.status_code, _body(response), url)
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    async def _raw(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> bytes:
        import httpx

        url = f"{self._base_url}/{path.lstrip('/')}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}

        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await self._send(client, method, url, clean, None)
            if response.status_code == 401:
                self._auth.invalidate()
                response = await self._send(client, method, url, clean, None)

            if response.status_code >= 400:
                raise classify(response.status_code, _body(response), url)
            return response.content

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        params: dict[str, Any],
        json: Any,
        *,
        content: bytes | None = None,
        content_type: str = "",
    ) -> httpx.Response:
        headers = await self._auth.headers()
        if json is not None:
            headers["Content-Type"] = "application/json"
        if content is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            return await client.request(
                method, url, headers=headers, params=params, content=content
            )
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
        size_param: str = "maxResults",
        page_size: int = 500,
    ) -> Results[Any]:
        """Follow ``nextPageToken`` until ``limit`` items are collected.

        Every one of these APIs caps a page well below what a caller may ask
        for — Gmail at 500, Calendar at 2500, Drive at 1000, Drive permissions
        at 100 — so "give me 600 files" is several requests or it is silently
        100. Doing it here keeps that off every call site.

        ``size_param`` and ``page_size`` are per-endpoint because Google is not
        consistent with itself: Gmail and Calendar read ``maxResults``, Drive
        and Meet read ``pageSize``, and each **ignores** the other rather than
        rejecting it. A wrong name is therefore not a failure — it is every
        request quietly asking for the server default, which is the same
        silent-truncation bug this module exists to prevent, one layer down.
        ``page_size`` must be the endpoint's own ceiling for the same reason:
        asking for more than it allows is a 400 on Drive and a clamp elsewhere.

        Returns :class:`Results` rather than a list, so a caller can tell a
        complete answer from a truncated one. Google, Jira, and Confluence page
        in three different dialects and agree on this one return type; that is
        what lets the coding agent learn the rule once.
        """

        return await page_through(
            lambda asked: self.request("GET", path, params={**(params or {}), **asked}),
            style=TokenPaging(items=items_key, size_param=size_param),
            limit=limit,
            page_size=page_size,
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
