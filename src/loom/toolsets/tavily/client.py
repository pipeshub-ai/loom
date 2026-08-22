"""Async Tavily API client — pure httpx, no vendor SDK.

Credentials resolve from an explicit argument, then the environment:

    TAVILY_API_KEY     sent as ``Authorization: Bearer tvly-…``

Three things about this API drive the design here.

**It does not paginate.** ``max_results`` is capped at 20 and there is no
cursor, so every read returns a plain ``list`` (or a wrapper) rather than
:class:`Results`. Asking for more than 20 raises rather than quietly returning
20 — a caller that requested 100 and reported 20 as the total is the failure
this area exists to prevent, and Tavily offers no second page to make up the
difference.

**Quota exhaustion has its own status codes**, and they are not the usual
ones: **432** is the plan limit and **433** the pay-as-you-go limit, neither of
which is in anybody's default 4xx handling. Both are permanent — they clear
when somebody changes a billing plan, not when the request is sent again — so
they are classified apart from 429, which is a genuine rate limit and worth
retrying.

**Extraction fails per URL.** ``/extract`` returns 200 with a
``failed_results`` array; it is carried through rather than dropped.
"""

from __future__ import annotations

from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.tavily.models import (
    TavilyExtraction,
    TavilySearch,
    TavilySiteMap,
)

BASE_URL = "https://api.tavily.com"

#: Tavily's documented ceiling for ``max_results`` on /search.
TAVILY_MAX_RESULTS = 20

#: Ceiling for one ``/extract`` call.
TAVILY_MAX_EXTRACT_URLS = 20

#: Documented limits on the domain filters.
TAVILY_MAX_INCLUDE_DOMAINS = 300
TAVILY_MAX_EXCLUDE_DOMAINS = 150


class TavilyError(WorkflowError):
    """A Tavily request failed. Retryable unless a subclass says otherwise."""

    def __init__(self, message: str, *, status: int = 0, **kw: Any) -> None:
        super().__init__(message)
        self.status = status


class TavilyPermanentError(TavilyError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class TavilyAuthError(TavilyPermanentError):
    """Missing, malformed, or revoked API key."""


class TavilyQuotaExhausted(TavilyPermanentError):  # noqa: N818 - names a state
    """HTTP 432 or 433 — the account's plan or pay-as-you-go limit is spent.

    Deliberately *not* retryable, and deliberately separate from
    :class:`TavilyRateLimited`. A 429 clears by waiting; this clears when
    somebody changes a billing plan. Retrying it burns the workflow's retry
    budget to arrive at the same answer, and reads to an operator as a flaky
    integration rather than as an empty account.
    """


class TavilyRateLimited(TavilyError):  # noqa: N818 - names a state
    """HTTP 429. Retryable, and the caller should back off."""

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class TavilyClient:
    """Thin async wrapper around the Tavily API.

    Parameters
    ----------
    api_key:
        A Tavily API key (``tvly-…``). Falls back to ``TAVILY_API_KEY``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
    ) -> None:
        self._key = api_key
        self._base_url = base_url.rstrip("/")
        # Generous, because /extract and /map are crawls rather than lookups
        # and Tavily's own per-request timeout goes to 150s.
        self._timeout = timeout

        if not self._key:
            raise TavilyAuthError(
                "Tavily needs an API key: set TAVILY_API_KEY or pass api_key=. "
                "Keys are issued at https://app.tavily.com."
            )

    # -- transport ----------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.post(
                f"{self._base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=_clean(payload),
            )
        if response.status_code >= 400:
            raise _classify(response)
        if not response.content:
            return {}
        return response.json()

    # -- search -------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        topic: str = "general",
        search_depth: str = "basic",
        include_answer: bool = False,
        include_raw_content: bool = False,
        include_images: bool = False,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        time_range: str = "",
        start_date: str = "",
        end_date: str = "",
        country: str = "",
        chunks_per_source: int = 0,
    ) -> TavilySearch:
        """Search the web.

        Returns a wrapper rather than a bare list because ``include_answer``
        puts a written answer beside the results, and returning only the list
        would discard the thing that was asked for.
        """
        _check_domains(include_domains, exclude_domains)
        payload: dict[str, Any] = {
            "query": query,
            "max_results": _bounded(max_results, TAVILY_MAX_RESULTS, "max_results"),
            "topic": topic or None,
            "search_depth": search_depth or None,
            "include_answer": include_answer or None,
            "include_raw_content": include_raw_content or None,
            "include_images": include_images or None,
            "include_domains": include_domains or None,
            "exclude_domains": exclude_domains or None,
            "time_range": time_range or None,
            "start_date": start_date or None,
            "end_date": end_date or None,
            "country": country or None,
            "chunks_per_source": chunks_per_source or None,
        }
        return TavilySearch.from_api(await self._post("/search", payload))

    async def extract(
        self,
        urls: list[str],
        *,
        extract_depth: str = "basic",
        output_format: str = "markdown",
        include_images: bool = False,
        rank_by: str = "",
        read_timeout: float = 0.0,
    ) -> TavilyExtraction:
        """Read the full text of pages you already have URLs for."""
        if not urls:
            raise TavilyPermanentError("extract needs at least one URL")
        if len(urls) > TAVILY_MAX_EXTRACT_URLS:
            raise TavilyPermanentError(
                f"Tavily accepts at most {TAVILY_MAX_EXTRACT_URLS} URLs per "
                f"/extract call, got {len(urls)}. Split the list across calls."
            )

        payload: dict[str, Any] = {
            "urls": urls,
            "extract_depth": extract_depth or None,
            "format": output_format or None,
            "include_images": include_images or None,
            "query": rank_by or None,
            "timeout": read_timeout or None,
        }
        return TavilyExtraction.from_api(await self._post("/extract", payload))

    async def map_site(
        self,
        url: str,
        *,
        max_depth: int = 1,
        max_breadth: int = 20,
        limit: int = 50,
        instructions: str = "",
        select_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
        allow_external: bool = False,
        read_timeout: float = 0.0,
    ) -> TavilySiteMap:
        """Discover the URLs under a site.

        ``allow_external`` defaults to False, unlike the API. "Map this site"
        means this site; the API's default wanders off it, and the extra URLs
        are indistinguishable from the ones that belong.
        """
        payload: dict[str, Any] = {
            "url": url,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "limit": limit,
            "instructions": instructions or None,
            "select_paths": select_paths or None,
            "exclude_paths": exclude_paths or None,
            "allow_external": allow_external,
            "timeout": read_timeout or None,
        }
        return TavilySiteMap.from_api(await self._post("/map", payload))


def _bounded(value: int, ceiling: int, field: str) -> int:
    """Refuse a request above the API's ceiling rather than quietly capping it.

    See ``exa/client.py::_bounded`` — same reasoning, same failure being
    avoided: Tavily has no cursor, so a silently capped request reports a
    fraction of the data as the whole of it.
    """
    if value < 1:
        raise TavilyPermanentError(f"{field} must be at least 1, got {value}")
    if value > ceiling:
        raise TavilyPermanentError(
            f"Tavily returns at most {ceiling} results per call and does not "
            f"paginate, so {field}={value} cannot be satisfied. Ask for "
            f"{ceiling} or fewer, or narrow the query."
        )
    return value


def _check_domains(
    include: list[str] | None, exclude: list[str] | None
) -> None:
    """Fail on an over-long filter here rather than as an opaque 400."""
    if include and len(include) > TAVILY_MAX_INCLUDE_DOMAINS:
        raise TavilyPermanentError(
            f"include_domains takes at most {TAVILY_MAX_INCLUDE_DOMAINS} "
            f"entries, got {len(include)}"
        )
    if exclude and len(exclude) > TAVILY_MAX_EXCLUDE_DOMAINS:
        raise TavilyPermanentError(
            f"exclude_domains takes at most {TAVILY_MAX_EXCLUDE_DOMAINS} "
            f"entries, got {len(exclude)}"
        )


def _clean(payload: Any) -> Any:
    """Drop unset fields so Tavily applies its own defaults."""
    if not isinstance(payload, dict):
        return payload
    return {k: v for k, v in payload.items() if v is not None}


def _classify(response: Any) -> TavilyError:
    """Turn a failed response into the narrowest error that fits."""
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    detail = ""
    if isinstance(body, dict):
        error = body.get("detail") or body.get("error") or body.get("message")
        detail = error.get("error", "") if isinstance(error, dict) else str(error or "")
    message = f"Tavily {status}: {detail or (response.text or 'request failed')[:200]}"

    if status == 429:
        return TavilyRateLimited(
            message,
            status=status,
            retry_after=float(response.headers.get("Retry-After", 0) or 0),
        )
    # 432 plan limit, 433 pay-as-you-go limit. Neither is in a default 4xx
    # handler's vocabulary, and neither clears by waiting.
    if status in (432, 433):
        return TavilyQuotaExhausted(message, status=status)
    if status in (401, 403):
        return TavilyAuthError(message, status=status)
    if 400 <= status < 500:
        return TavilyPermanentError(message, status=status)
    return TavilyError(message, status=status)


