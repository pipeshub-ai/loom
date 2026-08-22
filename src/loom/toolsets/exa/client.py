"""Async Exa API client — pure httpx, no vendor SDK.

Credentials resolve from an explicit argument, then the environment:

    EXA_API_KEY     sent as the ``x-api-key`` header

Three things about this API drive the design here.

**It does not paginate.** There is no cursor, offset, or page token on any
endpoint; ``numResults`` is capped at 100 and that is the whole answer. So every
read returns a plain ``list`` rather than :class:`Results` — declaring the
latter would promise a coverage guarantee the API cannot keep. What the client
*can* do is refuse to hide the ceiling: asking for more than 100 raises rather
than silently returning 100, because a workflow that requested 500 and reported
100 as the total is the failure this whole area exists to prevent.

**Contents fail per URL.** ``/contents`` returns 200 with a ``statuses`` array
saying which URLs did not come back. That array is carried through to
:class:`ExaContents.failed` rather than dropped, for the same reason.

**402 is not a 4xx like the others.** Running out of credits is permanent until
somebody tops up an account, so retrying spends three attempts to learn nothing.
It gets its own error class so a workflow can tell "your query was malformed"
from "your account is empty".
"""

from __future__ import annotations

from typing import Any

from loom.core.exceptions import NonRetryableError, WorkflowError
from loom.toolsets.exa.models import ExaAnswer, ExaContents, ExaResult

BASE_URL = "https://api.exa.ai"

#: Exa's documented ceiling for ``numResults`` on search and findSimilar.
#: Higher limits exist but are negotiated with their sales team, so a client
#: that assumed one would fail for everybody else.
EXA_MAX_RESULTS = 100

#: Ceiling for one ``/contents`` call, per Exa's schema (1-100 URLs).
EXA_MAX_URLS = 100


class ExaError(WorkflowError):
    """An Exa request failed. Retryable unless a subclass says otherwise."""

    def __init__(self, message: str, *, status: int = 0, **kw: Any) -> None:
        super().__init__(message)
        self.status = status


class ExaPermanentError(ExaError, NonRetryableError):
    """A request that fails the same way however often it is sent.

    The two-level shape is load-bearing: a flat
    ``class E(WorkflowError, NonRetryableError)`` has no consistent MRO and
    fails at import.
    """


class ExaAuthError(ExaPermanentError):
    """Missing, malformed, or revoked API key."""


class ExaCreditsExhausted(ExaPermanentError):  # noqa: N818 - names a state
    """HTTP 402 — the account is out of credits.

    Permanent on purpose. It resolves when somebody adds credit, not when the
    request is sent again, and it is the one Exa failure a workflow can act on
    by falling back to another provider rather than by waiting.
    """


class ExaRateLimited(ExaError):  # noqa: N818 - names a state
    """Too many requests. Retryable, and the caller should back off."""

    def __init__(self, message: str, *, retry_after: float = 0.0, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ExaClient:
    """Thin async wrapper around the Exa search API.

    Parameters
    ----------
    api_key:
        An Exa API key. Falls back to ``EXA_API_KEY``.
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self._key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

        if not self._key:
            raise ExaAuthError(
                "Exa needs an API key: set EXA_API_KEY or pass api_key=. "
                "Keys are issued at https://dashboard.exa.ai/api-keys."
            )

    # -- transport ----------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.post(
                f"{self._base_url}{path}",
                headers={
                    "x-api-key": self._key,
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
        num_results: int = 10,
        search_type: str = "auto",
        category: str = "",
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        start_published_date: str = "",
        end_published_date: str = "",
        include_text: bool = False,
        include_highlights: bool = False,
        summary_query: str = "",
        max_characters: int = 0,
    ) -> list[ExaResult]:
        """Search the web.

        Returns a plain list rather than :class:`Results`: Exa has no cursor on
        any endpoint, so there is no page to follow and no coverage to report.
        """
        payload: dict[str, Any] = {
            "query": query,
            "numResults": _bounded(num_results, EXA_MAX_RESULTS, "num_results"),
            "type": search_type or None,
            "category": category or None,
            "includeDomains": include_domains or None,
            "excludeDomains": exclude_domains or None,
            "startPublishedDate": start_published_date or None,
            "endPublishedDate": end_published_date or None,
            "contents": _contents(
                include_text, include_highlights, summary_query, max_characters
            ),
        }
        body = await self._post("/search", payload)
        return [ExaResult.from_api(r) for r in (body.get("results") or [])]

    async def find_similar(
        self,
        url: str,
        *,
        num_results: int = 10,
        exclude_source_domain: bool = True,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        start_published_date: str = "",
        end_published_date: str = "",
        include_text: bool = False,
        include_highlights: bool = False,
    ) -> list[ExaResult]:
        """Pages semantically similar to one you already have.

        ``exclude_source_domain`` defaults to True, unlike the API, whose
        default is False. "Find me more like this" almost always means more
        from *elsewhere*; leaving it off returns a page of neighbours from the
        same site, which reads like the search failed.
        """
        payload: dict[str, Any] = {
            "url": url,
            "numResults": _bounded(num_results, EXA_MAX_RESULTS, "num_results"),
            "excludeSourceDomain": exclude_source_domain,
            "includeDomains": include_domains or None,
            "excludeDomains": exclude_domains or None,
            "startPublishedDate": start_published_date or None,
            "endPublishedDate": end_published_date or None,
            "contents": _contents(include_text, include_highlights, "", 0),
        }
        body = await self._post("/findSimilar", payload)
        return [ExaResult.from_api(r) for r in (body.get("results") or [])]

    async def get_contents(
        self,
        urls: list[str],
        *,
        include_text: bool = True,
        include_highlights: bool = False,
        summary_query: str = "",
        max_characters: int = 0,
        highlights_query: str = "",
    ) -> ExaContents:
        """Fetch the text of pages you already have URLs for.

        Carries the per-URL ``statuses`` through, because this endpoint answers
        200 for a request in which some URLs failed.
        """
        if not urls:
            raise ExaPermanentError("get_contents needs at least one URL")
        if len(urls) > EXA_MAX_URLS:
            raise ExaPermanentError(
                f"Exa accepts at most {EXA_MAX_URLS} URLs per /contents call, "
                f"got {len(urls)}. Split the list across calls."
            )

        contents = _contents(
            include_text, include_highlights, summary_query, max_characters
        ) or {}
        if highlights_query:
            contents["highlights"] = {"query": highlights_query}
        body = await self._post("/contents", {"urls": urls, **contents})
        return ExaContents.from_api(body)

    async def answer(self, query: str, *, include_text: bool = False) -> ExaAnswer:
        """Ask a question and get an answer with citations."""
        body = await self._post("/answer", {"query": query, "text": include_text})
        return ExaAnswer.from_api(body)


def _contents(
    include_text: bool,
    include_highlights: bool,
    summary_query: str,
    max_characters: int,
) -> dict[str, Any] | None:
    """Build the ``contents`` sub-object, or ``None`` when nothing was asked for.

    Sending ``{}`` and sending nothing are not the same to Exa, and an empty
    object costs a crawl for content the caller never wanted.
    """
    contents: dict[str, Any] = {}
    if include_text:
        contents["text"] = (
            {"maxCharacters": max_characters} if max_characters > 0 else True
        )
    if include_highlights:
        contents["highlights"] = True
    if summary_query:
        contents["summary"] = {"query": summary_query}
    return contents or None


def _bounded(value: int, ceiling: int, field: str) -> int:
    """Refuse a request above the API's ceiling rather than quietly capping it.

    Clamping is the tempting move and the wrong one: a caller that asked for
    500 and received 100 has no way to tell that from 100 being all there was,
    so it reports a fifth of the data as the total. Exa offers no cursor to
    make up the difference, so the honest failure is at the door.
    """
    if value < 1:
        raise ExaPermanentError(f"{field} must be at least 1, got {value}")
    if value > ceiling:
        raise ExaPermanentError(
            f"Exa returns at most {ceiling} results per call and does not "
            f"paginate, so {field}={value} cannot be satisfied. Ask for "
            f"{ceiling} or fewer, or narrow the query."
        )
    return value


def _clean(payload: Any) -> Any:
    """Drop unset fields — Exa rejects nulls for several of them."""
    if not isinstance(payload, dict):
        return payload
    return {k: v for k, v in payload.items() if v is not None}


def _classify(response: Any) -> ExaError:
    """Turn a failed response into the narrowest error that fits."""
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("error") or body.get("message") or "")
    message = f"Exa {status}: {detail or (response.text or 'request failed')[:200]}"

    if status == 429:
        return ExaRateLimited(
            message,
            status=status,
            retry_after=float(response.headers.get("Retry-After", 0) or 0),
        )
    if status == 402:
        return ExaCreditsExhausted(message, status=status)
    if status in (401, 403):
        return ExaAuthError(message, status=status)
    if 400 <= status < 500:
        return ExaPermanentError(message, status=status)
    return ExaError(message, status=status)


