"""Example 11 — Agno Agent Backend.

Uses the Agno framework as the LOOM agent backend. The workflow code
is completely framework-agnostic — it only uses ctx.agent("prompt").
Tools are registered via the ToolsetRegistry.

Run:
    python3 examples/cookbook/11_agno_backend.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log, require_env

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.agents.backends.agno import AgnoBackend
from workflow_builder.agents.tool_registry import Toolset
from workflow_builder.state.memory import MemoryStore


def _build_backend_and_tools():
    """Build Agno backend with DuckDuckGo search."""
    from agno.models.anthropic import Claude
    from agno.tools.duckduckgo import DuckDuckGoTools

    log("setup", "Creating Agno backend with Claude + DuckDuckGo")
    model = Claude(id="claude-sonnet-4-6")
    backend = AgnoBackend(model=model)

    # Register DuckDuckGo as a toolset via Agno's native tools
    ddg = DuckDuckGoTools()
    toolset = Toolset.from_callables(
        "web",
        [ddg],
        summary="Web search via DuckDuckGo (Agno)",
    )
    return backend, toolset


@step
async def format_response(raw: str) -> str:
    """Clean up agent response for display."""
    lines = raw.strip().split("\n")
    cleaned = []
    for line in lines:
        line = line.strip()
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


@workflow(name="agno_research")
async def agno_research(ctx: Context, query: str) -> str:
    """Search for information using the Agno-powered agent."""
    result = await ctx.agent(
        f"Search for the latest news about: {query}. "
        "Return a brief summary of the top 3 results "
        "with their titles and key points."
    )
    formatted = await ctx.step(format_response, result.output)
    return formatted


async def main() -> None:
    require_env("ANTHROPIC_API_KEY")

    header("Agno Agent Backend")
    backend, toolset = _build_backend_and_tools()

    rt = Runtime(store=MemoryStore(), agent_backend=backend)
    rt.toolsets.register(toolset)
    log("runtime", "Ready with Agno backend + web toolset")

    query = "AI agent frameworks 2026"
    log("runtime", f"Query: {query}")

    result = await rt.run(agno_research, query)

    header("RESULT")
    log("runtime", f"Status: {result.status.value}")
    if result.output:
        print(f"\n{result.output}\n")


if __name__ == "__main__":
    asyncio.run(main())
