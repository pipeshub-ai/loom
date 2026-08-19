"""Exa ToolsetManifest — pure metadata, no client import.

Output schemas come from the Pydantic models so the contract cannot drift from
what the tools return.

Every operation declares ``pagination=False``, and that is a statement about
the API rather than an omission: Exa has no cursor, offset, or page token on
any endpoint. ``tests/test_manifest_imports.py`` checks that claim against the
client three ways, so the day Exa ships paging this file will fail the build
until it is updated.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.exa.models import ExaAnswer, ExaContents, ExaResult
from loom.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


EXA_MANIFEST = ToolsetManifest(
    id="exa",
    version="1.0.0",
    summary="Exa — neural web search, similar-page lookup, page text, and cited answers.",
    description=(
        "Exa Search API. Embeddings-based web search that takes a description "
        "of what you want rather than keywords, with filters for domain, "
        "publication date, and page category (research paper, news, github, "
        "company, people). Also fetches the text of pages you already have "
        "URLs for, finds pages similar to a given one, and answers a question "
        "with citations.\n\n"
        "Nothing here paginates: numResults is capped at 100 and there is no "
        "cursor, so asking for more raises rather than silently returning 100. "
        "Narrow the query or use include/exclude domains instead."
    ),
    base_url="https://api.exa.ai",
    auth={"type": "api_key", "fields": ["EXA_API_KEY"], "header": "x-api-key"},
    tools_module="loom.toolsets.exa.tools",
    egress_hosts=["api.exa.ai"],
    rate_limits={
        "results": (
            "100 maximum per search; there is no cursor, so a larger request "
            "is refused rather than silently clamped"
        ),
    },
    groups={
        "search": [
            OperationSpec(
                id="search.query",
                function="exa_search",
                summary="Search the web for pages matching a description.",
                description=(
                    "Embeddings-based, so a phrase describing what you want "
                    "beats keywords. num_results is 1-100 and does not "
                    "paginate. include_text returns whole pages and gets large "
                    "fast — prefer include_highlights when the result feeds a "
                    "model."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=False,
                output_schema=_array(ExaResult),
            ),
            OperationSpec(
                id="search.find_similar",
                function="exa_find_similar",
                summary="Find pages semantically similar to a URL you already have.",
                description=(
                    "exclude_source_domain defaults to True here, unlike the "
                    "API: 'more like this' usually means more from elsewhere."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=False,
                output_schema=_array(ExaResult),
            ),
        ],
        "contents": [
            OperationSpec(
                id="contents.get",
                function="exa_get_contents",
                summary="Fetch the text of pages you already have URLs for.",
                description=(
                    "1-100 URLs per call. Returns 200 even when some URLs "
                    "failed to crawl, so read `.failed` before treating "
                    "`.results` as the whole request."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=False,
                output_schema=ExaContents.model_json_schema(),
            ),
        ],
        "answers": [
            OperationSpec(
                id="answers.ask",
                function="exa_answer",
                summary="Ask a question and get an answer with citations.",
                description=(
                    "Exa searches, reads, and writes the answer. Use this when "
                    "the workflow wants a fact; use search.query when it wants "
                    "the pages. The citations are what make the answer "
                    "checkable."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                pagination=False,
                output_schema=ExaAnswer.model_json_schema(),
            ),
        ],
    },
)
