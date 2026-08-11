"""Example 12 — Pydantic AI Agent Backend.

Uses Pydantic AI as the LOOM agent backend. The workflow code is
completely framework-agnostic — it only uses ctx.agent("prompt").
Custom tools are registered via the ToolsetRegistry.

Run:
    python3 examples/cookbook/12_pydantic_ai_backend.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from utils import header, log, require_env

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.agents.backends.pydantic_ai import PydanticAIBackend
from workflow_builder.agents.tool_registry import Toolset
from workflow_builder.agents.tools import tool
from workflow_builder.state.memory import MemoryStore

# ---------------------------------------------------------------------------
# Custom tools (defined as LOOM Tools, auto-converted to Pydantic AI tools)
# ---------------------------------------------------------------------------


@tool
async def fetch_hn_stories(query: str, count: int = 3) -> str:
    """Search Hacker News for stories matching a query.

    Args:
        query: Search keywords.
        count: Number of stories to return.
    """
    url = "https://hn.algolia.com/api/v1/search"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params={"query": query, "hitsPerPage": count})
        resp.raise_for_status()
        data = resp.json()

    items = data.get("hits", [])
    lines = []
    for item in items:
        title = item.get("title") or item.get("story_title") or "No title"
        oid = item.get("objectID", "")
        story_url = item.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        points = item.get("points", 0)
        lines.append(f"- {title} ({points} points)\n  {story_url}")
    return "\n".join(lines) or "No results found."


@tool
async def fetch_url_text(url: str) -> str:
    """Fetch a web page and return its text content (first 2000 chars).

    Args:
        url: The URL to fetch.
    """
    import re

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = re.sub(r"<[^>]+>", " ", resp.text)
        return re.sub(r"\s+", " ", text).strip()[:2000]


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@step
async def summarize(raw: str) -> str:
    """Extract key points from raw text."""
    lines = raw.strip().split("\n")
    return "\n".join(line.strip() for line in lines if line.strip())


@workflow(name="pydantic_ai_research")
async def pydantic_ai_research(ctx: Context, query: str) -> str:
    """Research a topic using the Pydantic AI agent."""
    result = await ctx.agent(
        f"Use fetch_hn_stories to find 5 stories about: {query}. "
        "Then use fetch_url_text to read the top result. "
        "Summarize what you found in a concise report."
    )
    summary = await ctx.step(summarize, result.output)
    return summary


async def main() -> None:
    require_env("ANTHROPIC_API_KEY")

    header("Pydantic AI Agent Backend")

    log("setup", "Creating PydanticAI backend")
    backend = PydanticAIBackend(model="anthropic:claude-sonnet-4-6")

    # Register custom tools as a toolset
    toolset = Toolset.from_callables(
        "research",
        [fetch_hn_stories, fetch_url_text],
        summary="HN search + web fetch",
    )

    rt = Runtime(store=MemoryStore(), agent_backend=backend)
    rt.toolsets.register(toolset)
    log("runtime", "Ready with Pydantic AI backend + research toolset")

    query = "workflow automation AI agents"
    log("runtime", f"Query: {query}")

    result = await rt.run(pydantic_ai_research, query)

    header("RESULT")
    log("runtime", f"Status: {result.status.value}")
    if result.output:
        print(f"\n{result.output}\n")


if __name__ == "__main__":
    asyncio.run(main())
