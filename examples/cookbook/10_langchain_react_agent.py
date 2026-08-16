"""Example 10 — LangChain ReAct Agent via Runtime backend.

The Workflow Coding Agent generates framework-agnostic code that uses
ctx.agent("prompt"). Tools are registered on the Runtime via the
ToolsetRegistry. The generated workflow has zero knowledge of LangChain.

Architecture:
    rt.toolsets.register(Toolset.from_callables("web", [search, fetch]))
    rt = Runtime(agent_backend=LangChainBackend(llm=...))
        |
    ctx.agent("Use web.search to find articles")  <-- generated code
        |
    ToolsetRegistry resolves tools -> LangChainBackend converts -> ReAct agent

Run:
    python3 examples/cookbook/10_langchain_react_agent.py
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

# Allow running as a script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import box, header, load_workflow, log, print_coding_result, require_env

from loom.agents.backends.langchain import LangChainBackend
from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.providers.anthropic_provider import AnthropicProvider
from loom.agents.tool_registry import Toolset
from loom.runtime.engine import Runtime
from loom.stores.memory import MemoryStore

SPEC = """\
Create a workflow called "ai_article_digest" that:
1. Accepts a query string
2. Calls ctx.agent() asking it to use web.duckduckgo_results_json to
   search for 3-5 articles, then use web.fetch_url to read each page.
   Ask for structured output: TITLE, URL, SUMMARY for each.
3. Parses the output into a list of article dicts
4. Prints each article
5. Returns the list

Include a main() with Runtime(store=MemoryStore()), run the workflow
with query "latest AI breakthroughs August 2026", print status and output.
"""


def _build_web_toolset() -> tuple[object, Toolset]:
    """Build LangChain LLM + web research toolset."""
    from langchain_anthropic import ChatAnthropic
    from langchain_community.tools import DuckDuckGoSearchResults
    from langchain_community.utilities import DuckDuckGoSearchAPIWrapper
    from langchain_core.tools import tool as lc_tool

    log("setup", "Creating LLM + web tools")
    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0, max_tokens=4096)
    ddg = DuckDuckGoSearchAPIWrapper(max_results=5)
    search = DuckDuckGoSearchResults(api_wrapper=ddg)

    @lc_tool
    def fetch_url(url: str) -> str:
        """Fetch a web page and return text (first 3000 chars)."""
        import httpx

        resp = httpx.get(url, timeout=15, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        text = re.sub(r"<[^>]+>", " ", resp.text)
        return re.sub(r"\s+", " ", text).strip()[:3000]

    toolset = Toolset.from_callables(
        "web",
        [search, fetch_url],
        summary="Web research: search + fetch pages",
    )
    log("setup", f"Toolset 'web' ready ({len(toolset.manifest.all_operations())} tools)")
    return llm, toolset


async def main() -> None:
    require_env("ANTHROPIC_API_KEY")

    # ── Setup: build toolset + backend ──────────────────────────────
    llm, web_toolset = _build_web_toolset()
    backend = LangChainBackend(llm=llm)

    # ── Phase A: generate workflow ──────────────────────────────────
    header("Phase A: Generate Workflow")
    box(SPEC.strip(), "SPEC")

    # Coding agent auto-generates tool docs from the registry
    from loom.agents.tool_registry import ToolsetRegistry

    registry = ToolsetRegistry()
    registry.register(web_toolset)
    log("coding-agent", f"Tool docs (auto-generated):\n{registry.describe()}")

    t0 = time.perf_counter()
    agent = WorkflowCodingAgent(
        model=AnthropicProvider(model_name="claude-sonnet-4-6"),
        tool_registry=registry,
    )
    result = await agent.generate(SPEC)
    gen_time = time.perf_counter() - t0

    print_coding_result(result)
    print()
    box(result.code, "GENERATED CODE")
    if not result.is_clean:
        return

    # ── Phase B: execute with LangChain backend ─────────────────────
    header("Phase B: Execute")
    async with Runtime(store=MemoryStore(), agent_backend=backend) as rt:
        rt.toolsets.register(web_toolset)
        log("runtime", "Runtime ready with LangChain backend + web toolset")

        wf = load_workflow(result.code)
        if wf is None:
            log("runtime", "ERROR: no @workflow found")
            return
        log("runtime", f"Loaded workflow: {wf.name}")

        t1 = time.perf_counter()
        run = await rt.run(wf, "latest AI breakthroughs August 2026")

        header("RESULT")
        log("runtime", f"Status : {run.status.value}")
        log("runtime", f"Time   : {time.perf_counter() - t1:.0f}s (gen {gen_time:.0f}s)")
        if run.output:
            print(run.output if isinstance(run.output, str) else str(run.output))


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
