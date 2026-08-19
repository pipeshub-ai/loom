"""Tavily ToolsetManifest — pure metadata, no client import.

Output schemas come from the Pydantic models so the contract cannot drift from
what the tools return.

Every operation declares ``pagination=False``, and that is a statement about
the API rather than an omission: ``max_results`` is capped at 20 and Tavily
offers no cursor. ``tests/test_manifest_imports.py`` checks that claim against
the client three ways.
"""

from __future__ import annotations

from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)
from loom.toolsets.tavily.models import (
    TavilyExtraction,
    TavilySearch,
    TavilySiteMap,
)

TAVILY_MANIFEST = ToolsetManifest(
    id="tavily",
    version="1.0.0",
    summary="Tavily — web search for agents, page extraction, and site maps.",
    description=(
        "Tavily Search API. Search the web and optionally get a written "
        "answer alongside the results (include_answer), with topics for "
        "general, news, and finance, and filters for domain and date. Also "
        "reads the full text of pages you already have URLs for, and maps the "
        "URLs under a site so a workflow can read a docs tree in bulk.\n\n"
        "Nothing here paginates: max_results is capped at 20 and there is no "
        "cursor, so asking for more raises rather than silently returning 20. "
        "Use include_domains or a tighter query instead.\n\n"
        "Quota exhaustion arrives as HTTP 432 (plan limit) or 433 (pay-as-you-"
        "go limit) rather than 429, and neither clears by waiting."
    ),
    base_url="https://api.tavily.com",
    auth={"type": "bearer", "fields": ["TAVILY_API_KEY"]},
    tools_module="loom.toolsets.tavily.tools",
    egress_hosts=["api.tavily.com"],
    rate_limits={
        "results": (
            "20 maximum per search; there is no cursor, so a larger request "
            "is refused rather than silently clamped"
        ),
    },
    groups={
        "search": [
            OperationSpec(
                id="search.query",
                function="tavily_search",
                summary="Search the web, optionally with a written answer.",
                description=(
                    "max_results is 1-20 and does not paginate. Set "
                    "topic='news' when recency matters — it is also the only "
                    "topic that populates published_date. include_answer adds "
                    "a written answer beside the results, which is the reason "
                    "to prefer Tavily when the workflow wants a fact rather "
                    "than a list of pages."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=False,
                output_schema=TavilySearch.model_json_schema(),
            ),
        ],
        "pages": [
            OperationSpec(
                id="pages.extract",
                function="tavily_extract",
                summary="Read the full text of pages you already have URLs for.",
                description=(
                    "1-20 URLs per call. Returns 200 even when some URLs "
                    "failed, so read `.failed` before treating `.pages` as "
                    "the whole request. Tavily's own `timeout` parameter is "
                    "exposed as `read_timeout` because ctx.step claims "
                    "`timeout`."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=False,
                output_schema=TavilyExtraction.model_json_schema(),
            ),
            OperationSpec(
                id="pages.map_site",
                function="tavily_map_site",
                summary="Discover the URLs under a site.",
                description=(
                    "The step before a bulk read: map a docs site, then hand "
                    "the URLs to pages.extract. allow_external defaults to "
                    "False here, unlike the API — 'map this site' means this "
                    "site."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=False,
                output_schema=TavilySiteMap.model_json_schema(),
            ),
        ],
    },
)
