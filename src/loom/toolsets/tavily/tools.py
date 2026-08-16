"""Tavily step functions for use inside LOOM workflows.

Each is a ``@step``, so it journals, retries per its own policy, and can be
called with ``ctx.step(...)``::

    from loom.toolsets.tavily.tools import tavily_search, tavily_extract

    found = await ctx.step(tavily_search, "who acquired Wiz", include_answer=True)
    pages = await ctx.step(tavily_extract, [r.url for r in found.results[:3]])

Every operation here reads, so all of them retry — there is no write to
double-send. That also makes them a *taint source* under ``TaintBroker``: a run
that has searched the web is holding text nobody reviewed, and the next write
should want a human.

One naming note: Tavily's own ``timeout`` parameter is exposed here as
``read_timeout``. ``ctx.step`` claims ``timeout`` for itself, so a tool
declaring it is unreachable by keyword — ``ctx.step(tavily_extract,
timeout=30)`` would set the *step's* timeout and call the tool without one.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.tavily.models import TavilyExtraction, TavilySearch, TavilySiteMap

#: Reads, so retrying is free. Note that a spent quota (432/433) is classified
#: non-retryable in the client, so this budget is not burned on an empty
#: account.
_READ = Retry(max_attempts=3, initial_delay=1.0)

#: Crawls rather than lookups, and slow enough that a retry storm is its own
#: problem. One retry, then report.
_CRAWL = Retry(max_attempts=2, initial_delay=2.0)


@step(retry=_READ)
async def tavily_search(
    query: str,
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
    """Search the web with Tavily.

    Args:
        query: What to search for, in plain language.
        max_results: How many results to return, 1-20. Tavily does not
            paginate, so asking for more than 20 raises rather than silently
            returning 20.
        topic: ``general`` (default), ``news``, or ``finance``. Use ``news``
            when recency matters — it is also the only topic that populates
            ``published_date``.
        search_depth: ``basic`` (default, 1 credit) or ``advanced`` (2
            credits, better recall on hard queries).
        include_answer: Also return a written answer to the query. This is
            Tavily's distinguishing feature and arrives beside the results.
        include_raw_content: Also return each page's full text. Large; prefer
            the default snippets when feeding a model.
        include_images: Also return image URLs related to the query.
        include_domains: Only search these domains, at most 300.
        exclude_domains: Never search these domains, at most 150.
        time_range: ``day``, ``week``, ``month``, or ``year``.
        start_date: ``YYYY-MM-DD`` lower bound on publication date.
        end_date: ``YYYY-MM-DD`` upper bound on publication date.
        country: Two-letter code boosting results from one country. Only
            applies to the ``general`` topic.
        chunks_per_source: How many excerpts to take per page, 1-3. 0 means
            Tavily's default.

    Returns:
        The matching pages, plus the written answer when one was requested.
    """
    from loom.toolsets.tavily.client import get_default_client

    return await get_default_client().search(
        query,
        max_results=max_results,
        topic=topic,
        search_depth=search_depth,
        include_answer=include_answer,
        include_raw_content=include_raw_content,
        include_images=include_images,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        time_range=time_range,
        start_date=start_date,
        end_date=end_date,
        country=country,
        chunks_per_source=chunks_per_source,
    )


@step(retry=_READ)
async def tavily_extract(
    urls: list[str],
    extract_depth: str = "basic",
    output_format: str = "markdown",
    include_images: bool = False,
    rank_by: str = "",
    read_timeout: float = 0.0,
) -> TavilyExtraction:
    """Read the full text of pages you already have URLs for.

    Args:
        urls: Pages to read, 1-20 per call.
        extract_depth: ``basic`` (default) or ``advanced``, which is slower
            and better on pages that fight scrapers.
        output_format: ``markdown`` (default) or ``text``.
        include_images: Also return image URLs found on each page.
        rank_by: Re-rank each page's excerpts against this question. Empty
            returns the page in document order.
        read_timeout: Seconds Tavily may spend per request, 1-60. 0 means its
            own default. Named ``read_timeout`` because ``ctx.step`` claims
            ``timeout``.

    Returns:
        The pages that were read, plus a ``failed`` entry for every URL that
        was not. Check ``.failed`` before treating ``.pages`` as the whole
        request — this endpoint returns 200 when some URLs could not be read.
    """
    from loom.toolsets.tavily.client import get_default_client

    return await get_default_client().extract(
        urls,
        extract_depth=extract_depth,
        output_format=output_format,
        include_images=include_images,
        rank_by=rank_by,
        read_timeout=read_timeout,
    )


@step(retry=_CRAWL)
async def tavily_map_site(
    url: str,
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

    The step before a bulk read: map a docs site, then hand the URLs to
    ``tavily_extract``.

    Args:
        url: Root URL to start from.
        max_depth: How many link levels to follow, 1-5. Defaults to 1.
        max_breadth: How many links to follow per level, 1-500.
        limit: Total links to process before stopping.
        instructions: Plain-language guidance about what to look for. Doubles
            the credit cost.
        select_paths: Regex patterns a URL path must match to be kept.
        exclude_paths: Regex patterns that drop a URL path.
        allow_external: Follow links off the starting domain. Defaults to
            False, unlike the API — "map this site" means this site, and
            wandered-off URLs are indistinguishable from the ones that belong.
        read_timeout: Seconds Tavily may spend, 10-150. 0 means its default.

    Returns:
        The discovered URLs under ``base_url``.
    """
    from loom.toolsets.tavily.client import get_default_client

    return await get_default_client().map_site(
        url,
        max_depth=max_depth,
        max_breadth=max_breadth,
        limit=limit,
        instructions=instructions,
        select_paths=select_paths,
        exclude_paths=exclude_paths,
        allow_external=allow_external,
        read_timeout=read_timeout,
    )
