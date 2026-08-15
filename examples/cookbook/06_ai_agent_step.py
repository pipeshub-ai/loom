"""Example 6 — LLM agent as a workflow step.

A research workflow that uses Claude (via the LOOM agent layer) to
summarise fetched content.  The agent call is durably journaled — if
the process crashes mid-agent, the workflow replays without re-paying
for completed LLM turns.

Demonstrates: Agent, AnthropicProvider, ctx.agent(), structured output.

Requires:
    ANTHROPIC_API_KEY environment variable

Run:
    ANTHROPIC_API_KEY=sk-... python3 examples/cookbook/06_ai_agent_step.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import load_dotenv, require_env

# The agent below is built at import time, so credentials must be present by
# then — load .env before that rather than inside main().
load_dotenv()

from loom import Context, Retry, Runtime, step, workflow  # noqa: E402
from loom.agents.agent import Agent, PersistenceClass  # noqa: E402
from loom.agents.providers.anthropic_provider import AnthropicProvider  # noqa: E402
from loom.stores.memory import MemoryStore  # noqa: E402

# ---------------------------------------------------------------------------
# Define the summariser agent (created once, reused across runs)
# ---------------------------------------------------------------------------

summariser = Agent(
    name="summariser",
    instructions=(
        "You are a concise research assistant. "
        "Summarise the provided text in 2-3 sentences. "
        "Be factual and avoid padding."
    ),
    model=AnthropicProvider(
        model_name="claude-haiku-4-5-20251001",  # fast + cheap for summarisation
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
    ),
    persistence=PersistenceClass.EPHEMERAL,
)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=2))
async def fetch_article(query: str) -> str:
    """Fetch top Hacker News stories matching query and return their titles + snippets."""
    url = f"https://hn.algolia.com/api/v1/search?query={query}&hitsPerPage=5"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    items = data.get("hits", [])
    lines = []
    for item in items:
        title = item.get("title") or item.get("story_title", "")
        snippet = (item.get("story_text") or item.get("comment_text") or "")[:200]
        snippet = re.sub(r"<[^>]+>", "", snippet).strip()
        if title:
            lines.append(f"- {title}: {snippet}" if snippet else f"- {title}")
    return "\n".join(lines) or "No results found."


@step
async def format_result(url: str, summary: str) -> dict:
    """Package the summary with metadata."""
    return {"url": url, "summary": summary, "chars": len(summary)}


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="ai_research")
async def ai_research(ctx: Context, query: str) -> dict:
    """Fetch HN stories for a query, summarise them with Claude, return structured result."""
    text = await ctx.step(fetch_article, query)

    # ctx.agent() journals the entire agent result as a single durable entry
    agent_result = await ctx.agent(
        summariser,
        f"Summarise these Hacker News stories about '{query}':\n\n{text}",
    )
    summary = agent_result.output if isinstance(agent_result.output, str) else str(agent_result)

    return await ctx.step(format_result, query, summary)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    require_env("ANTHROPIC_API_KEY")

    rt = Runtime(store=MemoryStore())
    query = "AI workflow automation"

    print(f"Researching HN stories about: '{query}'")
    result = await rt.run(ai_research, query)

    print(f"\nStatus : {result.status.value}")
    if result.output:
        print(f"Query  : {result.output['url']}")
        print(f"Summary: {result.output['summary']}")
        print(f"Length : {result.output['chars']} chars")
    print(f"Run ID : {result.run_id}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
