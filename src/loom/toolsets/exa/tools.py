"""Exa step functions for use inside LOOM workflows.

Each is a ``@step``, so it journals, retries per its own policy, and can be
called with ``ctx.step(...)``::

    from loom.toolsets.exa.tools import exa_search, exa_get_contents

    hits  = await ctx.step(exa_search, "durable execution engines", num_results=5)
    pages = await ctx.step(exa_get_contents, [h.url for h in hits])

Every operation here reads, so all of them retry — there is no write to
double-send. That also makes them the canonical *taint source* under
``TaintBroker``: a run that has searched the web is holding text nobody
reviewed, and the next write should want a human.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.exa.client import ExaClient
from loom.toolsets.exa.models import ExaAnswer, ExaContents, ExaResult

#: Reads, so retrying is free. The delay is generous because Exa's 429 is the
#: failure most likely to clear on its own.
_READ = Retry(max_attempts=3, initial_delay=1.0)


@step(retry=_READ)
async def exa_search(
    query: str,
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
    """Search the web with Exa.

    Args:
        query: What to search for. Exa is embeddings-based, so a descriptive
            phrase ("papers arguing that X") works better than keywords.
        num_results: How many results to return, 1-100. Exa does not
            paginate, so asking for more than 100 raises rather than
            silently returning 100.
        search_type: ``auto`` (default), ``fast``, ``neural``, ``instant``,
            ``deep``, or ``deep-reasoning``.
        category: Narrow to one kind of page: ``company``, ``research paper``,
            ``news``, ``pdf``, ``github``, ``personal site``, ``people``, or
            ``financial report``.
        include_domains: Only return pages from these domains.
        exclude_domains: Never return pages from these domains.
        start_published_date: ISO 8601. Only pages published on or after it.
        end_published_date: ISO 8601. Only pages published on or before it.
        include_text: Also return each page's full text. Costs more and makes
            the result much larger; prefer highlights when feeding a model.
        include_highlights: Also return query-relevant snippets from each page.
        summary_query: Ask for an LLM summary of each page, guided by this
            question. Empty means no summary.
        max_characters: Cap the text returned per page. 0 means Exa's default.

    Returns:
        Matching pages, most relevant first. A plain list, not ``Results``:
        Exa has no cursor, so there is no page to follow.
    """

    from loom.toolsets.factory import client_for

    return await (await client_for("exa", ExaClient)).search(
        query,
        num_results=num_results,
        search_type=search_type,
        category=category,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        start_published_date=start_published_date,
        end_published_date=end_published_date,
        include_text=include_text,
        include_highlights=include_highlights,
        summary_query=summary_query,
        max_characters=max_characters,
    )


@step(retry=_READ)
async def exa_find_similar(
    url: str,
    num_results: int = 10,
    exclude_source_domain: bool = True,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str = "",
    end_published_date: str = "",
    include_text: bool = False,
    include_highlights: bool = False,
) -> list[ExaResult]:
    """Find pages semantically similar to one you already have.

    Args:
        url: The page to find neighbours of.
        num_results: How many to return, 1-100.
        exclude_source_domain: Skip results from the same site as ``url``.
            Defaults to True, unlike the API — "more like this" usually means
            more from elsewhere.
        include_domains: Only return pages from these domains.
        exclude_domains: Never return pages from these domains.
        start_published_date: ISO 8601 lower bound on publication date.
        end_published_date: ISO 8601 upper bound on publication date.
        include_text: Also return each page's full text.
        include_highlights: Also return relevant snippets from each page.

    Returns:
        Similar pages, most similar first.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("exa", ExaClient)).find_similar(
        url,
        num_results=num_results,
        exclude_source_domain=exclude_source_domain,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        start_published_date=start_published_date,
        end_published_date=end_published_date,
        include_text=include_text,
        include_highlights=include_highlights,
    )


@step(retry=_READ)
async def exa_get_contents(
    urls: list[str],
    include_text: bool = True,
    include_highlights: bool = False,
    summary_query: str = "",
    max_characters: int = 0,
    highlights_query: str = "",
) -> ExaContents:
    """Fetch the text of pages you already have URLs for.

    Args:
        urls: Pages to fetch, 1-100 per call. Exa document ids also work.
        include_text: Return each page's full text. Defaults to True, which is
            the reason to call this at all.
        include_highlights: Also return relevant snippets.
        summary_query: Ask for an LLM summary of each page, guided by this
            question.
        max_characters: Cap the text returned per page. 0 means Exa's default.
        highlights_query: Guide which snippets are chosen. Implies highlights.

    Returns:
        The pages that came back, plus a ``statuses`` entry for **every** URL
        requested. Read ``.failed`` before treating ``.results`` as the whole
        request — this endpoint returns 200 when some URLs could not be
        crawled.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("exa", ExaClient)).get_contents(
        urls,
        include_text=include_text,
        include_highlights=include_highlights,
        summary_query=summary_query,
        max_characters=max_characters,
        highlights_query=highlights_query,
    )


@step(retry=_READ)
async def exa_answer(query: str, include_text: bool = False) -> ExaAnswer:
    """Ask a question and get an answer with citations.

    Exa searches, reads what it finds, and writes the answer. Use it when the
    workflow wants a fact; use ``exa_search`` when it wants the pages.

    Args:
        query: The question, in plain language.
        include_text: Also return the full text of each cited page.

    Returns:
        The answer and the pages it came from. The citations are the point —
        an answer without them cannot be checked.
    """
    from loom.toolsets.factory import client_for

    return await (await client_for("exa", ExaClient)).answer(query, include_text=include_text)
