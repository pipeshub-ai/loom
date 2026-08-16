"""Example 25 — Web search: Exa, Tavily, and DuckDuckGo.

Three toolsets that answer the same question with three different contracts,
and a workflow that reads the difference rather than papering over it.

What this shows:

* **A coverage answer you can act on.** DuckDuckGo pages, so its reads return
  ``Results`` and ``.complete`` says whether the source ran out. Exa and Tavily
  have no cursor at all, so they return plain lists and *refuse* a request
  above their cap instead of quietly returning the cap.
* **Choosing a provider is a workflow decision.** The step below prefers a
  supported API when a key is present and falls back to the keyless one, which
  is the shape most real workflows want.
* **Search is a taint source.** Every operation is READ, so under
  ``TaintBroker`` the run below would need a human before it wrote anywhere.

Runs with no credentials at all — DuckDuckGo needs none. Set ``EXA_API_KEY`` or
``TAVILY_API_KEY`` to see the other two take over.

Run:
    pip install 'loomflow[duckduckgo]'
    python3 examples/cookbook/25_web_search.py
"""

from __future__ import annotations

import os

from utils import box, header, log

from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore

# ---------------------------------------------------------------------------
# One step per provider, so the journal records which one actually ran
# ---------------------------------------------------------------------------


@step
async def search_with_exa(query: str, limit: int) -> list[dict]:
    """Search via Exa. Needs EXA_API_KEY.

    Args:
        query: What to search for.
        limit: How many results, 1-100. Exa does not paginate.
    """
    from loom.toolsets.exa.tools import exa_search

    hits = await exa_search(query, num_results=limit)
    return [{"title": h.title, "url": h.url, "why": h.published_date} for h in hits]


@step
async def search_with_tavily(query: str, limit: int) -> list[dict]:
    """Search via Tavily. Needs TAVILY_API_KEY.

    Args:
        query: What to search for.
        limit: How many results, 1-20. Tavily does not paginate.
    """
    from loom.toolsets.tavily.tools import tavily_search

    found = await tavily_search(query, max_results=limit)
    return [{"title": r.title, "url": r.url, "why": r.snippet[:60]} for r in found.results]


@step
async def search_with_duckduckgo(query: str, limit: int) -> dict:
    """Search via DuckDuckGo. No credentials.

    Wrapped in a step of our own rather than called as
    ``ctx.step(ddg_search, ...)`` straight from the workflow body, and that is
    the point of this example: ``Results`` degrades to a plain list when it is
    journaled, so ``.complete`` has to be *read at the call site* and put into
    the output. Called from the body, it would be correct on the first run and
    gone on replay.

    Args:
        query: What to search for.
        limit: How many results to gather across pages.
    """
    from loom.toolsets.duckduckgo.tools import ddg_search

    found = await ddg_search(query, max_results=limit)
    return {
        "rows": [{"title": r.title, "url": r.url, "why": r.snippet[:60]} for r in found],
        # The whole reason this one returns Results: True means the source ran
        # out, so a short answer is not ambiguous.
        "complete": found.complete,
        "summary": found.summary(),
    }


def available_provider() -> str:
    """Whichever supported API has a key, else the keyless fallback."""
    if os.environ.get("EXA_API_KEY"):
        return "exa"
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    return "duckduckgo"


@workflow(name="research")
async def research(ctx: Context, params: dict) -> dict:
    """Search the web with whichever provider this deployment can reach."""
    query = params["query"]
    limit = params.get("limit", 12)
    provider = params.get("provider") or available_provider()

    await ctx.report(f"searching {provider} for {query!r}")

    if provider == "exa":
        rows = await ctx.step(search_with_exa, query, limit)
        return {"provider": provider, "rows": rows, "complete": None}
    if provider == "tavily":
        rows = await ctx.step(search_with_tavily, query, limit)
        return {"provider": provider, "rows": rows, "complete": None}

    answer = await ctx.step(search_with_duckduckgo, query, limit)
    return {"provider": provider, **answer}


async def main() -> None:
    header("Web Search — Exa, Tavily, DuckDuckGo")

    provider = available_provider()
    log("provider", f"using {provider}")
    if provider == "duckduckgo":
        log("note", "No EXA_API_KEY or TAVILY_API_KEY set, so the keyless")
        log("note", "fallback is running. It parses result pages rather than")
        log("note", "calling an API, so treat it as best-effort.")

    async with Runtime(store=MemoryStore()) as rt:
        result = await rt.run(
            research, {"query": "durable execution engines", "limit": 12}
        )

        if result.status.value == "failed":
            header("THAT FAILED, AND THE ERROR SAYS WHY")
            log("error", str(result.error.message if result.error else "")[:300])
            log("note", "A blocked search raises rather than returning [] —")
            log("note", "an empty list would read as 'nothing matched'.")
            return

        output = result.output or {}
        header("RESULTS")
        for row in output.get("rows", [])[:8]:
            log("hit", f"{row['title'][:52]:<52} {row['url'][:40]}")

        header("DID WE SEE EVERYTHING?")
        if output.get("complete") is None:
            log("coverage", f"{provider} has no cursor — one call is the answer.")
            log("note", "Asking for more than its cap raises rather than")
            log("note", "silently returning the cap, which would report a")
            log("note", "fraction of the data as the total.")
        else:
            log("coverage", output.get("summary", ""))
            log(
                "note",
                "complete=True means the source ran out; False means more"
                if output["complete"]
                else "complete=False — more sits behind the cursor",
            )

        header("THE JOURNAL RECORDS WHICH PROVIDER RAN")
        for entry in await rt.store.load_journal(result.run_id):
            log("journal", f"{entry.path:<5} {entry.kind.value:<6} {entry.name}")

        box(
            "Every operation across the three is READ and idempotent.\n"
            "Under Runtime(broker=TaintBroker(...)) this run is now tainted:\n"
            "it holds text nobody reviewed, so the next write wants a human.",
            "why the effect class matters",
        )


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
