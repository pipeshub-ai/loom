"""DuckDuckGo search client, over the third-party ``ddgs`` package.

    pip install 'loomsdk[duckduckgo]'

No credentials. ``DDGS_PROXY`` is honoured by ``ddgs`` itself if you need to
route through one.

**This is not an official API, and the difference matters.** DuckDuckGo
publishes no web-search API — the only endpoint they document,
``api.duckduckgo.com``, returns instant answers and no web results at all.
``ddgs`` parses search result pages instead. Three consequences, each handled
here as well as it can be:

**A block is an error, not an empty list.** ``ddgs`` raises
``RatelimitException`` when it is turned away, and this client turns that into a
*retryable* :class:`DuckDuckGoRateLimited` rather than letting it surface as
zero results. A search that quietly returns ``[]`` when it was blocked is the
worst failure this toolset can have: the workflow reads it as "nothing matched"
and carries on. What cannot be fixed is a *soft* block, where the upstream page
returns no rows and no error — that is genuinely indistinguishable from a query
nothing matched, and no amount of care here changes it. Treat a suspiciously
empty result the way you would treat a flaky scraper, because that is what this
is.

**It pages, and it under-delivers quietly.** Asking ``ddgs`` for 30 results
returns whatever it managed — 24, in the run that motivated this code — with no
field saying so. So this client does the paging itself, one page per request,
and returns :class:`Results`: ``complete`` is then the honest answer to "did I
see everything I asked for?" rather than something the caller has to infer from
a row count.

**It is synchronous.** Every call is wrapped in :func:`asyncio.to_thread`, so a
search does not block the event loop for the seconds it spends in HTTP.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loom.core.exceptions import ConfigurationError, NonRetryableError, WorkflowError
from loom.toolsets.duckduckgo.models import (
    DuckDuckGoImage,
    DuckDuckGoNews,
    DuckDuckGoResult,
)
from loom.toolsets.pagination import Page, Results, collect

#: Rows to request per page. Fixed rather than derived from what the caller
#: still needs: ``ddgs`` maps ``page`` onto the upstream engine's own
#: pagination, so varying the size between pages would shift the window and
#: either skip or repeat rows. ``collect`` trims the overshoot and reports the
#: coverage, which is exactly the job it exists for.
DDG_PAGE_SIZE = 20

#: Stop after this many pages however large the limit. A scraped source that
#: keeps answering is not proof there is more behind it.
DDG_MAX_PAGES = 10


class DuckDuckGoError(WorkflowError):
    """A DuckDuckGo search failed. Retryable unless a subclass says otherwise."""


class DuckDuckGoPermanentError(DuckDuckGoError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class DuckDuckGoRateLimited(DuckDuckGoError):  # noqa: N818 - names a state
    """Turned away for asking too often. Retryable, and back off first.

    Its own class because it is the failure this toolset has most of, and
    because the alternative — reporting it as zero results — would have a
    workflow act on "nothing matched" when nothing was searched.
    """


class DuckDuckGoClient:
    """Search DuckDuckGo (and the other engines ``ddgs`` can reach).

    Parameters
    ----------
    proxy:
        An http/https/socks5 proxy URL. Falls back to ``DDGS_PROXY``.
    request_timeout:
        Seconds for one HTTP request inside ``ddgs``.
    """

    def __init__(
        self,
        *,
        proxy: str | None = None,
        request_timeout: int = 20,
    ) -> None:
        self._proxy = proxy
        self._request_timeout = request_timeout

    def _ddgs(self) -> Any:
        """Build a ``ddgs`` client, or explain how to install it.

        Imported per call rather than at module scope so that importing this
        toolset — which the catalog does at registration — costs nothing and
        works without the extra installed.
        """
        try:
            from ddgs import DDGS
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ConfigurationError(
                "The duckduckgo toolset needs the ddgs package: "
                "pip install 'loomsdk[duckduckgo]'. Note that ddgs parses "
                "search result pages rather than calling an API — DuckDuckGo "
                "publishes no web-search API. For a supported search API use "
                "the exa or tavily toolsets."
            ) from exc
        return DDGS(proxy=self._proxy, timeout=self._request_timeout)

    # -- transport ----------------------------------------------------------

    async def _call(
        self, category: str, query: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """One page from ``ddgs``, off the event loop, with errors classified."""
        client = self._ddgs()
        method = getattr(client, category)

        def run() -> list[dict[str, Any]]:
            rows = method(query, **{k: v for k, v in kwargs.items() if v is not None})
            return list(rows or [])

        try:
            # ddgs is synchronous and spends seconds in HTTP. Running it inline
            # would stall every other step sharing this event loop.
            return await asyncio.to_thread(run)
        except Exception as exc:
            raise _classify(exc) from exc

    async def _paged(
        self,
        category: str,
        query: str,
        *,
        max_results: int,
        row: Any,
        **kwargs: Any,
    ) -> Results[Any]:
        """Walk pages until *max_results* rows, or the source runs out.

        Deduplicates by URL across pages, because a scraped source repeating a
        row between pages is ordinary and a caller counting results should not
        have to know that.

        Two things end the walk, and both are stated here rather than left to
        ``collect``'s runaway guard:

        * **A short page** — fewer rows than asked for means the source ran out.
        * **A page with no new rows** — every row already seen. For a scraped
          source that is how it says it has nothing more; asking again is far
          likelier to loop than to turn up something fresh.

        The second is why the raw count alone is not the test. It also means
        ``complete=True`` here says "stopped giving new rows", which for this
        source is the strongest form of exhausted available.
        """
        seen: set[str] = set()

        async def fetch(cursor: str | None, _size: int) -> Page:
            number = int(cursor or "1")
            raw = await self._call(
                category, query, page=number, max_results=DDG_PAGE_SIZE, **kwargs
            )
            fresh = []
            for index, item in enumerate(raw):
                model = row(item)
                # A row with neither a URL nor a title cannot be deduplicated
                # against anything, so it is keyed by where it appeared instead
                # of being dropped. Dropping was the first version, and it was
                # silent data loss inside a read whose entire purpose is to
                # report its own coverage honestly.
                key = model.url or model.title or f"#{number}.{index}"
                if key not in seen:
                    seen.add(key)
                    fresh.append(model)
            exhausted = len(raw) < DDG_PAGE_SIZE or not fresh
            return Page(items=fresh, cursor=None if exhausted else str(number + 1))

        return await collect(
            fetch,
            limit=max_results,
            page_size=DDG_PAGE_SIZE,
            max_pages=DDG_MAX_PAGES,
        )

    # -- searches -----------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        region: str = "us-en",
        safesearch: str = "moderate",
        timelimit: str = "",
        backend: str = "auto",
    ) -> Results[DuckDuckGoResult]:
        """Web search."""
        return await self._paged(
            "text",
            query,
            max_results=max_results,
            row=DuckDuckGoResult.from_api,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit or None,
            backend=backend,
        )

    async def news(
        self,
        query: str,
        *,
        max_results: int = 10,
        region: str = "us-en",
        safesearch: str = "moderate",
        timelimit: str = "",
        backend: str = "auto",
    ) -> Results[DuckDuckGoNews]:
        """News search."""
        return await self._paged(
            "news",
            query,
            max_results=max_results,
            row=DuckDuckGoNews.from_api,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit or None,
            backend=backend,
        )

    async def images(
        self,
        query: str,
        *,
        max_results: int = 10,
        region: str = "us-en",
        safesearch: str = "moderate",
        timelimit: str = "",
        backend: str = "auto",
    ) -> Results[DuckDuckGoImage]:
        """Image search."""
        return await self._paged(
            "images",
            query,
            max_results=max_results,
            row=DuckDuckGoImage.from_api,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit or None,
            backend=backend,
        )


def _classify(exc: BaseException) -> DuckDuckGoError:
    """Turn a ``ddgs`` failure into the narrowest error that fits.

    Matched on class *name* rather than by importing the exception types,
    because ``ddgs`` is an optional dependency and this module has to be
    importable without it. Names are checked across the whole MRO so a future
    subclass still lands in the right bucket.
    """
    if isinstance(exc, ConfigurationError | DuckDuckGoError):
        return exc if isinstance(exc, DuckDuckGoError) else DuckDuckGoError(str(exc))

    names = {base.__name__ for base in type(exc).__mro__}
    message = f"DuckDuckGo search failed: {type(exc).__name__}: {exc}"

    if "RatelimitException" in names:
        return DuckDuckGoRateLimited(
            f"{message}. DuckDuckGo is turning this client away; back off, or "
            "use the exa or tavily toolsets, which have supported APIs."
        )
    if "TimeoutException" in names:
        return DuckDuckGoError(message)
    if "DDGSException" in names:
        return DuckDuckGoPermanentError(message)
    return DuckDuckGoError(message)


