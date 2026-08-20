"""Phases 3 and 4 — the product surface, and scale.

P3 closes the lifecycle gaps a workflow platform is measured on: change a
workflow by asking, hold a run, and turn a production failure into a committed
test. P4 removes the ceilings that only appear at size.
"""

from __future__ import annotations

import asyncio

import pytest

from loom import Context, Runtime, step, workflow
from loom.core.models import ExecutionStatus
from loom.stores.memory import MemoryStore

# ---------------------------------------------------------------------------
# P3 · edit — changing a workflow by describing the change
# ---------------------------------------------------------------------------

_BEFORE = '''from loom import Context, step, workflow


@step
async def fetch(url: str) -> str:
    """Fetch it."""
    return url


@workflow(name="digest")
async def digest(ctx: Context, url: str) -> str:
    return await ctx.step(fetch, url)
'''

_AFTER = '''from loom import Context, step, workflow


@step
async def fetch(url: str) -> str:
    """Fetch it."""
    return url


@step
async def summarise(body: str) -> str:
    """Summarise it."""
    return body[:10]


@workflow(name="digest")
async def digest(ctx: Context, url: str) -> str:
    body = await ctx.step(fetch, url)
    return await ctx.step(summarise, body)
'''


class TestTheEditDiffIsReviewable:
    """A reviewer of new code reads the code; a reviewer of an edit reads the
    difference. LOOM already generates both artifacts at commit time — the
    graph and the narration — so the node-level delta is the reviewable form
    for somebody who does not read Python."""

    def test_the_graph_delta_names_what_appeared(self) -> None:
        from loom.agents.coding_agent import graph_delta

        assert graph_delta(_BEFORE, _AFTER) == ["+summarise"]

    def test_the_graph_delta_names_what_disappeared(self) -> None:
        from loom.agents.coding_agent import graph_delta

        assert graph_delta(_AFTER, _BEFORE) == ["-summarise"]

    def test_an_unchanged_file_has_no_delta(self) -> None:
        from loom.agents.coding_agent import graph_delta

        assert graph_delta(_BEFORE, _BEFORE) == []

    def test_unprojectable_source_yields_no_delta_rather_than_an_error(
        self,
    ) -> None:
        """A diff is a courtesy; the code is the artifact."""
        from loom.agents.coding_agent import graph_delta

        assert graph_delta(_BEFORE, "def broken(:\n") == []

    def test_the_unified_diff_is_a_diff(self) -> None:
        from loom.agents.coding_agent import unified_diff_of

        diff = unified_diff_of(_BEFORE, _AFTER)

        assert diff.startswith("--- before.py")
        assert "+async def summarise" in diff.replace("+@step\n", "")


class TestEditIsOnThePortNotTheCli:
    """`author` was put on the port so the CLI, the MCP server and anything
    else get one implementation. An edit that lived only in the CLI would be
    the second copy, and the second copy is always the one that rots."""

    def test_every_adapter_implements_it(self) -> None:
        import inspect

        from loom.facade import LocalFacade, RemoteFacade, RuntimeFacade
        from loom.identity.facade import AuthorizedFacade

        expected = inspect.signature(RuntimeFacade.edit)
        for adapter in (LocalFacade, RemoteFacade, AuthorizedFacade):
            assert (
                inspect.signature(adapter.edit).parameters == expected.parameters
            ), adapter.__name__

    @pytest.mark.asyncio
    async def test_the_remote_facade_refuses_with_a_reason(self) -> None:
        from loom.core.exceptions import ConfigurationError
        from loom.facade import RemoteFacade

        facade = RemoteFacade.__new__(RemoteFacade)
        with pytest.raises(ConfigurationError) as caught:
            await facade.edit("code", "change it")

        assert "author" in str(caught.value).lower() or "local" in str(
            caught.value
        ).lower()

    @pytest.mark.asyncio
    async def test_editing_needs_its_own_scope(self) -> None:
        from loom.core.exceptions import InsufficientScope
        from loom.identity.facade import AuthorizedFacade
        from loom.identity.principal import Principal

        facade = AuthorizedFacade(
            object(), Principal(subject="u", scopes=frozenset({"workflows:publish"}))
        )

        with pytest.raises(InsufficientScope):
            await facade.edit("code", "change it")

    def test_the_cli_exposes_it(self) -> None:
        from loom.cli import _HANDLERS

        assert "edit" in _HANDLERS


# ---------------------------------------------------------------------------
# P3 · pause — an operator can hold a run
# ---------------------------------------------------------------------------


_TICKS: list[int] = []


@step
async def _tick(n: int) -> int:
    await asyncio.sleep(0.02)
    _TICKS.append(n)
    return n


@workflow(name="p3_holdable")
async def _holdable(ctx: Context, _inp: object = None) -> int:
    total = 0
    for i in range(5):
        total += await ctx.step(_tick, i)
    return total


class TestAnOperatorCanHoldARun:
    """There was no way to do this. A run parked only when its own code said
    so, so the only things an operator could do to a misbehaving run were
    cancel it — terminal, unwinding compensations — or watch it."""

    @pytest.fixture(autouse=True)
    def _clear(self):
        _TICKS.clear()

    @pytest.mark.asyncio
    async def test_a_held_run_stops_between_steps_and_resumes_where_it_was(
        self,
    ) -> None:
        store = MemoryStore()
        runtime = Runtime(store=store, max_inline_wait=0)
        runtime.register(_holdable)

        driving = asyncio.create_task(runtime.run(_holdable, None))
        await asyncio.sleep(0.05)
        run_id = (await store.list_executions())[0].run_id
        await runtime.pause(run_id)
        held = await driving

        assert held.status is ExecutionStatus.SUSPENDED
        done_before = list(_TICKS)
        assert 0 < len(done_before) < 5, "held mid-run, not at either end"

        await runtime.unpause(run_id)
        released = await runtime.wait(run_id, timeout=5)

        assert released.status is ExecutionStatus.COMPLETED
        assert released.output == sum(range(5))
        assert list(range(5)) == _TICKS, "no step ran twice, none was skipped"

    @pytest.mark.asyncio
    async def test_the_request_is_persisted(self) -> None:
        store = MemoryStore()
        runtime = Runtime(store=store, max_inline_wait=0)
        runtime.register(_holdable)

        driving = asyncio.create_task(runtime.run(_holdable, None))
        await asyncio.sleep(0.05)
        run_id = (await store.list_executions())[0].run_id
        await runtime.pause(run_id)
        await driving

        record = await store.get_execution(run_id)
        assert record is not None
        assert record.pause_requested is True
        assert record.awaiting_event == f"resume:{run_id}"

    @pytest.mark.asyncio
    async def test_releasing_clears_the_flag(self) -> None:
        store = MemoryStore()
        runtime = Runtime(store=store, max_inline_wait=0)
        runtime.register(_holdable)

        driving = asyncio.create_task(runtime.run(_holdable, None))
        await asyncio.sleep(0.05)
        run_id = (await store.list_executions())[0].run_id
        await runtime.pause(run_id)
        await driving
        await runtime.unpause(run_id)
        await runtime.wait(run_id, timeout=5)

        record = await store.get_execution(run_id)
        assert record is not None and record.pause_requested is False

    @pytest.mark.asyncio
    async def test_pausing_a_finished_run_says_so(self) -> None:
        from loom.core.exceptions import RegistryError

        runtime = Runtime(store=MemoryStore())
        runtime.register(_holdable)
        result = await runtime.run(_holdable, None)

        with pytest.raises(RegistryError):
            await runtime.pause(result.run_id)

    @pytest.mark.asyncio
    async def test_holding_one_run_does_not_hold_another(self) -> None:
        """The resume event is per run, so releasing one holds nothing else."""
        store = MemoryStore()
        runtime = Runtime(store=store, max_inline_wait=0)
        runtime.register(_holdable)

        first = asyncio.create_task(runtime.run(_holdable, None))
        await asyncio.sleep(0.05)
        held_id = (await store.list_executions())[0].run_id
        await runtime.pause(held_id)
        await first

        second = await runtime.run(_holdable, None)

        assert second.status is ExecutionStatus.COMPLETED

    def test_the_cli_exposes_both_halves(self) -> None:
        from loom.cli import _HANDLERS

        assert "pause" in _HANDLERS
        assert "unpause" in _HANDLERS


# ---------------------------------------------------------------------------
# P3 · pin — a production run becomes a committed test
# ---------------------------------------------------------------------------


@step
async def _reads(api_key: str) -> dict:
    return {"rows": 3, "api_key": api_key}


@step
async def _renders(rows: int) -> str:
    return f"{rows} rows"


@workflow(name="p3_pinnable")
async def _pinnable(ctx: Context, payload: dict) -> str:
    got = await ctx.step(_reads, payload["token"])
    return await ctx.step(_renders, got["rows"])


class TestPinningARun:
    """LOOM has better raw material than any comparable platform — a complete,
    replayable record — and two ways to *look* at it and none to keep it. The
    mechanism existed (`given` seeds a journal entry, and a seeded entry means
    exactly what a recorded one means); nothing connected the two."""

    @pytest.fixture
    async def pinned(self):
        from loom.facade import LocalFacade

        runtime = Runtime(store=MemoryStore())
        runtime.register(_pinnable)
        result = await runtime.run(_pinnable, {"token": "sk-super-secret"})
        return await LocalFacade(runtime).pin(result.run_id, module="flows.pinnable")

    @pytest.mark.asyncio
    async def test_every_step_becomes_a_seed(self, pinned) -> None:
        assert pinned["seeded"] == 2
        assert "given(_reads, returns=" in pinned["source"]
        assert "given(_renders, returns=" in pinned["source"]

    @pytest.mark.asyncio
    async def test_no_secret_reaches_the_generated_file(self, pinned) -> None:
        """A step's *inputs* are redacted into the journal; its outputs are
        not — they are what the step produced, and nobody writing a workflow
        expected them pasted into a file somebody commits."""
        assert "sk-super-secret" not in pinned["source"]

    @pytest.mark.asyncio
    async def test_it_says_when_redaction_changed_the_data(self, pinned) -> None:
        """A pinned test whose input silently became '***' reproduces a
        different run from the one it claims to."""
        assert any("redacted" in note for note in pinned["notes"])

    @pytest.mark.asyncio
    async def test_the_generated_file_is_valid_python(self, pinned) -> None:
        compile(pinned["source"], pinned["filename"], "exec")

    @pytest.mark.asyncio
    async def test_it_asserts_the_status_the_run_reached(self, pinned) -> None:
        assert "ExecutionStatus.COMPLETED" in pinned["source"]

    @pytest.mark.asyncio
    async def test_a_missing_module_is_a_todo_not_a_guess(self) -> None:
        from loom.facade import LocalFacade

        runtime = Runtime(store=MemoryStore())
        runtime.register(_pinnable)
        result = await runtime.run(_pinnable, {"token": "x"})

        pinned = await LocalFacade(runtime).pin(result.run_id)

        assert "TODO" in pinned["source"]
        assert any("no module given" in note for note in pinned["notes"])

    @pytest.mark.asyncio
    async def test_a_failed_run_pins_its_failure(self) -> None:
        from loom.facade import LocalFacade

        @step
        async def explodes(_x: int) -> int:
            raise ValueError("downstream said no")

        @workflow(name="p3_failing")
        async def failing(ctx: Context, _inp: object = None) -> int:
            return await ctx.step(explodes, 1)

        runtime = Runtime(store=MemoryStore())
        runtime.register(failing)
        result = await runtime.run(failing, None)

        pinned = await LocalFacade(runtime).pin(result.run_id, module="flows.failing")

        assert "raises=" in pinned["source"]
        assert "downstream said no" in pinned["source"]
        assert "ExecutionStatus.FAILED" in pinned["source"]

    @pytest.mark.asyncio
    async def test_a_repeated_step_gets_numbered_occurrences(self) -> None:
        """`given` resolves by name and kind, so only the occurrence is
        ordinal — and it has to count per name, not per entry."""
        from loom.facade import LocalFacade

        @step
        async def again(n: int) -> int:
            return n

        @workflow(name="p3_repeats")
        async def repeats(ctx: Context, _inp: object = None) -> int:
            total = 0
            for i in range(3):
                total += await ctx.step(again, i)
            return total

        runtime = Runtime(store=MemoryStore())
        runtime.register(repeats)
        result = await runtime.run(repeats, None)

        pinned = await LocalFacade(runtime).pin(result.run_id, module="flows.repeats")

        assert "occurrence=1" in pinned["source"]
        assert "occurrence=2" in pinned["source"]

    def test_the_cli_exposes_it(self) -> None:
        from loom.cli import _HANDLERS

        assert "pin" in _HANDLERS


# ---------------------------------------------------------------------------
# P4 · the event log does not scan what retention threw away
# ---------------------------------------------------------------------------


class TestEventLogReadsAreBounded:
    """`read` walked every sequence from the caller's position to the head, one
    store round trip each, skipping the ones retention had deleted. A topic
    retained down to its last hundred events with a head in the millions and a
    checkpoint at 1 issued a million single-key gets to find a hundred rows."""

    @pytest.mark.asyncio
    async def test_a_reader_behind_the_window_does_not_walk_the_gap(self) -> None:
        from loom.events.log import StoreBackedEventLog
        from loom.events.models import EventRecord, RetentionPolicy

        class _Counting(MemoryStore):
            gets = 0

            async def get(self, key):
                type(self).gets += 1
                return await super().get(key)

        store = _Counting()
        log = StoreBackedEventLog(store)
        for index in range(60):
            await log.append(
                "t", [EventRecord(event_id=f"e{index}", type="t", payload={})]
            )

        # A count ceiling, not an age cutoff: the point is a live topic whose
        # *old* records are gone, which is the shape a slow reader meets.
        removed = await log.retain(
            "t", RetentionPolicy(max_age_seconds=86_400.0, max_records=5)
        )
        assert removed > 40, f"only {removed} removed — nothing to skip over"

        _Counting.gets = 0
        found = await log.read("t", after=None, limit=10)

        assert found, "the retained tail is still readable"
        assert _Counting.gets < 25, (
            f"{_Counting.gets} round trips to read {len(found)} events — "
            "still walking the deleted range"
        )

    @pytest.mark.asyncio
    async def test_retention_advances_the_tail(self) -> None:
        from loom.events.log import StoreBackedEventLog
        from loom.events.models import EventRecord, RetentionPolicy

        store = MemoryStore()
        log = StoreBackedEventLog(store)
        for index in range(20):
            await log.append(
                "t", [EventRecord(event_id=f"e{index}", type="t", payload={})]
            )

        await log.retain(
            "t", RetentionPolicy(max_age_seconds=86_400.0, max_records=5)
        )

        assert int(await store.get("eventlog:tail:t") or 0) > 0

    @pytest.mark.asyncio
    async def test_two_topics_appending_at_once_both_register(self) -> None:
        """`_remember_topic` is a read-modify-write on one global key while the
        append lock is per topic, so one registration was lost — after which
        retention and `loom events topics` cannot see that topic at all."""
        from loom.events.log import StoreBackedEventLog
        from loom.events.models import EventRecord

        log = StoreBackedEventLog(MemoryStore())

        await asyncio.gather(*(
            log.append(name, [EventRecord(event_id=f"{name}-1", type=name, payload={})])
            for name in ("alpha", "beta", "gamma", "delta")
        ))

        assert set(await log.topics()) == {"alpha", "beta", "gamma", "delta"}


# ---------------------------------------------------------------------------
# P4 · flow control holds across processes
# ---------------------------------------------------------------------------


class TestAdmissionStateIsAPort:
    """Every counter was a process-local dict, so `Runtime(admission=...)`
    provided no concurrency limit, no rate limit and no singleton guarantee in
    any multi-worker deployment — the only kind that has these problems."""

    @pytest.mark.asyncio
    async def test_two_workers_sharing_a_store_share_the_limit(self) -> None:
        from loom.runtime.admission_state import StoreBackedAdmissionState
        from loom.runtime.flowcontrol import (
            AdmissionController,
            AdmissionDecision,
            ConcurrencyPolicy,
            FlowControlPolicy,
        )

        store = MemoryStore()
        policy = FlowControlPolicy(concurrency=ConcurrencyPolicy(limit=1))
        worker_a = AdmissionController(
            state=StoreBackedAdmissionState(store, owner="a")
        )
        worker_b = AdmissionController(
            state=StoreBackedAdmissionState(store, owner="b")
        )

        assert (await worker_a.evaluate("f", policy)).decision is AdmissionDecision.ADMIT
        await worker_a.record_start("f")

        assert (await worker_b.evaluate("f", policy)).decision is AdmissionDecision.DELAY

    @pytest.mark.asyncio
    async def test_the_default_is_still_process_local(self) -> None:
        """Unchanged behaviour for a single process, which is what shipped."""
        from loom.runtime.admission_state import InMemoryAdmissionState
        from loom.runtime.flowcontrol import (
            AdmissionController,
            AdmissionDecision,
            ConcurrencyPolicy,
            FlowControlPolicy,
        )

        policy = FlowControlPolicy(concurrency=ConcurrencyPolicy(limit=1))
        one = AdmissionController(state=InMemoryAdmissionState())
        other = AdmissionController(state=InMemoryAdmissionState())

        await one.record_start("f")

        assert (await other.evaluate("f", policy)).decision is AdmissionDecision.ADMIT

    @pytest.mark.asyncio
    async def test_a_released_slot_is_reusable(self) -> None:
        from loom.runtime.admission_state import StoreBackedAdmissionState
        from loom.runtime.flowcontrol import (
            AdmissionController,
            AdmissionDecision,
            ConcurrencyPolicy,
            FlowControlPolicy,
        )

        store = MemoryStore()
        policy = FlowControlPolicy(concurrency=ConcurrencyPolicy(limit=1))
        controller = AdmissionController(state=StoreBackedAdmissionState(store))

        await controller.record_start("f")
        await controller.record_end("f")

        assert (
            await controller.evaluate("f", policy)
        ).decision is AdmissionDecision.ADMIT

    @pytest.mark.asyncio
    async def test_an_idle_key_expires(self) -> None:
        """The dicts grew one entry per partition key and never shrank, so a
        policy partitioned by customer id was a leak the size of the customer
        list."""
        from loom.runtime.admission_state import InMemoryAdmissionState

        state = InMemoryAdmissionState(ttl_seconds=0.0)
        await state.write("k", 1)

        assert await state.read("k") is None

    @pytest.mark.asyncio
    async def test_a_counter_at_zero_is_removed_not_kept(self) -> None:
        from loom.runtime.admission_state import InMemoryAdmissionState

        state = InMemoryAdmissionState()
        await state.enter("k")
        await state.leave("k")

        assert state._counts == {}

    @pytest.mark.asyncio
    async def test_cancel_previous_is_refused_rather_than_ignored(self) -> None:
        """It admitted the new run and cancelled nothing; the comment said
        "caller is responsible", and no caller was."""
        from loom.runtime.flowcontrol import (
            AdmissionController,
            FlowControlPolicy,
            SingletonPolicy,
        )

        controller = AdmissionController()
        await controller.record_start("f")

        with pytest.raises(NotImplementedError) as caught:
            await controller.evaluate(
                "f", FlowControlPolicy(singleton=SingletonPolicy(mode="cancel_previous"))
            )

        assert "not implemented" in str(caught.value)


# ---------------------------------------------------------------------------
# P4 · a Runtime can say what it can do
# ---------------------------------------------------------------------------


class TestCapabilityReporting:
    """Fifteen of fifty-one constructor parameters are optional ports, reached
    through `getattr(runtime, name, None)` driven by a string map — so mypy sees
    none of them, and an operator could not find out what their deployment
    could do without reading the constructor."""

    def test_a_bare_runtime_reports_what_it_lacks(self) -> None:
        runtime = Runtime(store=MemoryStore())

        capabilities = runtime.capabilities()

        assert capabilities["embeddings"] is False
        assert capabilities["human"] is False

    def test_a_wired_port_reports_as_present(self) -> None:
        from loom.nodes.human.channels import AutoRespondChannel

        runtime = Runtime(store=MemoryStore(), human=AutoRespondChannel())

        assert runtime.capabilities()["human"] is True

    def test_missing_capabilities_answers_in_the_order_asked(self) -> None:
        runtime = Runtime(store=MemoryStore())

        assert runtime.missing_capabilities("embeddings", "vectors") == [
            "embeddings",
            "vectors",
        ]

    def test_the_node_requirement_check_uses_the_same_names(self) -> None:
        """This and `check_requirements` must not disagree about what
        "configured" means."""
        from loom.nodes.base import _CAPABILITY_ATTRS
        from loom.runtime.engine import CAPABILITY_PORTS

        assert set(_CAPABILITY_ATTRS.values()) <= set(CAPABILITY_PORTS)
