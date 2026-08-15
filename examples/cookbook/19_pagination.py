"""Paged reads: getting all of it, or saying you did not.

Every hosted API caps a page below what you ask for, and none of them treat
exceeding it as an error. Ask for 500 rows, get 100, with a 200 OK. A workflow
that reports ``f"{len(rows)} found"`` on that is wrong in the way that survives
review — the number is real and only the framing lies.

Two patterns, and the choice is about the *set*, not the API:

**Bounded** — a project's issues, a page's comments. One call, and say what it
covers.

**Unbounded** — a mailbox, an audit log. One page per step, cursor kept in
``ctx.state``. Raising ``max_results`` is the wrong answer here: one call for
50,000 rows is a single journal entry, so a crash refetches all of them and the
whole page sits in memory at once.

    python 19_pagination.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from utils import header, log

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.state.memory import MemoryStore
from workflow_builder.toolsets.pagination import Page, Results, collect

# ---------------------------------------------------------------------------
# A stand-in for a real API: 250 rows, served 40 at a time
# ---------------------------------------------------------------------------

ROWS = [f"TICKET-{i:03d}" for i in range(250)]
PAGE_CAP = 40


async def _api(cursor: str | None, size: int) -> Page:
    """One request. Your API's dialect lives here and nowhere else."""
    start = int(cursor or 0)
    batch = ROWS[start : start + min(size, PAGE_CAP)]
    nxt = start + len(batch)
    return Page(batch, cursor=str(nxt) if nxt < len(ROWS) else None, total=len(ROWS))


@step
async def search_tickets(max_results: int = 20, cursor: str | None = None) -> Results[str]:
    """Search tickets, following pages until the limit or the end.

    Returning ``Results`` is the whole declaration: it is what tells the coding
    agent this read is paged, and what lets a caller ask whether it saw
    everything.

    Args:
        max_results: Most rows to return.
        cursor: Where to resume from. Omit to start at the beginning.
    """

    async def fetch(at: str | None, size: int) -> Page:
        return await _api(at or cursor, size)

    return await collect(fetch, limit=max_results, page_size=PAGE_CAP)


# ---------------------------------------------------------------------------
# Bounded: one call, and report the coverage
# ---------------------------------------------------------------------------


@workflow(name="bounded_report")
async def bounded_report(ctx: Context, limit: int = 100) -> str:
    """Fetch up to *limit* rows and say honestly what that covers."""
    tickets = await ctx.step(search_tickets, max_results=limit)

    # .complete survives the step boundary — the value carries it through the
    # journal, so this reads correctly here and identically on a replay.
    coverage = tickets.summary() if not tickets.complete else f"all {len(tickets)}"
    return f"Reporting on {coverage}: {tickets[0]} … {tickets[-1]}"


# ---------------------------------------------------------------------------
# Unbounded: one page per step, resumable across runs
# ---------------------------------------------------------------------------


@workflow(name="drain_everything")
async def drain_everything(ctx: Context, _: Any = None) -> str:
    """Walk the whole set a page at a time, remembering where it stopped.

    Each page is its own journal entry, so a crash resumes at the page it died
    on rather than refetching everything. The cursor lives in ``ctx.state``,
    which outlives the run — so a second run continues where the first stopped
    instead of starting over.
    """
    cursor = await ctx.state.get("cursor")
    seen = await ctx.state.get("seen", default=0)

    while True:
        page = await ctx.step(search_tickets, max_results=PAGE_CAP, cursor=cursor)
        seen += len(page)
        await ctx.report(f"page of {len(page)}, {seen} so far")

        if page.complete:
            await ctx.state.delete("cursor")
            break
        cursor = page.cursor
        await ctx.state.set("cursor", cursor)
        await ctx.state.set("seen", seen)

    await ctx.state.set("seen", seen)
    return f"drained {seen} rows"


async def main() -> None:
    rt = Runtime(store=MemoryStore())
    rt.register_all([bounded_report, drain_everything])

    header("BOUNDED — ONE CALL, HONEST COVERAGE")
    capped = await rt.run(bounded_report, 100)
    log("capped", capped.output)
    log("note", "250 exist; the report says so rather than implying 100 is all")

    everything = await rt.run(bounded_report, 250)
    log("full", everything.output)

    header("UNBOUNDED — ONE PAGE PER STEP")
    drained = await rt.run(drain_everything)
    log("result", str(drained.output))

    entries = await rt.history(drained.run_id)
    log("journal", f"{len(entries)} entries — one per page, not one for all 250")
    for report in rt.stream.since(drained.run_id)[:3]:
        log("progress", report.message)

    header("WHY NOT JUST RAISE THE LIMIT")
    log("note", "One call for 50,000 rows is one journal entry.")
    log("note", "A crash refetches every row; the whole page is held at once.")
    log("note", "A page per step resumes where it stopped, and streams progress.")


if __name__ == "__main__":
    asyncio.run(main())
