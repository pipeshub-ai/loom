"""Big tool results: showing a page instead of silently sending a prefix.

An agent calls a search tool. The tool returns four megabytes. Nothing stops
that reaching the model — and it reaches it again on every following turn,
until the provider truncates it. The model then answers, confidently, about
data it never saw. That is the failure worth naming: not an error, a *wrong
answer that looks right*.

``ResultBounds`` caps what one tool result contributes to the conversation and
replaces the rest with something the model can act on: what was omitted, the
format, the shape, and the two calls that read the whole thing back.

Three properties are worth watching for in the output:

**The cap is on the replacement.** The notice's own byte cost is reserved out
of the budget, so bounding can never make a result larger. Truncate-then-append
— the obvious implementation — violates this by exactly the length of the notice.

**A paged read keeps its coverage.** ``Results`` serializes its rows first and
its ``complete``/``total`` last, which is exactly where a head-and-tail cut
would lose them. The coverage is hoisted into the notice before any truncation,
so one page is never summarized into looking like the whole set.

**The journal keeps the value whole.** Bounding is a property of the
conversation, not of the run: a replay has to reconstruct what happened.

    python 23_bounded_tool_results.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import header, log

from loom.agents.agent import Agent
from loom.agents.bounds import BlobSpillStore, ResultBounds
from loom.agents.executor import AgentContext
from loom.agents.messages import ToolCall
from loom.agents.tools import tool
from loom.blobs.blob import BlobService, LocalBlobBackend
from loom.testing.mock import MockModelProvider, mock_response
from loom.toolsets.pagination import Results

# ---------------------------------------------------------------------------
# A tool that returns far more than anyone wants in a prompt
# ---------------------------------------------------------------------------

ISSUES = [
    {"key": f"ENG-{i:04d}", "summary": "Investigate flaky checkout test " * 3}
    for i in range(400)
]


@tool
async def search_issues(query: str) -> str:
    """Search the issue tracker.

    Args:
        query: What to search for.
    """
    return json.dumps({"issues": ISSUES, "total": 9_000})


@tool
async def search_paged(query: str) -> Results[dict]:
    """Search the issue tracker, one page at a time.

    Args:
        query: What to search for.
    """
    return Results(ISSUES, complete=False, total=9_000)


async def main() -> None:
    blobs = BlobService(LocalBlobBackend(Path("./.loom-blobs")))
    store = BlobSpillStore(blobs)

    header("WITHOUT BOUNDS — THE WHOLE THING GOES TO THE MODEL")
    model = MockModelProvider(
        responses=[
            mock_response(
                tool_calls=[ToolCall(name="search_issues", arguments={"query": "x"})]
            ),
            mock_response("summarised"),
        ]
    )
    plain = Agent(name="unbounded", model=model, tools=[search_issues])
    await plain("find flaky tests", context=AgentContext(run_id="demo-1"))
    sent = _tool_text(model)
    log("sent to model", f"{len(sent.encode()):,} bytes")
    log("note", "and again on every following turn of this agent")

    header("WITH BOUNDS — A PAGE, AND HOW TO READ THE REST")
    model = MockModelProvider(
        responses=[
            mock_response(
                tool_calls=[ToolCall(name="search_issues", arguments={"query": "x"})]
            ),
            mock_response("summarised"),
        ]
    )
    bounded = Agent(
        name="bounded",
        model=model,
        tools=[search_issues],
        bounds=ResultBounds(max_bytes=1_200),
    )
    await bounded("find flaky tests", context=AgentContext(run_id="demo-2", spill=store))
    sent = _tool_text(model)
    log("sent to model", f"{len(sent.encode()):,} bytes (cap 1,200)")
    log("notice", _notice(sent))

    header("THE MODEL CAN READ THE REST")
    offered = {t.name for t in (model.last_request().tools or [])}
    log("tools offered", ", ".join(sorted(offered)))
    log("note", "mounted before the overflow — a locator with no reader is just truncation")

    found = re.search(r"blob:[0-9a-f]{64}", sent)
    assert found is not None
    locator = found.group(0)
    page = await store.read(locator, offset=0, limit=80)
    log("read_spill", page)
    hits = await store.grep(locator, "ENG-0007", max_matches=1)
    log("grep_spill", hits[0][:80] if hits else "(none)")

    header("A PAGED READ KEEPS ITS COVERAGE")
    model = MockModelProvider(
        responses=[
            mock_response(
                tool_calls=[ToolCall(name="search_paged", arguments={"query": "x"})]
            ),
            mock_response("summarised"),
        ]
    )
    paged = Agent(
        name="paged",
        model=model,
        tools=[search_paged],
        bounds=ResultBounds(max_bytes=1_200),
    )
    await paged("find flaky tests", context=AgentContext(run_id="demo-3", spill=store))
    sent = _tool_text(model)
    log("coverage", _notice(sent).lstrip("(").split(" Omitted")[0])
    log("note", "400 rows of 9,000 — never rendered as a total")


def _notice(sent: str) -> str:
    """The omission notice, which is the last paragraph of a bounded result."""
    return sent.rsplit("\n\n", 1)[-1]


def _tool_text(model: MockModelProvider) -> str:
    """The tool result as the model received it."""
    for message in reversed(model.requests[-1].messages):
        if getattr(message, "role", "") == "tool":
            return str(message.content)
    return ""


if __name__ == "__main__":
    asyncio.run(main())
