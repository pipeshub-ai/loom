"""One request helper for every Microsoft Graph call.

OneDrive and SharePoint differ in their resources, not in how they are called:
same bearer token, same ``{"error": {"code", "message"}}`` envelope, same
``@odata.nextLink`` paging. Putting that here means a fix to token invalidation,
throttling, or error classification lands in both.

Tests inject an ``httpx`` transport rather than patching module internals, so
what they exercise is the real request construction — URL, params, headers and
body — right up to the socket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loom.toolsets.microsoft.errors import classify
from loom.toolsets.pagination import LinkPaging, Results, page_through

if TYPE_CHECKING:
    import httpx

    from loom.toolsets.microsoft.auth import MicrosoftAuth

__all__ = ["GRAPH_PAGING", "NEXT_URL", "GraphSession"]

#: The key under which a follow-up page's absolute URL is threaded through
#: :func:`page_through`. Named rather than positional because it is not a query
#: parameter at all — see :data:`GRAPH_PAGING`.
NEXT_URL = "__next_url"

#: Graph's paging dialect, expressed in the one LOOM already has.
#:
#: ``@odata.nextLink`` is a **complete URL**, and the reference is unusually
#: direct about what to do with it: "Use the entire URL […] Don't try to extract
#: the ``$skiptoken`` or ``$skip`` value and use it in a different request."
#: That rules out :class:`CursorPaging`, which parses a value out of a link and
#: re-sends it as a parameter, and selects :class:`LinkPaging`, whose cursor
#: *is* the next request.
#:
#: ``done_field=None`` because Graph signals the end by omitting the link
#: rather than by setting a flag; ``LinkPaging.read`` already yields no cursor
#: in that case. ``total_field`` is ``@odata.count``, which is only populated
#: when ``$count=true`` was asked for — so ``Results.total`` is usually ``None``
#: here, which is the honest answer rather than a guess.
GRAPH_PAGING = LinkPaging(
    items="value",
    path_param=NEXT_URL,
    link_field="@odata.nextLink",
    done_field=None,
    total_field="@odata.count",
    size_param="$top",
)


class GraphSession:
    """Authenticated JSON calls against the Microsoft Graph base URL."""

    def __init__(
        self,
        auth: MicrosoftAuth,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    @property
    def auth(self) -> MicrosoftAuth:
        return self._auth

    @property
    def base_url(self) -> str:
        return self._base_url

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform one call and return the decoded body (``None`` for 204).

        A 401 is retried exactly once with a freshly minted token. That is not a
        retry policy in disguise — it is the one case where the same request,
        sent again immediately, legitimately succeeds: the cached token expired
        sooner than its stated lifetime.

        ``headers`` carries the per-request ``Prefer`` values several Outlook
        endpoints need — body format, response timezone — which are not query
        parameters and change what the response *means* rather than which rows
        come back.
        """
        import httpx

        url = self._url(path)
        clean = {k: v for k, v in (params or {}).items() if v is not None}

        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await self._send(client, method, url, clean, json, extra=headers)
            if response.status_code == 401:
                self._auth.invalidate()
                response = await self._send(
                    client, method, url, clean, json, extra=headers
                )
            self._check(response, url)
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    async def download(self, path: str, **params: Any) -> bytes:
        """Perform one call and return the body as bytes rather than JSON.

        File content comes back as whatever the file is — a PDF, a spreadsheet,
        an image. Decoding that as JSON raises a ``ValueError`` several frames
        from the cause, so the two paths are separate methods rather than a
        flag. An error body *is* still JSON, so failures classify exactly as
        they do for every other call.

        Redirects are followed: ``/content`` answers ``302`` to a
        pre-authenticated CDN URL, and not following it returns an empty body
        that reads as an empty file.
        """
        import httpx

        url = self._url(path)
        clean = {k: v for k, v in params.items() if v is not None}

        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport, follow_redirects=True
        ) as client:
            response = await self._send(client, "GET", url, clean, None)
            if response.status_code == 401:
                self._auth.invalidate()
                response = await self._send(client, "GET", url, clean, None)
            self._check(response, url)
            return response.content

    async def send_bytes(
        self,
        method: str,
        path: str,
        *,
        content: bytes,
        content_type: str = "application/octet-stream",
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send a raw body — a small file upload, or one fragment of a large one."""
        import httpx

        url = self._url(path)
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
            self._check(response, url)
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    async def put_fragment(
        self, upload_url: str, *, content: bytes, content_range: str
    ) -> tuple[int, Any]:
        """PUT one fragment to a pre-authenticated upload session URL.

        **Deliberately unauthenticated.** The upload URL carries its own
        pre-authentication, and the reference warns: "If you include the
        ``Authorization`` header when issuing the PUT call, it might result in
        an ``HTTP 401 Unauthorized`` response. Only include the Authorization
        header and bearer token when issuing the POST request during the first
        step." This is the one request in the codebase that must not be signed.

        Returns the status alongside the body because the status *is* the
        answer: ``202`` means more fragments are expected, ``200``/``201`` means
        the file is complete and the body is the finished driveItem.
        """
        import httpx

        # Content-Length is httpx's to set from the body; writing it here too
        # would send it twice. Content-Range is ours, and is the only thing
        # telling the service where this fragment belongs.
        headers = {"Content-Range": content_range}
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await client.put(upload_url, headers=headers, content=content)
        self._check(response, upload_url)
        body = response.json() if response.content else None
        return response.status_code, body

    async def post_monitor(self, path: str, json: Any = None) -> str:
        """POST a long-running action and return its monitor URL.

        Graph answers ``202 Accepted`` for actions it performs asynchronously —
        ``copy`` is the one that matters here — with an empty body and a
        ``Location`` header naming a URL that reports progress. The body is
        therefore not the answer, and a caller that reads it gets ``None`` and
        concludes nothing happened.
        """
        import httpx

        url = self._url(path)
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await self._send(client, "POST", url, {}, json)
            if response.status_code == 401:
                self._auth.invalidate()
                response = await self._send(client, "POST", url, {}, json)
            self._check(response, url)
            return str(response.headers.get("location", ""))

    async def paginate(
        self,
        path: str,
        *,
        limit: int,
        params: dict[str, Any] | None = None,
        page_size: int = 200,
        row: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Results[Any]:
        """Follow ``@odata.nextLink`` until ``limit`` items are collected.

        Returns :class:`Results` rather than a list, so a caller can tell a
        complete answer from a truncated one.

        The page sizes callers pass are deliberately conservative. The paging
        reference warns that an over-large ``$top`` "might be ignored, it might
        default to the maximum page size for that API, or Microsoft Graph might
        return an error" — and the first of those is silent, which is the whole
        failure mode ``Results`` exists to prevent.
        """
        first = dict(params or {})

        async def request(asked: dict[str, Any]) -> Any:
            follow = asked.pop(NEXT_URL, None)
            if follow:
                # The next link already encodes every original parameter,
                # $top included. Sending ours again would duplicate the query
                # parameter on a URL that already carries it — and the
                # reference says to use the entire URL as given.
                #
                # Headers are *not* in the link, though, so they are re-sent:
                # a Prefer that shaped page one and not page two would give one
                # result set in two formats.
                return await self.request("GET", follow, headers=headers)
            return await self.request(
                "GET", path, params={**first, **asked}, headers=headers
            )

        return await page_through(
            request,
            style=GRAPH_PAGING,
            limit=limit,
            page_size=page_size,
            row=row,
        )

    # -- convenience ---------------------------------------------------------

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: Any = None, **params: Any) -> Any:
        return await self.request("POST", path, params=params, json=json)

    async def patch(self, path: str, json: Any = None, **params: Any) -> Any:
        return await self.request("PATCH", path, params=params, json=json)

    async def delete(self, path: str, **params: Any) -> Any:
        return await self.request("DELETE", path, params=params)

    # -- internals -----------------------------------------------------------

    def _url(self, path: str) -> str:
        """Join to the base, unless the caller already holds an absolute URL.

        An ``@odata.nextLink`` and an upload session's ``uploadUrl`` are both
        complete URLs handed back by the service — and the second one is not
        even on the Graph host.
        """
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _check(self, response: httpx.Response, url: str) -> None:
        if response.status_code >= 400:
            raise classify(
                response.status_code, _body(response), url, dict(response.headers)
            )

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
        extra: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = await self._auth.headers()
        if extra:
            headers.update(extra)
        # `params or None` is load-bearing, not tidiness. httpx *replaces* a
        # URL's query string whenever params is supplied — an empty dict clears
        # it. Every follow-up page is an absolute @odata.nextLink whose whole
        # meaning lives in its $skiptoken, so passing {} silently re-fetched
        # page one, forever, until the MAX_PAGES backstop cut it off with a
        # result full of duplicates and no error anywhere.
        query = params or None
        if json is not None:
            headers["Content-Type"] = "application/json"
        if content is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            return await client.request(
                method, url, headers=headers, params=query, content=content
            )
        return await client.request(
            method, url, headers=headers, params=query, json=json
        )


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
