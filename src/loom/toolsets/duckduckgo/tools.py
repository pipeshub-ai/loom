"""DuckDuckGo step functions for use inside LOOM workflows.

Each is a ``@step``, so it journals, retries per its own policy, and can be
called with ``ctx.step(...)``::

    from loom.toolsets.duckduckgo.tools import ddg_search

    found = await ctx.step(ddg_search, "durable execution", max_results=25)
    if not found.complete:
        await ctx.report(found.summary())

Needs ``pip install 'loomsdk[duckduckgo]'``.

All three return :class:`Results`. ``complete`` is True only when the source
was genuinely exhausted, so a short answer is no longer ambiguous: ``ddgs``
asked for 30 returns whatever it managed with no field saying so, and 24 rows
could equally mean "that is all there is" or "it stopped early". Here the first
case comes back ``complete=True`` and the second ``complete=False`` with a
cursor to resume from.

Every operation reads, so all of them retry — and unlike Exa and Tavily, the
retry here is load-bearing rather than incidental: being turned away is the
common failure, and it is classified retryable precisely so a workflow backs
off instead of treating a block as "nothing matched".
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.duckduckgo.models import (
    DuckDuckGoImage,
    DuckDuckGoNews,
    DuckDuckGoResult,
)
from loom.toolsets.pagination import Results

#: More attempts and a longer wait than the other web toolsets. This is a
#: scraped source: being turned away is routine rather than exceptional, and
#: backing off is usually all it takes.
_READ = Retry(max_attempts=4, initial_delay=2.0)


@step(retry=_READ)
async def ddg_search(
    query: str,
    max_results: int = 10,
    region: str = "us-en",
    safesearch: str = "moderate",
    timelimit: str = "",
    backend: str = "auto",
) -> Results[DuckDuckGoResult]:
    """Search the web via DuckDuckGo.

    No API key. Note that this parses search result pages rather than calling
    an API — DuckDuckGo publishes no web-search API — so it is best-effort.
    Prefer ``exa_search`` or ``tavily_search`` where reliability matters.

    Args:
        query: What to search for.
        max_results: How many results to return. Pages are followed to reach
            it; check ``.complete`` on the way out.
        region: Locale, e.g. ``us-en``, ``uk-en``, ``de-de``.
        safesearch: ``on``, ``moderate`` (default), or ``off``.
        timelimit: Restrict by age: ``d`` day, ``w`` week, ``m`` month,
            ``y`` year. Empty means no restriction.
        backend: Which engines to try. ``auto`` (default) tries several; name
            one — ``duckduckgo``, ``brave``, ``mojeek``, ``wikipedia`` — to
            pin it.

    Returns:
        Matching pages, deduplicated by URL across pages. ``.complete`` is
        True only when the source ran out — so ``complete=True`` with fewer
        rows than ``max_results`` means that really is everything, and
        ``complete=False`` means more sits behind ``.cursor``.
    """
    from loom.toolsets.duckduckgo.client import get_default_client

    return await get_default_client().search(
        query,
        max_results=max_results,
        region=region,
        safesearch=safesearch,
        timelimit=timelimit,
        backend=backend,
    )


@step(retry=_READ)
async def ddg_news(
    query: str,
    max_results: int = 10,
    region: str = "us-en",
    safesearch: str = "moderate",
    timelimit: str = "",
    backend: str = "auto",
) -> Results[DuckDuckGoNews]:
    """Search news via DuckDuckGo.

    Args:
        query: What to search for.
        max_results: How many results to return across pages.
        region: Locale, e.g. ``us-en``.
        safesearch: ``on``, ``moderate`` (default), or ``off``.
        timelimit: ``d``, ``w``, ``m``, or ``y``. Usually what you want here.
        backend: Which engines to try. ``auto`` by default.

    Returns:
        News items with a publication date and source, deduplicated by URL.
        ``.complete`` is True only when the source ran out; False means
        more sits behind ``.cursor``.
    """
    from loom.toolsets.duckduckgo.client import get_default_client

    return await get_default_client().news(
        query,
        max_results=max_results,
        region=region,
        safesearch=safesearch,
        timelimit=timelimit,
        backend=backend,
    )


@step(retry=_READ)
async def ddg_images(
    query: str,
    max_results: int = 10,
    region: str = "us-en",
    safesearch: str = "moderate",
    timelimit: str = "",
    backend: str = "auto",
) -> Results[DuckDuckGoImage]:
    """Search images via DuckDuckGo.

    Args:
        query: What to search for.
        max_results: How many results to return across pages.
        region: Locale, e.g. ``us-en``.
        safesearch: ``on``, ``moderate`` (default), or ``off``.
        timelimit: ``d``, ``w``, ``m``, or ``y``.
        backend: Which engines to try. ``auto`` by default.

    Returns:
        Images with the direct image URL, a thumbnail, and the page each
        appears on. ``.complete`` is True only when the source ran out; False means
        more sits behind ``.cursor``.
    """
    from loom.toolsets.duckduckgo.client import get_default_client

    return await get_default_client().images(
        query,
        max_results=max_results,
        region=region,
        safesearch=safesearch,
        timelimit=timelimit,
        backend=backend,
    )
