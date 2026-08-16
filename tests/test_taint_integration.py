"""Taint through a real Runtime, rather than against hand-built EffectCalls.

`test_taint.py` asserts the rule. This asserts that a workflow actually meets
it, which is a different claim and the one that was wrong: the rule was correct
in isolation and unreachable in practice. Three seams had to line up and none of
them did.

- A step's **effect class** never left its manifest, so every read reached the
  broker looking like a write and nothing could taint.
- An **approval** is written to the journal by ``wait_for_event`` and never
  dispatched, so the escape hatch could not fire.
- Taint lived in **memory**, and the engine serves a journaled call without
  dispatching it — so after any park, retry, or restart the run came back with
  no history and the rule permitted everything.

Each failure was silent, and two of the three failed *open*. That is why these
tests drive a Runtime end to end instead of a broker directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom import Context, Runtime, step, workflow
from loom.agents.tool_registry import Toolset
from loom.runtime.effects import DirectBroker
from loom.runtime.taint import TaintBroker, TaintPolicy
from loom.stores.memory import MemoryStore
from loom.stores.sqlite import SQLiteStore
from loom.toolsets.manifest import EffectClass


@step
async def search_the_web(query: str) -> str:
    """A read of something nobody reviewed."""
    return f"results for {query}"


@step
async def delete_ticket(ticket: str) -> str:
    """The call the rule exists to stop."""
    return f"deleted {ticket}"


@step
async def post_summary(text: str) -> str:
    return f"posted {text}"


def a_runtime(
    store: Any = None,
    policy: TaintPolicy | None = None,
    clock: Any = None,
) -> Runtime:
    """A Runtime whose toolset declares what each step is.

    The declaration is the point: without a manifest saying ``search_the_web``
    reads, the call arrives classified as a write like everything else.
    """
    rt = Runtime(
        store=store or MemoryStore(),
        broker=TaintBroker(DirectBroker(), policy or TaintPolicy()),
        **({"clock": clock} if clock is not None else {}),
    )
    rt.toolsets.register(
        Toolset.from_steps(
            "web",
            [search_the_web, delete_ticket, post_summary],
            effects={
                "search_the_web": EffectClass.READ,
                "delete_ticket": EffectClass.DESTRUCTIVE,
                "post_summary": EffectClass.WRITE,
            },
        )
    )
    return rt


class TestTheEffectClassReachesTheBroker:
    """Without this every other assertion here is vacuous."""

    def test_a_declared_read_is_known_to_be_a_read(self) -> None:
        assert a_runtime().toolsets.effect_of("search_the_web") is EffectClass.READ

    def test_a_declared_destructive_is_known_to_be_destructive(self) -> None:
        assert (
            a_runtime().toolsets.effect_of("delete_ticket")
            is EffectClass.DESTRUCTIVE
        )

    def test_an_undeclared_step_stays_unclassified(self) -> None:
        """A local ``@step`` is not a manifest operation, and guessing one here
        would invent the declaration the manifest exists to make."""
        assert a_runtime().toolsets.effect_of("some_local_helper") is None


class TestTheRuleHoldsForARealRun:
    async def test_a_destructive_call_after_a_read_fails_the_run(self) -> None:
        @workflow(name="read_then_delete")
        async def read_then_delete(ctx: Context, _: Any = None) -> str:
            await ctx.step(search_the_web, query="how to close tickets")
            return await ctx.step(delete_ticket, ticket="T-1")

        rt = a_runtime()
        rt.register(read_then_delete)

        result = await rt.run(read_then_delete)

        assert result.status.value == "failed"
        assert "has read external data" in (result.error.message if result.error else "")

    async def test_the_same_call_without_the_read_succeeds(self) -> None:
        """The rule has to be about the read, not about the delete."""

        @workflow(name="just_delete")
        async def just_delete(ctx: Context, _: Any = None) -> str:
            return await ctx.step(delete_ticket, ticket="T-1")

        rt = a_runtime()
        rt.register(just_delete)

        result = await rt.run(just_delete)

        assert result.status.value == "completed"
        assert result.output == "deleted T-1"

    async def test_reads_stay_allowed_after_a_read(self) -> None:
        @workflow(name="read_twice")
        async def read_twice(ctx: Context, _: Any = None) -> str:
            await ctx.step(search_the_web, query="one")
            return await ctx.step(search_the_web, query="two")

        rt = a_runtime()
        rt.register(read_twice)

        assert (await rt.run(read_twice)).status.value == "completed"


class TestApprovalClearsItAcrossAPark:
    """The escape hatch, over the park it necessarily involves.

    Asking a human means suspending, and suspending means re-entering — so this
    is also the test that taint survives re-entry. Before the journal-derived
    state it passed for the wrong reason: the run came back with *no* taint at
    all, so the write proceeded whether or not anyone had approved.
    """

    @pytest.fixture
    def approval_flow(self):
        @workflow(name="read_ask_delete")
        async def read_ask_delete(ctx: Context, _: Any = None) -> str:
            await ctx.step(search_the_web, query="what to delete")
            await ctx.wait_for_approval("cleanup")
            return await ctx.step(delete_ticket, ticket="T-9")

        return read_ask_delete

    async def test_it_parks_and_then_deletes_once_approved(
        self, approval_flow
    ) -> None:
        rt = a_runtime()
        rt.register(approval_flow)

        parked = await rt.run(approval_flow)
        assert parked.status.value == "suspended"

        await rt.approve(parked.run_id, "cleanup")
        resumed = await rt.resume(parked.run_id)

        assert resumed.status.value == "completed", (
            f"approval did not clear the taint: {resumed.error}"
        )
        assert resumed.output == "deleted T-9"

    async def test_without_the_approval_it_stays_refused(self) -> None:
        """The mutation guard for the test above: if taint were simply lost
        across the park, this would pass too — and it must not."""

        @workflow(name="read_park_delete")
        async def read_park_delete(ctx: Context, _: Any = None) -> str:
            await ctx.step(search_the_web, query="what to delete")
            await ctx.wait_for_event("unrelated")
            return await ctx.step(delete_ticket, ticket="T-9")

        rt = a_runtime()
        rt.register(read_park_delete)

        parked = await rt.run(read_park_delete)
        await rt.send_event(parked.run_id, "unrelated", {})
        resumed = await rt.resume(parked.run_id)

        assert resumed.status.value == "failed", (
            "an unrelated event cleared the taint, or the taint did not survive "
            "the park"
        )

    async def test_a_read_after_the_approval_taints_again(self) -> None:
        """An approval covers what was read before it, not everything after."""

        @workflow(name="approve_then_read")
        async def approve_then_read(ctx: Context, _: Any = None) -> str:
            await ctx.wait_for_approval("cleanup")
            await ctx.step(search_the_web, query="late lookup")
            return await ctx.step(delete_ticket, ticket="T-9")

        rt = a_runtime()
        rt.register(approve_then_read)

        parked = await rt.run(approve_then_read)
        await rt.approve(parked.run_id, "cleanup")
        resumed = await rt.resume(parked.run_id)

        assert resumed.status.value == "failed"


class TestItSurvivesTheProcess:
    async def test_taint_is_rebuilt_by_a_second_runtime(self, tmp_path) -> None:
        """The crash case, and the reason memory was never enough.

        A different Runtime over the same store is what a restart, a redeploy,
        or a second worker looks like. Its broker has never dispatched anything
        for this run, so everything it knows it must read from the journal.
        """
        db = str(tmp_path / "runs.db")

        @workflow(name="read_park_delete_durable")
        async def flow(ctx: Context, _: Any = None) -> str:
            await ctx.step(search_the_web, query="what to delete")
            await ctx.wait_for_event("go")
            return await ctx.step(delete_ticket, ticket="T-4")

        first = a_runtime(SQLiteStore(db))
        first.register(flow)
        parked = await first.run(flow)
        assert parked.status.value == "suspended"

        second = a_runtime(SQLiteStore(db))
        second.register(flow)
        await second.send_event(parked.run_id, "go", {})
        resumed = await second.resume(parked.run_id)

        assert resumed.status.value == "failed", (
            "a fresh process forgot what the run had read"
        )

    async def test_a_timer_park_resumed_elsewhere_still_remembers(
        self, tmp_path
    ) -> None:
        """The case only the re-entry hook covers: a new process, and no event.

        Every other test here parks on an event, and *delivering* that event
        re-derives the state as a side effect — so all of them pass with the
        re-entry hook deleted. A timer park adds nothing to the journal while it
        waits, and a second Runtime has an empty broker, so re-entry is the only
        moment anything can recover what the run read. Getting this wrong fails
        open, which is why it is worth a test that is this specific.
        """
        db = str(tmp_path / "timer.db")

        @workflow(name="read_sleep_delete")
        async def flow(ctx: Context, _: Any = None) -> str:
            await ctx.step(search_the_web, query="what to delete")
            await ctx.sleep(300)
            return await ctx.step(delete_ticket, ticket="T-7")

        from datetime import UTC, datetime, timedelta

        from loom.runtime.clock import ManualClock

        started = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

        first = a_runtime(SQLiteStore(db), clock=ManualClock(started))
        first.register(flow)
        parked = await first.run(flow)
        assert parked.status.value == "suspended"

        # A second process, later, with a broker that has never seen this run.
        second = a_runtime(
            SQLiteStore(db), clock=ManualClock(started + timedelta(seconds=400))
        )
        second.register(flow)
        resumed = await second.resume(parked.run_id)

        assert resumed.status.value == "failed", (
            "a fresh process resumed a timer park having forgotten the read"
        )

    async def test_a_terminal_run_stops_being_tracked(self) -> None:
        """A map keyed by run id and never cleared is a leak the size of the
        process, and a worker is the process that never restarts."""

        @workflow(name="brief")
        async def brief(ctx: Context, _: Any = None) -> str:
            return await ctx.step(search_the_web, query="x")

        rt = a_runtime()
        rt.register(brief)
        result = await rt.run(brief)

        assert result.run_id not in rt.broker._runs


class TestPolicyAndDefaults:
    async def test_a_write_can_be_allowed_while_destructive_is_not(self) -> None:
        """The rates differ enough that one dial cannot serve both: nearly every
        useful workflow writes after reading, and very few need to delete."""

        @workflow(name="read_then_post")
        async def read_then_post(ctx: Context, _: Any = None) -> str:
            await ctx.step(search_the_web, query="news")
            return await ctx.step(post_summary, text="digest")

        rt = a_runtime(policy=TaintPolicy(block_writes=False))
        rt.register(read_then_post)

        assert (await rt.run(read_then_post)).status.value == "completed"

    async def test_an_exempt_target_is_not_blocked(self) -> None:
        @workflow(name="read_then_exempt")
        async def read_then_exempt(ctx: Context, _: Any = None) -> str:
            await ctx.step(search_the_web, query="news")
            return await ctx.step(post_summary, text="digest")

        rt = a_runtime(policy=TaintPolicy(exempt=frozenset({"post_summary"})))
        rt.register(read_then_exempt)

        assert (await rt.run(read_then_exempt)).status.value == "completed"

    async def test_the_default_runtime_is_unaffected(self) -> None:
        """Nothing above happens unless a host asked for it."""

        @workflow(name="read_then_delete_unguarded")
        async def flow(ctx: Context, _: Any = None) -> str:
            await ctx.step(search_the_web, query="anything")
            return await ctx.step(delete_ticket, ticket="T-1")

        rt = Runtime(store=MemoryStore())
        rt.register(flow)

        assert (await rt.run(flow)).status.value == "completed"
