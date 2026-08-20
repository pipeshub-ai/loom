"""Phase 1 — correctness of the core.

C1 and C7 are the same defect seen from two ends: the journal identifies a call
by *position* and the graph identifies it by *name*, and neither was right.
Everything else here follows from settling that, or from a control that was
declared and never wired.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from loom import Context, GrantSet, Runtime, step, workflow
from loom.core.exceptions import ConfigurationError
from loom.core.models import ExecutionStatus
from loom.runtime.effects import GuardedBroker
from loom.runtime.journal import VerifyMode
from loom.security.authority import Authority
from loom.stores.memory import MemoryStore

# ---------------------------------------------------------------------------
# C1 — a durable call's identity does not depend on how long the last one took
# ---------------------------------------------------------------------------

#: Mutated between a run and its replay to simulate the same code running on a
#: machine where one step is faster. That is all it took to make two logically
#: distinct call sites swap journal paths.
_LATENCY = {"slow": 0.05}


@step
async def _slow(tag: str) -> str:
    await asyncio.sleep(_LATENCY["slow"])
    return tag


@step
async def _quick(tag: str) -> str:
    return tag


@workflow(name="p1_interleave")
async def _interleave(ctx: Context, _inp: object = None) -> str:
    async def left() -> str:
        await ctx.step(_slow, "L1")
        return await ctx.step(_quick, "L2")

    async def right() -> str:
        await ctx.step(_quick, "R1")
        return await ctx.step(_quick, "R2")

    return ",".join(await ctx.gather(left(), right()))


class TestConcurrentBranchesGetTheirOwnNumbering:
    """The defect: paths came from one counter shared by every branch, taken
    when a call was *constructed*. Under `gather` the numbering therefore
    followed timing, and a replay served each branch the other's recorded
    values — silently, and the run reported ``completed`` with different
    output."""

    @pytest.mark.asyncio
    async def test_a_replay_with_different_timings_produces_the_same_answer(
        self,
    ) -> None:
        _LATENCY["slow"] = 0.05
        runtime = Runtime(store=MemoryStore())
        runtime.register(_interleave)
        original = await runtime.run(_interleave, None)
        assert original.output == "L2,R2"

        _LATENCY["slow"] = 0.0  # the same run, on a machine where it is fast
        replayed = await runtime.replay(original.run_id)

        assert replayed.status is ExecutionStatus.COMPLETED
        assert replayed.output == original.output

    @pytest.mark.asyncio
    async def test_paths_are_branch_local(self) -> None:
        _LATENCY["slow"] = 0.05
        runtime = Runtime(store=MemoryStore())
        runtime.register(_interleave)
        result = await runtime.run(_interleave, None)

        entries = await runtime.store.load_journal(result.run_id)
        by_path = {e.path: e.input["args"][0] for e in entries}

        # Branch 0 owns 0.*, branch 1 owns 1.*, and neither can renumber the
        # other however the timings fall.
        assert by_path == {"0.0": "L1", "0.1": "L2", "1.0": "R1", "1.1": "R2"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("latency", [0.0, 0.005, 0.02, 0.05])
    async def test_the_numbering_is_invariant_under_latency(
        self, latency: float
    ) -> None:
        """The property the design has to hold: shuffle how long steps take,
        and every journal path stays where it was."""
        _LATENCY["slow"] = latency
        runtime = Runtime(store=MemoryStore())
        runtime.register(_interleave)
        result = await runtime.run(_interleave, None)

        entries = await runtime.store.load_journal(result.run_id)
        assert sorted(e.path for e in entries) == ["0.0", "0.1", "1.0", "1.1"]

    @pytest.mark.asyncio
    async def test_already_constructed_calls_keep_their_flat_paths(self) -> None:
        """The compatibility contract. ``ctx.gather(ctx.step(a), ctx.step(b))``
        was already deterministic — both paths are fixed in argument order
        before `gather` ever runs — so it must keep the numbering every journal
        written before this change contains."""

        @workflow(name="p1_flat")
        async def flat(ctx: Context, _inp: object = None) -> list[str]:
            return await ctx.gather(ctx.step(_quick, "a"), ctx.step(_quick, "b"))

        runtime = Runtime(store=MemoryStore())
        runtime.register(flat)
        result = await runtime.run(flat, None)

        entries = await runtime.store.load_journal(result.run_id)
        assert sorted(e.path for e in entries) == ["0", "1"]

    @pytest.mark.asyncio
    async def test_a_nested_gather_nests_its_numbering(self) -> None:
        async def one(c: Context, tag: str) -> str:
            return await c.step(_quick, tag)

        @workflow(name="p1_nested")
        async def nested(ctx: Context, _inp: object = None) -> str:
            async def outer() -> str:
                inner = await ctx.gather(one(ctx, "x"), one(ctx, "y"))
                return "".join(inner)

            results = await ctx.gather(outer(), one(ctx, "z"))
            return "".join(results)

        runtime = Runtime(store=MemoryStore())
        runtime.register(nested)
        result = await runtime.run(nested, None)

        entries = await runtime.store.load_journal(result.run_id)
        paths = sorted(e.path for e in entries)
        # Two coroutine branches inside a coroutine branch: the outer gather
        # owns 0.* and 1.*, and the inner one subdivides 0.* again.
        assert paths == ["0.0.0", "0.1.0", "1.0"]
        assert result.output == "xyz"

    @pytest.mark.asyncio
    async def test_mixed_arguments_stay_deterministic(self) -> None:
        """A gather over both an eager call and a coroutine still numbers the
        same way on every run — construction order, then gather order."""

        @workflow(name="p1_mixed")
        async def mixed(ctx: Context, _inp: object = None) -> list[str]:
            async def branch() -> str:
                return await ctx.step(_quick, "coro")

            return await ctx.gather(ctx.step(_quick, "eager"), branch())

        runtime = Runtime(store=MemoryStore())
        runtime.register(mixed)
        first = await runtime.run(mixed, None)
        second = await runtime.run(mixed, None)

        one = sorted(e.path for e in await runtime.store.load_journal(first.run_id))
        two = sorted(e.path for e in await runtime.store.load_journal(second.run_id))
        assert one == two == ["0", "1.0"]

    @pytest.mark.asyncio
    async def test_max_concurrency_does_not_change_the_numbering(self) -> None:
        @workflow(name="p1_bounded")
        async def bounded(ctx: Context, _inp: object = None) -> list[str]:
            async def branch(tag: str) -> str:
                return await ctx.step(_quick, tag)

            return await ctx.gather(
                *(branch(t) for t in "abcd"), max_concurrency=2
            )

        runtime = Runtime(store=MemoryStore())
        runtime.register(bounded)
        result = await runtime.run(bounded, None)

        entries = await runtime.store.load_journal(result.run_id)
        assert sorted(e.path for e in entries) == ["0.0", "1.0", "2.0", "3.0"]
        assert result.output == ["a", "b", "c", "d"]


class TestRawConcurrencyIsReported:
    """`ctx.gather` is fixed; `asyncio.gather` cannot be, because it is not
    ours. So it is named — at declaration time for a hand-written workflow, and
    as a blocking error for generated code."""

    def test_the_scanner_names_the_ctx_equivalent(self) -> None:
        from loom.runtime.determinism import UNSAFE_CALLS

        for primitive in (
            "asyncio.gather",
            "asyncio.wait",
            "asyncio.as_completed",
            "asyncio.create_task",
            "asyncio.TaskGroup",
        ):
            assert UNSAFE_CALLS[primitive] == "await ctx.gather(...)"

    def test_the_validator_errors_on_a_raw_gather_in_a_body(self) -> None:
        from loom.agents.validator import CodeValidator

        issues = CodeValidator().validate(
            "import asyncio\n"
            "from loom import Context, step, workflow\n"
            "\n"
            "@step\n"
            "async def a(x: int) -> int:\n"
            "    return x\n"
            "\n"
            '@workflow(name="w")\n'
            "async def w(ctx: Context, i: int) -> int:\n"
            "    r = await asyncio.gather(ctx.step(a, 1), ctx.step(a, 2))\n"
            "    return sum(r)\n"
        )

        concurrency = [i for i in issues if "asyncio.gather" in i.message]
        assert concurrency and concurrency[0].severity == "error"
        assert "ctx.gather" in concurrency[0].message

    def test_a_step_body_may_still_use_asyncio_freely(self) -> None:
        """The rule is about *orchestration*. Inside a step, concurrency is
        ordinary Python and allocates no journal paths."""
        from loom.agents.validator import CodeValidator

        issues = CodeValidator().validate(
            "import asyncio\n"
            "from loom import Context, step, workflow\n"
            "\n"
            "@step\n"
            "async def fan(xs: list) -> list:\n"
            "    return await asyncio.gather(*[_one(x) for x in xs])\n"
            "\n"
            "async def _one(x):\n"
            "    return x\n"
            "\n"
            '@workflow(name="w")\n'
            "async def w(ctx: Context, i: list) -> list:\n"
            "    return await ctx.step(fan, i)\n"
        )

        assert [i for i in issues if "asyncio.gather" in i.message] == []


class TestVerificationIsStrictByDefault:
    @pytest.mark.asyncio
    async def test_a_genuine_divergence_now_fails_instead_of_being_served(
        self,
    ) -> None:
        """With branch numbering fixed, an argument mismatch is a real
        divergence rather than the engine's own race, so serving the recorded
        value is no longer defensible."""

        @workflow(name="p1_swap")
        async def original(ctx: Context, _inp: object = None) -> str:
            first = await ctx.step(_quick, "a")
            second = await ctx.step(_quick, "b")
            return f"{first}{second}"

        store = MemoryStore()
        runtime = Runtime(store=store)
        result = await runtime.run(original, None)

        @workflow(name="p1_swap")
        async def swapped(ctx: Context, _inp: object = None) -> str:
            second = await ctx.step(_quick, "b")
            first = await ctx.step(_quick, "a")
            return f"{first}{second}"

        resumed = Runtime(store=store)
        resumed.register(swapped)
        replayed = await resumed.replay(result.run_id)

        assert replayed.status is ExecutionStatus.FAILED

    def test_warn_remains_available_for_state_derived_arguments(self) -> None:
        runtime = Runtime(store=MemoryStore(), verify=VerifyMode.WARN)
        assert runtime.verify is VerifyMode.WARN


# ---------------------------------------------------------------------------
# C6 — a node is not a toolset
# ---------------------------------------------------------------------------


async def _run_node(node_id: str, payload: object, grants: GrantSet) -> object:
    @workflow(name=f"p1_node_{abs(hash((node_id, str(grants))))}", grants=grants)
    async def flow(ctx: Context, _inp: object = None) -> object:
        return await ctx.node(node_id, payload)

    runtime = Runtime(
        store=MemoryStore(), broker=GuardedBroker(), authority=Authority()
    )
    runtime.register(flow)
    return await runtime.run(flow, None)


_SWITCH = ("control.switch", {"value": "a", "cases": {"a": 1}})


class TestNodesHaveTheirOwnGrantDimension:
    """A node call journals as a step whose target is ``<category>.<id>``, and
    the broker's bridged-toolset branch read ``control`` as a toolset — one no
    manifest declares and no grant can name. So declaring any toolset grant
    denied every ``ctx.node()`` call, including pure computation, and told the
    author to grant a toolset that does not exist."""

    @pytest.mark.asyncio
    async def test_a_toolset_grant_no_longer_denies_a_computation_node(
        self,
    ) -> None:
        result = await _run_node(*_SWITCH, GrantSet(toolsets=["jira.issues:read"]))
        assert result.status is ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_self_contained_categories_pass_even_under_strict(self) -> None:
        result = await _run_node(*_SWITCH, GrantSet(strict=True))
        assert result.status is ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_a_reaching_node_is_denied_when_the_dimension_is_declared(
        self,
    ) -> None:
        result = await _run_node(
            "io.http_request",
            {"url": "https://example.invalid"},
            GrantSet(nodes=["human"]),
        )
        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert result.error.type == "EffectDenied"
        assert "io.http_request" in result.error.message

    @pytest.mark.asyncio
    async def test_a_reaching_node_is_denied_under_strict_with_no_entry(
        self,
    ) -> None:
        result = await _run_node(
            "io.http_request",
            {"url": "https://example.invalid"},
            GrantSet(strict=True),
        )
        assert result.error is not None
        assert result.error.type == "EffectDenied"

    def test_a_category_entry_permits_the_whole_category(self) -> None:
        grant = GrantSet(nodes=["io"])
        assert grant.allows_node("io.http_request")
        assert grant.allows_node("io.wait_for_webhook")
        assert not grant.allows_node("human.approval")

    def test_a_full_id_permits_only_itself(self) -> None:
        grant = GrantSet(nodes=["human.approval"])
        assert grant.allows_node("human.approval")
        assert not grant.allows_node("human.escalate")

    def test_declaring_only_nodes_is_not_an_empty_grant(self) -> None:
        assert not GrantSet(nodes=["io"]).is_empty

    @pytest.mark.asyncio
    async def test_strict_closes_artifact_and_event_kinds_too(self) -> None:
        """`strict` promises every dimension is deny-by-default. `artifact` and
        `event` had no branch at all, so it did not cover them."""
        from loom.runtime.effects import EffectCall, GuardedBroker

        broker = GuardedBroker()
        authority = Authority(grant=GrantSet(strict=True))

        async def perform() -> str:
            return "done"

        for kind in ("artifact", "event"):
            outcome = await broker.dispatch(
                EffectCall(kind=kind, target="x", perform=perform), authority
            )
            assert not outcome.ok, kind


# ---------------------------------------------------------------------------
# C7 — the trace overlay matches something
# ---------------------------------------------------------------------------


_TRACED_SOURCE = '''
from datetime import timedelta

from loom import Context, step, workflow


@step
async def fetch(x: int) -> int:
    return x + 1


@step
async def summarise(x: int) -> str:
    return f"v{x}"


@workflow(name="traced")
async def traced(ctx: Context, _inp=None) -> str:
    a = await ctx.step(fetch, 1)
    for _ in range(2):
        a = await ctx.step(fetch, a)
    got = await ctx.gather(ctx.step(fetch, 1), ctx.step(fetch, 2))
    return await ctx.step(summarise, a + sum(got))
'''


@pytest.fixture
def traced_module(tmp_path: Path):
    import importlib.util

    path = tmp_path / "traced_flow.py"
    path.write_text(_TRACED_SOURCE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("traced_flow", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


class TestRunTraceMatchesRealJournals:
    """The previous matcher compared a journal *path* (``"0"``, ``"3.1"``)
    against a graph node *id* (``fetch``, ``jira.create_issue``) — two
    namespaces that have never overlapped, so it matched nothing, ever. A
    completed run rendered with every node pending, and the test that covered
    it hand-built an entry with ``path="fetch"``, a shape the engine has never
    produced."""

    @pytest.mark.asyncio
    async def test_a_completed_run_leaves_no_durable_node_pending(
        self, traced_module
    ) -> None:
        from loom.graph.pipeline import build_graph
        from loom.graph.trace import overlay_journal

        module, path = traced_module
        runtime = Runtime(store=MemoryStore())
        runtime.register(module.traced)
        result = await runtime.run(module.traced, None)
        assert result.status is ExecutionStatus.COMPLETED

        graph = build_graph(path, flow_id="traced")
        overlay = overlay_journal(
            graph, await runtime.store.load_journal(result.run_id), run_id=result.run_id
        )

        pending = [
            node_id
            for node_id, trace in overlay.node_traces.items()
            if trace.status == "pending"
        ]
        assert pending == []

    @pytest.mark.asyncio
    async def test_nothing_in_the_journal_goes_unmatched(
        self, traced_module
    ) -> None:
        """The visibility requirement: a broken overlay must be loud, not
        empty. `unmatched_entries` is how a stale committed graph or an
        extractor gap becomes something an operator can see."""
        from loom.graph.pipeline import build_graph
        from loom.graph.trace import overlay_journal

        module, path = traced_module
        runtime = Runtime(store=MemoryStore())
        runtime.register(module.traced)
        result = await runtime.run(module.traced, None)

        overlay = overlay_journal(
            build_graph(path, flow_id="traced"),
            await runtime.store.load_journal(result.run_id),
        )

        assert overlay.unmatched_entries == []

    @pytest.mark.asyncio
    async def test_repeated_calls_are_conserved_across_their_nodes(
        self, traced_module
    ) -> None:
        """One label at several call sites, one of them in a loop.

        The guarantee is conservation, not per-site attribution: every entry
        lands on a node carrying that label, and the counts add up. Which of
        five ``fetch`` entries belongs to which of four ``fetch`` sites is not
        recoverable from a journal that records what ran rather than which line
        issued it — so the matcher does not pretend otherwise, and neither does
        this test.
        """
        from loom.graph.pipeline import build_graph
        from loom.graph.trace import overlay_journal

        module, path = traced_module
        runtime = Runtime(store=MemoryStore())
        runtime.register(module.traced)
        result = await runtime.run(module.traced, None)
        journal = await runtime.store.load_journal(result.run_id)

        overlay = overlay_journal(build_graph(path, flow_id="traced"), journal)

        fetch_entries = sum(1 for e in journal if e.name == "fetch")
        matched = sum(
            trace.entries
            for node_id, trace in overlay.node_traces.items()
            if node_id.startswith("fetch")
        )
        assert matched == fetch_entries
        assert all(
            overlay.node_traces[n].status == "completed"
            for n in overlay.node_traces
            if n.startswith("fetch")
        )

    @pytest.mark.asyncio
    async def test_control_flow_nodes_are_structural_not_pending(
        self, traced_module
    ) -> None:
        from loom.graph.pipeline import build_graph
        from loom.graph.trace import overlay_journal

        module, path = traced_module
        runtime = Runtime(store=MemoryStore())
        runtime.register(module.traced)
        result = await runtime.run(module.traced, None)

        overlay = overlay_journal(
            build_graph(path, flow_id="traced"),
            await runtime.store.load_journal(result.run_id),
        )

        assert overlay.node_traces["return"].status == "structural"
        assert overlay.node_traces["loop"].status == "structural"

    @pytest.mark.asyncio
    async def test_the_facade_trace_shows_the_same_thing(
        self, traced_module
    ) -> None:
        """What `loom watch` and the canvas actually read."""
        from loom.facade import LocalFacade

        module, _ = traced_module
        runtime = Runtime(store=MemoryStore())
        runtime.register(module.traced)
        result = await runtime.run(module.traced, None)

        trace = await LocalFacade(runtime).trace(result.run_id)
        statuses = {n["id"]: n["data"]["status"] for n in trace["nodes"]}

        assert "completed" in statuses.values()
        assert statuses.get("summarise") == "completed"

    @pytest.mark.asyncio
    async def test_an_unmatched_entry_is_reported(self, traced_module) -> None:
        from loom.graph.pipeline import build_graph
        from loom.graph.trace import overlay_journal
        from loom.runtime.journal import EntryKind, EntryStatus, JournalEntry

        _, path = traced_module
        stray = JournalEntry(
            path="99",
            kind=EntryKind.STEP,
            name="a_step_the_graph_does_not_contain",
            status=EntryStatus.COMPLETED,
        )
        overlay = overlay_journal(build_graph(path, flow_id="traced"), [stray])

        assert overlay.unmatched_entries == ["a_step_the_graph_does_not_contain"]

    @pytest.mark.asyncio
    async def test_time_travel_uses_the_same_matcher(self, traced_module) -> None:
        """It had its own copy of the matching rule, and inherited its defect."""
        from loom.graph.pipeline import build_graph
        from loom.graph.timetravel import TimeTraveler

        module, path = traced_module
        runtime = Runtime(store=MemoryStore())
        runtime.register(module.traced)
        result = await runtime.run(module.traced, None)
        journal = await runtime.store.load_journal(result.run_id)

        traveller = TimeTraveler(build_graph(path, flow_id="traced"), journal)
        first = traveller.snapshot_at(0)
        last = traveller.snapshot_at(traveller.max_seq)

        def completed(graph) -> int:
            return sum(
                1 for n in graph.nodes if n.metadata.get("status") == "completed"
            )

        assert completed(first) == 1
        assert completed(last) > completed(first)


# ---------------------------------------------------------------------------
# H4 — cancellation survives leaving the process
# ---------------------------------------------------------------------------


@step
async def _record_compensation(marker: list[str]) -> str:
    return "did work"


class TestCancellationIsDurable:
    """`cancel()` recorded the request in an in-memory set and wrote CANCELLED
    straight onto the record. A worker in another process never saw it, kept
    running steps, and overwrote the status on its next update — and because
    its body never raised, the compensation stack never unwound."""

    @pytest.mark.asyncio
    async def test_the_request_is_persisted(self) -> None:
        @workflow(name="p1_cancellable")
        async def flow(ctx: Context, _inp: object = None) -> str:
            await ctx.wait_for_event("never")
            return "done"

        store = MemoryStore()
        runtime = Runtime(store=store, max_inline_wait=0)
        runtime.register(flow)
        result = await runtime.run(flow, None)

        await runtime.cancel(result.run_id)
        record = await store.get_execution(result.run_id)

        assert record is not None
        assert record.cancel_requested is True

    @pytest.mark.asyncio
    async def test_another_process_observes_the_request(self) -> None:
        """Two Runtimes over one store — the shape a real deployment has, and
        the one an in-memory set cannot serve."""

        @workflow(name="p1_two_process")
        async def flow(ctx: Context, _inp: object = None) -> str:
            await ctx.wait_for_event("never")
            return "done"

        store = MemoryStore()
        worker = Runtime(store=store, node_id="worker", max_inline_wait=0)
        worker.register(flow)
        result = await worker.run(flow, None)

        operator = Runtime(store=store, node_id="operator")
        operator.register(flow)
        await operator.cancel(result.run_id)

        record = await store.get_execution(result.run_id)
        assert record is not None and record.cancel_requested

    @pytest.mark.asyncio
    async def test_an_unleased_run_still_goes_terminal_immediately(self) -> None:
        """Nothing is driving a suspended run, so there is no body to raise
        inside and cancelling it means writing the status — the behaviour every
        release before this had."""

        @workflow(name="p1_unleased")
        async def flow(ctx: Context, _inp: object = None) -> str:
            await ctx.wait_for_event("never")
            return "done"

        store = MemoryStore()
        runtime = Runtime(store=store, max_inline_wait=0)
        runtime.register(flow)
        result = await runtime.run(flow, None)

        await runtime.cancel(result.run_id)
        record = await store.get_execution(result.run_id)

        assert record is not None
        assert record.status is ExecutionStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_a_durable_call_is_the_cancellation_boundary(self) -> None:
        """Cancellation takes effect between steps — never mid-step — which is
        what lets the journal stay consistent and the compensations run."""
        performed: list[str] = []

        @step
        async def note(tag: str) -> str:
            performed.append(tag)
            return tag

        @workflow(name="p1_boundary")
        async def flow(ctx: Context, _inp: object = None) -> str:
            await ctx.step(note, "first")
            ctx._runtime._cancelled.add(ctx.run_id)  # a cancel landing mid-body
            await ctx.step(note, "second")
            return "unreachable"

        runtime = Runtime(store=MemoryStore())
        runtime.register(flow)
        result = await runtime.run(flow, None)

        assert result.status is ExecutionStatus.CANCELLED
        assert performed == ["first"]

    @pytest.mark.asyncio
    async def test_compensations_unwind_on_a_mid_body_cancel(self) -> None:
        unwound: list[str] = []

        async def rollback(tag: str) -> None:
            unwound.append(tag)

        @step
        async def book(tag: str) -> str:
            return tag

        @workflow(name="p1_compensate")
        async def flow(ctx: Context, _inp: object = None) -> str:
            await ctx.step(book, "seat")
            await ctx.compensate(rollback, "seat")
            ctx._runtime._cancelled.add(ctx.run_id)
            await ctx.step(book, "hotel")
            return "unreachable"

        runtime = Runtime(store=MemoryStore())
        runtime.register(flow)
        await runtime.run(flow, None)

        assert unwound == ["seat"]


# ---------------------------------------------------------------------------
# H9 — what code an in-flight run resumes against
# ---------------------------------------------------------------------------


class TestVersionPolicy:
    """`resolve_workflow` read the in-process registry and nothing else, so a
    run parked on a 24-hour approval resumed against whatever was deployed in
    the meantime. The version store existed and was consulted only to recover
    source text for a sandbox."""

    @staticmethod
    async def _parked_then_redeployed(policy):

        store = MemoryStore()

        @workflow(name="p1_deployable")
        async def v1(ctx: Context, _inp: object = None) -> str:
            await ctx.wait_for_event("go")
            return await ctx.step(_quick, "v1")

        starter = Runtime(store=store, max_inline_wait=0)
        starter.register(v1)
        run = await starter.run(v1, None)

        @workflow(name="p1_deployable")
        async def v2(ctx: Context, _inp: object = None) -> str:
            await ctx.wait_for_event("go")
            return await ctx.step(_quick, "v2")

        resumed = Runtime(store=store, version_policy=policy, max_inline_wait=0)
        resumed.register(v2)
        await resumed.send_event(run.run_id, "go")
        return await resumed.wait(run.run_id, timeout=5)

    @pytest.mark.asyncio
    async def test_latest_resumes_against_current_code(self) -> None:
        from loom.runtime.versions import VersionPolicy

        result = await self._parked_then_redeployed(VersionPolicy.LATEST)

        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == "v2"

    @pytest.mark.asyncio
    async def test_refuse_will_not_resume_a_changed_body(self) -> None:
        from loom.runtime.versions import VersionPolicy

        result = await self._parked_then_redeployed(VersionPolicy.REFUSE)

        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None and result.error.type == "CodeChanged"

    @pytest.mark.asyncio
    async def test_pinned_refuses_rather_than_falling_back(self) -> None:
        """Asking to be pinned and silently getting `LATEST` is the one outcome
        this dial exists to make impossible."""
        from loom.runtime.versions import VersionPolicy

        result = await self._parked_then_redeployed(VersionPolicy.PINNED)

        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None and result.error.type == "CodeChanged"

    @pytest.mark.asyncio
    async def test_unchanged_code_is_never_refused(self) -> None:
        from loom.runtime.versions import VersionPolicy

        @workflow(name="p1_stable")
        async def flow(ctx: Context, _inp: object = None) -> str:
            return await ctx.step(_quick, "same")

        runtime = Runtime(
            store=MemoryStore(), version_policy=VersionPolicy.REFUSE
        )
        runtime.register(flow)
        result = await runtime.run(flow, None)

        assert result.status is ExecutionStatus.COMPLETED

    def test_the_default_is_the_historical_behaviour(self) -> None:
        from loom.runtime.versions import VersionPolicy

        assert Runtime(store=MemoryStore()).version_policy is VersionPolicy.LATEST


# ---------------------------------------------------------------------------
# H1 — a control that was never wired is refused, not advertised
# ---------------------------------------------------------------------------


class TestStrictDeterminismIsRefused:
    """It was assigned to `self.strict_determinism` and read at zero sites,
    while the documentation said "violating these raises NondeterminismError in
    strict mode". Refused rather than removed, so a host that set it is told
    what it actually had."""

    def test_setting_it_raises_and_names_the_alternatives(self) -> None:
        with pytest.raises(ConfigurationError) as caught:
            Runtime(store=MemoryStore(), strict_determinism=True)

        message = str(caught.value)
        assert "never enforced anything" in message
        assert "assert_replays" in message

    def test_leaving_it_alone_is_unchanged(self) -> None:
        assert Runtime(store=MemoryStore(), strict_determinism=False) is not None

    def test_no_runtime_parameter_is_dead(self) -> None:
        """The meta-check. `strict_determinism` reached production because
        nothing asked whether a constructor parameter was ever read."""
        import ast
        import inspect
        import re
        import textwrap

        import loom.runtime.engine as engine

        tree = ast.parse(textwrap.dedent(inspect.getsource(engine.Runtime.__init__)))
        params = [
            a.arg
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            for a in node.args.kwonlyargs
        ]
        source = "".join(
            path.read_text(encoding="utf-8")
            for path in Path(engine.__file__).parent.parent.rglob("*.py")
        )
        dead = [
            name
            for name in params
            if len(re.findall(rf"\b{name}\b", source)) <= 2
        ]
        assert dead == [], f"constructor parameters nothing reads: {dead}"


# ---------------------------------------------------------------------------
# H8 — who approved is attested, not asserted
# ---------------------------------------------------------------------------


class TestApprovalProvenance:
    """`responder` was read straight out of the answer payload, so the audit
    record for the act that *clears read-to-write taint* said whatever the
    caller typed."""

    def test_an_unauthenticated_answer_keeps_its_claim(self) -> None:
        from datetime import UTC, datetime

        from loom.nodes.human.nodes import _approval_from

        decided = _approval_from(
            {"approved": True, "responder": "ops@acme.com"},
            decided_at=datetime.now(UTC),
        )
        assert decided.responder == "ops@acme.com"

    def test_an_attested_identity_overrides_a_claimed_one(self) -> None:
        from datetime import UTC, datetime

        from loom.nodes.human.attest import attest
        from loom.nodes.human.nodes import _approval_from

        forged = {"approved": True, "responder": "ceo@acme.com"}
        decided = _approval_from(
            attest(forged, "intern@acme.com"), decided_at=datetime.now(UTC)
        )

        assert decided.responder == "intern@acme.com"

    def test_a_bare_answer_still_carries_the_identity(self) -> None:
        from loom.nodes.human.attest import attest, responder_of

        assert responder_of(attest(True, "ops@acme.com")) == "ops@acme.com"

    def test_nothing_is_stamped_without_a_subject(self) -> None:
        from loom.nodes.human.attest import ATTESTED_KEY, attest

        assert ATTESTED_KEY not in attest({"approved": True}, "")

    @pytest.mark.asyncio
    async def test_the_authorized_facade_stamps_the_caller(self) -> None:
        from loom.identity.facade import AuthorizedFacade
        from loom.identity.principal import Principal
        from loom.nodes.human.attest import ATTESTED_KEY

        seen: dict[str, object] = {}

        class _Recording:
            async def get(self, run_id: str) -> dict[str, object]:
                return {"run_id": run_id, "metadata": {}}

            async def send_event(self, run_id, name, payload, *, dedupe_key=None):
                seen["payload"] = payload
                return {"delivered": True}

        facade = AuthorizedFacade(
            _Recording(),
            Principal(subject="alice@acme.com", scopes=frozenset({"admin"})),
        )
        await facade.send_event("run-1", "approval:refund", {"approved": True})

        assert seen["payload"][ATTESTED_KEY] == "alice@acme.com"
