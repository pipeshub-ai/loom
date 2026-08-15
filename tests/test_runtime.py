"""End-to-end runtime tests covering the core execution loop."""

from __future__ import annotations

import pytest

from loom import (
    Context,
    ExecutionStatus,
    Failure,
    OnError,
    Runtime,
    step,
    workflow,
)

# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------


class TestBasicExecution:
    @pytest.mark.asyncio
    async def test_simple_workflow(self) -> None:
        @step
        async def greet(name: str) -> str:
            return f"Hello, {name}!"

        @workflow
        async def hello(ctx: Context, name: str) -> str:
            return await ctx.step(greet, name)

        rt = Runtime()
        result = await rt.run(hello, "world")
        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == "Hello, world!"

    @pytest.mark.asyncio
    async def test_multi_step_workflow(self) -> None:
        @step
        async def add(x: int, y: int) -> int:
            return x + y

        @step
        async def multiply(x: int, y: int) -> int:
            return x * y

        @workflow
        async def math_wf(ctx: Context, x: int) -> int:
            a = await ctx.step(add, x, 10)
            b = await ctx.step(multiply, a, 2)
            return b

        rt = Runtime()
        result = await rt.run(math_wf, 5)
        assert result.output == 30  # (5 + 10) * 2

    @pytest.mark.asyncio
    async def test_identity_workflow(self) -> None:
        @workflow
        async def passthrough(ctx: Context, data: dict) -> dict:
            return data

        rt = Runtime()
        result = await rt.run(passthrough, {"key": "value"})
        assert result.output == {"key": "value"}


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


class TestDeterministicHelpers:
    @pytest.mark.asyncio
    async def test_ctx_now(self) -> None:
        from datetime import datetime

        @workflow
        async def time_wf(ctx: Context, _: None) -> str:
            t = ctx.now()
            assert isinstance(t, datetime)
            return t.isoformat()

        rt = Runtime()
        result = await rt.run(time_wf, None)
        assert result.status is ExecutionStatus.COMPLETED
        assert "T" in result.output  # ISO format

    @pytest.mark.asyncio
    async def test_ctx_uuid4(self) -> None:
        @workflow
        async def uuid_wf(ctx: Context, _: None) -> str:
            return ctx.uuid4()

        rt = Runtime()
        result = await rt.run(uuid_wf, None)
        assert len(result.output) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_ctx_random(self) -> None:
        @workflow
        async def random_wf(ctx: Context, _: None) -> float:
            rng = ctx.random()
            return rng.random()

        rt = Runtime()
        result = await rt.run(random_wf, None)
        assert 0.0 <= result.output <= 1.0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestGather:
    @pytest.mark.asyncio
    async def test_gather_multiple_steps(self) -> None:
        @step
        async def square(x: int) -> int:
            return x * x

        @workflow
        async def gather_wf(ctx: Context, _: None) -> list:
            return await ctx.gather(
                ctx.step(square, 2),
                ctx.step(square, 3),
                ctx.step(square, 4),
            )

        rt = Runtime()
        result = await rt.run(gather_wf, None)
        assert result.output == [4, 9, 16]

    @pytest.mark.asyncio
    async def test_map_items(self) -> None:
        @step
        async def double(x: int) -> int:
            return x * 2

        @workflow
        async def map_wf(ctx: Context, items: list) -> list:
            return await ctx.map(double, items, max_concurrency=2)

        rt = Runtime()
        result = await rt.run(map_wf, [1, 2, 3, 4])
        assert result.output == [2, 4, 6, 8]


# ---------------------------------------------------------------------------
# Retry and error handling
# ---------------------------------------------------------------------------


class TestRetryAndErrors:
    @pytest.mark.asyncio
    async def test_step_failure_propagates(self) -> None:
        call_count = 0

        @step(retry=1)
        async def fail_step(_: None) -> None:
            nonlocal call_count
            call_count += 1
            raise ValueError("intentional")

        @workflow
        async def fail_wf(ctx: Context, _: None) -> None:
            await ctx.step(fail_step, None)

        rt = Runtime()
        result = await rt.run(fail_wf, None)
        assert result.status is ExecutionStatus.FAILED
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_then_succeed(self) -> None:
        attempts = 0

        @step(retry=3)
        async def flaky(x: int) -> int:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError("not yet")  # retryable (not in PERMANENT_ERRORS)
            return x * 10

        @workflow
        async def flaky_wf(ctx: Context, x: int) -> int:
            return await ctx.step(flaky, x)

        rt = Runtime()
        result = await rt.run(flaky_wf, 5)
        assert result.output == 50
        assert attempts == 3

    @pytest.mark.asyncio
    async def test_on_error_continue(self) -> None:
        @step(retry=1, on_error=OnError.CONTINUE, fallback="fallback_value")
        async def risky(_: None) -> str:
            raise RuntimeError("boom")

        @workflow
        async def wf(ctx: Context, _: None) -> str:
            return await ctx.step(risky, None)

        rt = Runtime()
        result = await rt.run(wf, None)
        assert result.output == "fallback_value"

    @pytest.mark.asyncio
    async def test_on_error_route(self) -> None:
        @step(retry=1, on_error=OnError.ROUTE)
        async def risky(_: None) -> str:
            raise RuntimeError("boom")

        @workflow
        async def wf(ctx: Context, _: None) -> object:
            return await ctx.step(risky, None)

        rt = Runtime()
        result = await rt.run(wf, None)
        assert isinstance(result.output, Failure)
        assert result.output.step == "risky"


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


class TestReplay:
    @pytest.mark.asyncio
    async def test_replay_produces_same_output(self) -> None:
        call_count = 0

        @step
        async def counted_step(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x + 1

        @workflow
        async def wf(ctx: Context, x: int) -> int:
            return await ctx.step(counted_step, x)

        rt = Runtime()
        original = await rt.run(wf, 10)
        assert original.output == 11
        assert call_count == 1

        replayed = await rt.replay(original.run_id)
        assert replayed.output == 11
        # Step should NOT re-execute — it's served from journal
        assert call_count == 1


# ---------------------------------------------------------------------------
# Step classes in runtime
# ---------------------------------------------------------------------------


class TestStepClassesInRuntime:
    @pytest.mark.asyncio
    async def test_pure_step(self) -> None:
        from loom.steps.definition import pure

        @pure
        async def format_name(name: str) -> str:
            return name.upper()

        @workflow
        async def wf(ctx: Context, name: str) -> str:
            return await ctx.step(format_name, name)

        rt = Runtime()
        result = await rt.run(wf, "alice")
        assert result.output == "ALICE"

    @pytest.mark.asyncio
    async def test_effect_step(self) -> None:
        from loom.steps.definition import effect

        @effect(timeout=10)
        async def fetch(url: str) -> str:
            return f"data from {url}"

        @workflow
        async def wf(ctx: Context, url: str) -> str:
            return await ctx.step(fetch, url)

        rt = Runtime()
        result = await rt.run(wf, "https://example.com")
        assert result.output == "data from https://example.com"

    @pytest.mark.asyncio
    async def test_node_step(self) -> None:
        from loom.steps.definition import node

        @node
        async def transform(data: dict) -> dict:
            return {k: v * 2 for k, v in data.items()}

        @workflow
        async def wf(ctx: Context, data: dict) -> dict:
            return await ctx.step(transform, data)

        rt = Runtime()
        result = await rt.run(wf, {"a": 1, "b": 2})
        assert result.output == {"a": 2, "b": 4}


# ---------------------------------------------------------------------------
# Retry from failure
# ---------------------------------------------------------------------------


class TestRetryFromFailure:
    @pytest.mark.asyncio
    async def test_retry_reuses_earlier_work(self) -> None:
        step_1_count = 0
        step_2_count = 0
        should_fail = True

        @step
        async def expensive(x: int) -> int:
            nonlocal step_1_count
            step_1_count += 1
            return x * 100

        @step(retry=1)
        async def flaky(x: int) -> int:
            nonlocal step_2_count, should_fail
            step_2_count += 1
            if should_fail:
                raise ValueError("temp failure")
            return x + 1

        @workflow
        async def wf(ctx: Context, x: int) -> int:
            a = await ctx.step(expensive, x)
            b = await ctx.step(flaky, a)
            return b

        rt = Runtime()
        result = await rt.run(wf, 5)
        assert result.status is ExecutionStatus.FAILED
        assert step_1_count == 1
        assert step_2_count == 1

        # Fix the flaky step and retry
        should_fail = False
        result2 = await rt.retry(result.run_id, use_current_code=True)
        assert result2.status is ExecutionStatus.COMPLETED
        assert result2.output == 501  # 5*100 + 1
        # expensive should NOT re-run (replayed from journal)
        assert step_1_count == 1
        # flaky should run again
        assert step_2_count == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_idempotency_key_dedup(self) -> None:
        run_count = 0

        @step
        async def inc(_: None) -> int:
            nonlocal run_count
            run_count += 1
            return run_count

        @workflow
        async def wf(ctx: Context, _: None) -> int:
            return await ctx.step(inc, None)

        rt = Runtime()
        r1 = await rt.run(wf, None, idempotency_key="unique-key-1")
        r2 = await rt.run(wf, None, idempotency_key="unique-key-1")
        assert r1.run_id == r2.run_id
        assert run_count == 1  # only ran once


class TestExecutionResultReads:
    """``print(result)`` is the first thing anyone does with a run.

    The generated repr answered it with every step record, every timestamp, and
    the full usage breakdown — hundreds of characters in which the two facts
    that matter, did it work and what came back, were buried.
    """

    def test_a_completed_run_leads_with_the_output(self) -> None:
        from loom.core.models import ExecutionResult, ExecutionStatus

        text = repr(
            ExecutionResult(
                run_id="run_1",
                workflow="doubler",
                status=ExecutionStatus.COMPLETED,
                output="the answer is 42",
            )
        )
        assert "doubler" in text
        assert "completed" in text
        assert "the answer is 42" in text
        assert len(text) < 200, "a summary should not scroll"

    def test_a_failed_run_leads_with_the_error(self) -> None:
        from loom.core.models import (
            ErrorInfo,
            ExecutionResult,
            ExecutionStatus,
        )

        text = repr(
            ExecutionResult(
                run_id="run_1",
                workflow="breaks",
                status=ExecutionStatus.FAILED,
                error=ErrorInfo(type="RuntimeError", message="upstream is down"),
            )
        )
        assert "failed" in text
        assert "upstream is down" in text

    def test_a_long_output_is_clipped(self) -> None:
        from loom.core.models import ExecutionResult, ExecutionStatus

        text = repr(
            ExecutionResult(
                run_id="run_1",
                workflow="chatty",
                status=ExecutionStatus.COMPLETED,
                output="x" * 5_000,
            )
        )
        assert len(text) < 250
        assert "…" in text

    def test_str_and_repr_agree(self) -> None:
        from loom.core.models import ExecutionResult, ExecutionStatus

        result = ExecutionResult(
            run_id="r", workflow="w", status=ExecutionStatus.COMPLETED, output=1
        )
        assert str(result) == repr(result)

    def test_every_field_is_still_there(self) -> None:
        """Presentation only — nothing is hidden from a caller that asks."""
        from loom.core.models import ExecutionResult, ExecutionStatus

        result = ExecutionResult(
            run_id="r", workflow="w", status=ExecutionStatus.COMPLETED, output=42
        )
        assert result.output == 42
        assert result.run_id == "r"
        assert result.model_dump()["workflow"] == "w"
