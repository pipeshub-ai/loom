"""Example 28 — Hooks: middleware around every operation.

One workflow, four middleware, and the three things about hooks that are easy
to get wrong.

What this shows:

* **The three shapes.** ``before`` and ``after`` observe and decide; ``around``
  receives the rest of the chain and may call it zero, one, or many times —
  which is what a cache or a retry needs and what a single-pass hook cannot
  express.
* **Which family runs on replay.** Effect hooks sit behind the journal lookup
  and never see a replayed call. Body hooks fire on *every* re-entry, which is
  why they cannot decide — and ``re_entry`` is how they tell "began" from
  "resumed".
* **A refusal survives.** A denied call is journaled as a failure, so a replay
  takes the same branch even against a Runtime with no middleware at all.

Runs offline with no credentials.

Run:
    python3 examples/cookbook/28_hooks.py
"""

from __future__ import annotations

from utils import box, header, log

from loom import Context, Runtime, step, workflow
from loom.runtime.effects import EffectDenied
from loom.runtime.hooks import HookContext
from loom.stores.memory import MemoryStore

#: What the hooks observed, in order. Printed at the end so the interleaving
#: of before/after/around is visible rather than described.
TRACE: list[str] = []

#: Steps that actually executed. The point of several assertions below.
RAN: list[str] = []


# ---------------------------------------------------------------------------
# An ordinary workflow. Nothing here knows a hook exists.
# ---------------------------------------------------------------------------


@step
async def fetch(topic: str) -> list[str]:
    """Pretend to read something expensive."""
    RAN.append("fetch")
    return [f"{topic} #{i}" for i in range(3)]


@step
async def summarise(items: list[str]) -> str:
    RAN.append("summarise")
    return f"{len(items)} items"


@step
async def delete_everything(_summary: str) -> str:
    """Deliberately destructive, so a hook has something to refuse."""
    RAN.append("delete_everything")
    return "deleted"


@workflow(name="pipeline")
async def pipeline(ctx: Context, topic: str) -> dict:
    items = await ctx.step(fetch, topic)
    summary = await ctx.step(summarise, items)
    try:
        # The guard below refuses this. The workflow handles the refusal rather
        # than failing — a denial is a policy outcome, not a bug, and telling
        # the two apart is worth an `except` clause.
        await ctx.step(delete_everything, summary)
        outcome = "deleted"
    except EffectDenied as denied:
        outcome = f"refused ({denied})"
    return {"summary": summary, "outcome": outcome}


# ---------------------------------------------------------------------------
# The middleware. Registered on the Runtime, not on the workflow.
# ---------------------------------------------------------------------------


def install(rt: Runtime) -> None:
    @rt.hooks.on_workflow_start
    async def began(ctx) -> None:
        # Fires on every body entry, replay included — which is exactly why
        # this family cannot deny anything.
        TRACE.append(f"workflow {'resumed' if ctx.re_entry else 'started'}")

    @rt.hooks.on_workflow_end
    async def ended(ctx) -> None:
        # `status` distinguishes suspended from failed. Parking is not failure.
        TRACE.append(f"workflow {ctx.status}")

    @rt.hooks.before_step
    async def audit(ctx: HookContext) -> None:
        TRACE.append(f"  before {ctx.target}")

    @rt.hooks.after_step
    async def record(ctx: HookContext) -> None:
        # Registered after `audit`, and runs *before* it on the way out:
        # `after` unwinds. Nothing sorts that — it is what nesting means.
        TRACE.append(f"  after  {ctx.target} -> {str(ctx.result)[:24]}")

    @rt.hooks.around_step(target="fetch")
    async def cached(ctx: HookContext, next) -> list[str]:
        """A cache: `next()` is simply not called on a hit.

        This is the shape that needs `around`. A hook that can only run before
        and after cannot decline to run the thing in the middle.
        """
        if "fetch" in RAN:
            TRACE.append("  around fetch -> cache hit, step skipped")
            return ["cached #0", "cached #1", "cached #2"]
        return await next()

    @rt.hooks.before_step(target="delete_*")
    async def no_deletes(ctx: HookContext) -> None:
        """Routed by name here, because that is what a *local* step offers.

        Routing by `effect=EffectClass.DESTRUCTIVE` is the better key and works
        for toolset operations, where a manifest declared the class. A plain
        local `@step` stays unclassified on purpose — "inventing one here would
        guess at the very thing the manifest exists to state" — so it defaults
        to WRITE and a name pattern is what is left.

            @rt.hooks.before_tool(effect=EffectClass.DESTRUCTIVE)
            async def confirm(ctx): ctx.ask("deletes data")
        """
        ctx.deny("this deployment does not permit deletes")


async def main() -> None:
    header("Hooks — middleware around every operation")

    async with Runtime(store=MemoryStore()) as rt:
        rt.register(pipeline)
        install(rt)
        log("setup", f"registered: {', '.join(rt.hooks.names())}")

        header("FIRST RUN")
        first = await rt.run(pipeline, "durable execution")
        for line in TRACE:
            log("hook", line)
        log("output", str(first.output))
        log("ran", f"steps that executed: {RAN}")
        log("note", "delete_everything never ran — the guard refused it before")
        log("note", "dispatch, so there was nothing to undo.")

        header("WHICH FAMILY RUNS ON REPLAY")
        TRACE.clear()
        replayed = await rt.replay(first.run_id)
        for line in TRACE:
            log("hook", line)
        log(
            "note",
            "Only the body hooks fired, and re_entry told them so. Effect",
        )
        log("note", "hooks sit behind the journal lookup, so a replay never")
        log("note", "reaches them — which is what makes it safe to give them")
        log("note", "I/O and the power to refuse.")

        header("A REFUSAL SURVIVES THE REPLAY")
        log("first ", str(first.output["outcome"])[:64])
        log("replay", str(replayed.output["outcome"])[:64])
        log(
            "note",
            f"identical: {first.output == replayed.output}. The denial was journaled,",
        )
        log("note", "so the branch does not depend on the middleware installed now.")

        header("RECORDED ON THE RUN, NOT IN THE VERSION")
        record = await rt.get(first.run_id)
        assert record is not None
        log("metadata", str(record.metadata.get("loom.middleware")))

        box(
            "Middleware says what a deployment enforces, not what a workflow is.\n"
            "Folding it into the workflow's content_hash would give one commit\n"
            "as many versions as it has environments — so it is recorded on the\n"
            "run instead, where a denial can still be explained months later.",
            "why it is not versioned",
        )


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    # run_main is asyncio.run plus the two things a program needs: SIGINT and
    # SIGTERM cancel main() so its cleanup runs, and an interrupt becomes an
    # exit code instead of a traceback.
    raise SystemExit(run_main(main()))
