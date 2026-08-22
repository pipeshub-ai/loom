"""DuckDuckGo ToolsetManifest — pure metadata, no client import.

Output schemas come from the Pydantic models so the contract cannot drift from
what the tools return.

Unlike Exa and Tavily, every operation here declares ``pagination=True``: the
underlying package exposes a page number and the client follows it, so the
tools return :class:`Results` and ``complete`` is a real answer.
``tests/test_manifest_imports.py`` checks that claim against the client three
ways.

The description says plainly that this is not an official API. That is not a
disclaimer for its own sake — the coding agent reads these descriptions to
choose between integrations, and "best-effort, no key" versus "supported, needs
a key" is exactly the trade-off it should be making on the workflow author's
behalf.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.duckduckgo.models import (
    DuckDuckGoImage,
    DuckDuckGoNews,
    DuckDuckGoResult,
)
from loom.toolsets.manifest import (
    AuthField,
    AuthSpec,
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


DUCKDUCKGO_MANIFEST = ToolsetManifest(
    id="duckduckgo",
    version="1.0.0",
    summary="DuckDuckGo — web, news, and image search with no API key. Best-effort.",
    description=(
        "Web, news, and image search requiring no credentials, via the "
        "third-party `ddgs` package (pip install 'loomsdk[duckduckgo]').\n\n"
        "NOT AN OFFICIAL API. DuckDuckGo publishes no web-search API — their "
        "only documented endpoint returns instant answers and no web results "
        "— so `ddgs` parses search result pages instead. It is rate-limited "
        "aggressively and can break when those pages change. Prefer the exa "
        "or tavily toolsets where reliability matters; reach for this one "
        "when no API key is available or the search is incidental.\n\n"
        "Being turned away raises a retryable error rather than returning an "
        "empty list, so a blocked search does not read as 'nothing matched'. "
        "All three operations paginate and return Results: `.complete` is True "
        "only when the source ran out, so a short answer is no longer "
        "ambiguous between 'that is everything' and 'it stopped early'."
    ),
    base_url="https://duckduckgo.com",
    auth=AuthSpec(
        client="loom.toolsets.duckduckgo.client:DuckDuckGoClient",
        # No API and no key — `ddgs` parses result pages.
        kind="none",
        # Not a credential, and declared anyway: the client reads it, so it has
        # to reach the constructor through the same path everything else does.
        # A value that is only obtainable from the ambient environment is the
        # thing this whole change removes, whether or not it is secret.
        fields=(
            AuthField(name="DDGS_PROXY", label="Outbound proxy", arg="proxy",
                      secret=False, required=False,
                      example="socks5://127.0.0.1:1080"),
        ),
    ),
    tools_module="loom.toolsets.duckduckgo.tools",
    egress_hosts=["duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"],
    rate_limits={
        "model": (
            "unofficial; scraped result pages with no published limit. Being "
            "blocked surfaces as a retryable DuckDuckGoRateLimited rather "
            "than as an empty result set"
        ),
    },
    groups={
        "search": [
            OperationSpec(
                id="search.web",
                function="ddg_search",
                summary="Search the web. No API key; best-effort.",
                description=(
                    "Follows pages to reach max_results and deduplicates by "
                    "URL. `.complete` is True only when the source ran out. "
                    "timelimit takes d/w/m/y; backend pins one engine "
                    "instead of trying several."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(DuckDuckGoResult),
            ),
            OperationSpec(
                id="search.news",
                function="ddg_news",
                summary="Search news. No API key; best-effort.",
                description=(
                    "Carries a publication date and source, which the web "
                    "search does not. timelimit=d is the usual filter here."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(DuckDuckGoNews),
            ),
            OperationSpec(
                id="search.images",
                function="ddg_images",
                summary="Search images. No API key; best-effort.",
                description=(
                    "`image` is the direct URL of the image; `url` is the page "
                    "it appears on. Confusing the two is the usual mistake."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(DuckDuckGoImage),
            ),
        ],
    },
)
