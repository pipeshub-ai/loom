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


@step
async def lookup_release_notes(product: str) -> str:
    """Look up release notes for a product from a local table.

    Args:
        product: Product name to look up.
    """
    notes = {
        "loom": "0.11.0 — unified toolset registry, saga compensation, blob offload.",
        "python": "3.13 — free-threaded build, improved REPL, JIT groundwork.",
    }
    return notes.get(product.lower(), f"No release notes on file for {product!r}.")


def _build_backend_and_tools():
    """Build the Agno backend and whichever web toolset is available.

    DuckDuckGo needs the ``ddgs`` package, which Agno does not pull in. When it
    is missing the example still runs — the point being demonstrated is the
    backend and toolset registration, not the search provider.
    """
    from agno.models.anthropic import Claude

    log("setup", "Creating Agno backend with Claude")
    backend = AgnoBackend(model=Claude(id="claude-sonnet-4-6"))

    try:
        from agno.tools.duckduckgo import DuckDuckGoTools
    except ImportError:
        log("setup", "ddgs not installed — using a local toolset instead")
        log("setup", "For live web search: pip install ddgs")
        return backend, Toolset.from_steps("notes", [lookup_release_notes])

    log("setup", "Registering DuckDuckGo web search")
    toolset = Toolset.from_callables(
        "web",
        [DuckDuckGoTools()],
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
    """Research a topic using the Agno-powered agent.

    Note there is no Agno import anywhere in this workflow — swapping the
    backend on the Runtime is the only change needed to run it on a different
    framework.
    """
    result = await ctx.agent(
        f"Research this topic and summarise the top findings: {query}. "
        "Use the tools available to you."
    )
    formatted = await ctx.step(format_response, result.output)
    return formatted


async def main() -> None:
    require_env("ANTHROPIC_API_KEY")

    header("Agno Agent Backend")
    backend, toolset = _build_backend_and_tools()

    rt = Runtime(store=MemoryStore(), agent_backend=backend)
    rt.toolsets.register(toolset)
    log("runtime", f"Ready with Agno backend + '{toolset.manifest.id}' toolset")

    query = "AI agent frameworks 2026" if toolset.manifest.id == "web" else "loom"
    log("runtime", f"Query: {query}")

    result = await rt.run(agno_research, query)

    header("RESULT")
    log("runtime", f"Status: {result.status.value}")
    if result.output:
        print(f"\n{result.output}\n")


if __name__ == "__main__":
    asyncio.run(main())
