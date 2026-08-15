"""The journal is already the right seam for testing a workflow.

A journal entry *is* the statement "this step returned that", and the engine
already prefers a recorded entry to running anything. So a test can say what
happened rather than substituting a stand-in for the step that would have.
"""

from __future__ import annotations

import pytest

from loom import Context, ExecutionStatus, Runtime, step, workflow
from loom.runtime.journal import EntryKind, EntryStatus
from loom.stores.memory import MemoryStore
from loom.testing import assert_replays, given, run_with, seed

executed: list[str] = []


@step
async def expensive(topic: str) -> str:
    executed.append("expensive")
    return "real result"


@step
async def summarize(text: str) -> str:
    executed.append("summarize")
    return f"summary of {text}"


@step
async def notify(text: str) -> str:
    executed.append("notify")
    return "sent"


@workflow(name="seam_pipeline")
async def pipeline(ctx: Context, topic: str = "ai") -> str:
    research = await ctx.step(expensive, topic)
    return await ctx.step(summarize, research)


class TestSeeding:
    def setup_method(self) -> None:
        executed.clear()

    @pytest.mark.asyncio
    async def test_a_seeded_step_does_not_run(self) -> None:
        result = await run_with(pipeline, "ai", given(expensive, returns="canned"))

        assert result.output == "summary of canned"
        assert executed == ["summarize"], "the expensive step should not have run"

    @pytest.mark.asyncio
    async def test_without_seeding_everything_runs(self) -> None:
        result = await run_with(pipeline, "ai")

        assert result.output == "summary of real result"
        assert executed == ["expensive", "summarize"]

    @pytest.mark.asyncio
    async def test_a_seeded_failure_replays_as_a_failure(self) -> None:
        """Error paths without arranging for a real error."""

        @workflow(name="seam_failing")
        async def failing(ctx: Context, _: object = None) -> str:
            value = await ctx.step(expensive, "x")
            return await ctx.step(notify, value)

        result = await run_with(
            failing, None, given(expensive, raises=TimeoutError("smtp down"))
        )

        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert "smtp down" in result.error.message
        assert executed == [], "nothing after the failure should have run"

    def test_returns_and_raises_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            given(expensive, returns="x", raises=RuntimeError("y"))

    def test_seed_builds_entries_a_caller_can_place_itself(self) -> None:
        entries = seed(given(expensive, returns="canned"))

        assert len(entries) == 1
        assert entries[0].name == "expensive"
        assert entries[0].kind is EntryKind.STEP
        assert entries[0].status is EntryStatus.COMPLETED
        assert entries[0].metadata["seeded"] is True

    def test_a_step_a_workflow_or_a_name_all_resolve(self) -> None:
        assert given(expensive).name == "expensive"
        assert given(pipeline).name == "seam_pipeline"
        assert given("expensive").name == "expensive"

    @pytest.mark.asyncio
    async def test_a_mismatched_seed_surfaces_as_divergence(self) -> None:
        """Not silently ignored — the engine's own check catches it."""
        result = await run_with(pipeline, "ai", given(notify, returns="wrong"))

        # The body reaches `expensive` where the seed says `notify`, which is a
        # shape divergence and fails loudly rather than running with a fact
        # nobody used.
        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert "diverged" in result.error.message


class TestAssertReplays:
    def setup_method(self) -> None:
        executed.clear()

    @pytest.mark.asyncio
    async def test_a_deterministic_workflow_passes(self) -> None:
        assert await assert_replays(pipeline, "ai") is not None

    @pytest.mark.asyncio
    async def test_it_catches_an_unjournaled_clock(self) -> None:
        """The failure replay exists to prevent, made visible in a test."""
        import random

        @workflow(name="nondeterministic_flow")
        async def flaky(ctx: Context, _: object = None) -> str:
            # Reading randomness directly rather than through ctx.random().
            return f"value {random.random()}"

        with pytest.raises(AssertionError) as caught:
            await assert_replays(flaky, None)

        message = str(caught.value)
        assert "not deterministic" in message
        assert "ctx.random()" in message

    @pytest.mark.asyncio
    async def test_it_accepts_a_caller_s_runtime(self) -> None:
        rt = Runtime(store=MemoryStore())
        assert await assert_replays(pipeline, "ai", runtime=rt) is not None
