"""Coverage for the final P2 items.

Journal growth guards, `ctx.emit`, import-symbol validation, the supervisor
review pass, and the persisted workflow catalog.

Includes a drift test that would have caught the `ctx.emit` bug on the day it
was introduced: every `ctx.*` name the graph extractor knows about must exist on
`Context`.
"""

from __future__ import annotations

import inspect

import pytest

from loom import Context, ExecutionStatus, Runtime, step, workflow
from loom.agents.result import AgentResult
from loom.agents.supervisor import (
    CodeSupervisor,
    Finding,
    SupervisorVerdict,
)
from loom.agents.validator import CodeValidator
from loom.core.exceptions import BudgetExceeded
from loom.runtime.registry import (
    InMemoryWorkflowRegistry,
    StoreBackedWorkflowRegistry,
    WorkflowRecord,
    record_for,
)
from loom.stores.memory import MemoryStore
from loom.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Journal growth guard
# ---------------------------------------------------------------------------


@step
async def tick(n: int) -> int:
    """One journaled operation."""
    return n


@workflow(name="chatty")
async def chatty(ctx: Context, count: int) -> int:
    """Journal `count` operations."""
    for i in range(count):
        await ctx.step(tick, i)
    return count


class TestJournalGrowthGuard:
    async def test_a_normal_run_is_unaffected(self) -> None:
        rt = Runtime(store=MemoryStore())
        assert (await rt.run(chatty, 5)).status is ExecutionStatus.COMPLETED

    async def test_passing_the_hard_limit_fails_the_run(self) -> None:
        rt = Runtime(store=MemoryStore(), journal_max_entries=10)
        result = await rt.run(chatty, 50)

        assert result.status is ExecutionStatus.FAILED
        assert "continue_as_new" in result.error.message

    async def test_the_failure_names_the_budget(self) -> None:
        rt = Runtime(store=MemoryStore(), journal_max_entries=5)
        result = await rt.run(chatty, 50)

        assert result.error.type == BudgetExceeded.__name__

    async def test_the_soft_limit_warns_once(self, caplog) -> None:
        import logging

        rt = Runtime(
            store=MemoryStore(), journal_warn_entries=3, journal_max_entries=0
        )
        with caplog.at_level(logging.WARNING, logger="workflow.chatty"):
            result = await rt.run(chatty, 20)

        assert result.status is ExecutionStatus.COMPLETED
        warnings = [r for r in caplog.records if "continue_as_new" in r.getMessage()]
        # Warned, and only once — not on every operation past the threshold.
        assert len(warnings) == 1

    async def test_zero_disables_the_hard_limit(self) -> None:
        rt = Runtime(store=MemoryStore(), journal_max_entries=0)
        assert (await rt.run(chatty, 30)).status is ExecutionStatus.COMPLETED

    async def test_continue_as_new_keeps_a_run_under_the_limit(self) -> None:
        """The escape hatch the error message recommends actually works."""

        @workflow(name="rotating")
        async def rotating(ctx: Context, remaining: int) -> int:
            for i in range(3):
                await ctx.step(tick, i)
            if remaining > 1:
                await ctx.continue_as_new(remaining - 1)
            return remaining

        rt = Runtime(store=MemoryStore(), journal_max_entries=8)
        try:
            result = await rt.run(rotating, 4)
            # Would have journaled 12 operations in one run; each rotation gets
            # a fresh journal, so none of them approaches the limit.
            assert result.status is ExecutionStatus.COMPLETED
        finally:
            await rt.shutdown()


# ---------------------------------------------------------------------------
# ctx.emit and extractor drift
# ---------------------------------------------------------------------------


class TestEmit:
    async def test_emit_reaches_a_waiting_run(self) -> None:
        @workflow(name="listener")
        async def listener(ctx: Context, _input: str) -> str:
            payload = await ctx.wait_for_event("order.shipped")
            return str(payload["order"])

        @workflow(name="shipper")
        async def shipper(ctx: Context, order: str) -> str:
            await ctx.emit("order.shipped", {"order": order})
            return order

        rt = Runtime(store=MemoryStore())
        parked = await rt.run(listener, "go")
        assert parked.status is ExecutionStatus.SUSPENDED

        await rt.run(shipper, "A-1")
        resumed = await rt.resume(parked.run_id)

        assert resumed.status is ExecutionStatus.COMPLETED
        assert resumed.output == "A-1"

    async def test_emit_is_journaled_so_replay_does_not_resend(self) -> None:
        @workflow(name="emitter")
        async def emitter(ctx: Context, _input: str) -> str:
            await ctx.emit("thing.happened", {"n": 1})
            return "done"

        rt = Runtime(store=MemoryStore())
        first = await rt.run(emitter, "go")

        entries = await rt.history(first.run_id)
        assert [e.name for e in entries] == ["emit:thing.happened"]

        # Replay serves the emit from the journal rather than firing again.
        replayed = await rt.replay(first.run_id)
        assert replayed.status is ExecutionStatus.COMPLETED

    def test_every_extractor_ctx_call_exists_on_context(self) -> None:
        """Drift guard.

        The extractor mapped `ctx.emit` to a node kind while `Context` had no
        such method — it was reading an API that did not exist. This fails the
        moment that happens again.
        """
        from loom.graph.extractor import _CTX_CALL_MAP

        missing = [name for name in _CTX_CALL_MAP if not hasattr(Context, name)]
        assert missing == [], f"extractor references non-existent ctx.{missing}"

    def test_the_extractor_knows_about_the_durable_context_api(self) -> None:
        """The reverse direction, as a warning rather than a hard rule.

        Not every Context method belongs in a graph — bookkeeping like `tag` and
        `set_metadata` has no visual meaning — but a durable operation that the
        extractor does not know about is invisible on the canvas.
        """
        from loom.graph.extractor import _CTX_CALL_MAP

        durable = {
            "step", "map", "gather", "sleep", "sleep_until", "wait_for_event",
            "wait_for_approval", "child", "agent", "publish", "emit", "signal",
        }
        assert durable <= set(_CTX_CALL_MAP)


# ---------------------------------------------------------------------------
# Import symbol validation
# ---------------------------------------------------------------------------


_VALID = '''
from loom import Context, Retry, step, workflow

@step(retry=Retry(max_attempts=2))
async def s() -> int:
    """Do a thing."""
    return 1

@workflow(name="w")
async def w(ctx: Context, x: int) -> int:
    return await ctx.step(s)
'''


class TestSymbolValidation:
    def test_valid_imports_pass(self) -> None:
        assert [i for i in CodeValidator().validate(_VALID) if i.category == "imports"] == []

    def test_a_misspelled_sdk_symbol_is_caught(self) -> None:
        issues = CodeValidator().validate(_VALID.replace("Retry,", "Retryy,"))
        messages = [i.message for i in issues if i.category == "imports"]

        assert any("Retryy" in m for m in messages)

    def test_the_message_suggests_the_real_name(self) -> None:
        issues = CodeValidator().validate(_VALID.replace("Retry,", "Retryy,"))
        message = next(i.message for i in issues if i.category == "imports")

        assert "did you mean 'Retry'" in message

    def test_submodules_are_not_flagged(self) -> None:
        """`from loom import stores` is valid even though `stores` is
        not an attribute until it has been imported."""
        code = "from loom import stores\n" + _VALID
        issues = CodeValidator().validate(code)

        assert not any("stores" in i.message for i in issues if i.category == "imports")

    def test_deep_module_paths_resolve(self) -> None:
        code = _VALID + "\nfrom loom.stores.memory import MemoryStore\n"
        assert not [i for i in CodeValidator().validate(code) if i.category == "imports"]

    def test_a_bad_symbol_in_a_deep_path_is_caught(self) -> None:
        code = _VALID + "\nfrom loom.stores.memory import MemoryStorage\n"
        issues = CodeValidator().validate(code)

        assert any("MemoryStorage" in i.message for i in issues)

    def test_third_party_modules_are_not_imported(self) -> None:
        """Importing an arbitrary package to check a name would run its side
        effects during a static check."""
        code = "from definitely_not_installed_xyz import Thing\n" + _VALID
        issues = CodeValidator().validate(code)

        assert not any("Thing" in i.message for i in issues)

    def test_star_imports_are_skipped(self) -> None:
        code = "from loom import *\n" + _VALID
        assert not [i for i in CodeValidator().validate(code) if i.category == "imports"]

    def test_relative_imports_are_skipped(self) -> None:
        code = "from . import helpers\n" + _VALID
        assert not [i for i in CodeValidator().validate(code) if i.category == "imports"]


# ---------------------------------------------------------------------------
# Supervisor review
# ---------------------------------------------------------------------------


class ScriptedSupervisor(CodeSupervisor):
    """A supervisor with canned verdicts, so the loop can be tested offline."""

    def __init__(self, verdicts: list[SupervisorVerdict]) -> None:
        super().__init__(model=object())
        self._verdicts = list(verdicts)
        self.reviewed: list[str] = []

    async def review(self, spec: str, code: str) -> SupervisorVerdict:
        self.reviewed.append(code)
        if self._verdicts:
            return self._verdicts.pop(0)
        return SupervisorVerdict(approved=True)


class TestSupervisorVerdict:
    def test_approved_with_no_findings_is_not_blocking(self) -> None:
        assert not SupervisorVerdict(approved=True).blocking

    def test_an_error_finding_blocks_even_when_approved(self) -> None:
        """A reviewer that approves while listing an error contradicts itself;
        the finding wins."""
        verdict = SupervisorVerdict(
            approved=True,
            findings=[Finding(severity="error", category="retry-safety", message="x")],
        )
        assert verdict.blocking

    def test_warnings_alone_do_not_block(self) -> None:
        verdict = SupervisorVerdict(
            approved=True,
            findings=[Finding(severity="warning", category="durability", message="x")],
        )
        assert not verdict.blocking

    def test_feedback_lists_every_finding(self) -> None:
        verdict = SupervisorVerdict(
            approved=False,
            findings=[
                Finding(severity="error", category="retry-safety", message="double charge"),
                Finding(severity="warning", category="durability", message="io in body"),
            ],
        )
        feedback = verdict.as_feedback()

        assert "double charge" in feedback
        assert "io in body" in feedback

    def test_feedback_carries_the_code_being_revised(self) -> None:
        """The author agent is ephemeral: feedback without the code asks it to
        edit something it can no longer see, and it invents a reply instead."""
        verdict = SupervisorVerdict(
            approved=False,
            findings=[Finding(severity="error", category="durability", message="x")],
        )
        feedback = verdict.as_feedback("async def flow(ctx): ...")

        assert "async def flow(ctx): ..." in feedback

    def test_rejection_without_findings_still_says_something(self) -> None:
        assert "no findings" in SupervisorVerdict(approved=False).as_feedback()

    async def test_a_broken_supervisor_approves_rather_than_failing(self) -> None:
        """A reviewer that errors must not fail the generation it was advising."""

        class ExplodingModel:
            model_name = "boom"

            async def complete(self, request):
                raise RuntimeError("provider is down")

        verdict = await CodeSupervisor(ExplodingModel()).review("spec", "code")

        assert verdict.approved
        assert "unavailable" in verdict.summary

    def test_instructions_are_customizable(self) -> None:
        from loom.agents.supervisor import DEFAULT_SUPERVISOR_PROMPT

        replaced = CodeSupervisor(object(), instructions="Only check for typos.")
        assert replaced.build_prompt() == "Only check for typos."

        appended = CodeSupervisor(object(), extra_instructions="Also: no eval().")
        prompt = appended.build_prompt()
        assert DEFAULT_SUPERVISOR_PROMPT in prompt
        assert prompt.endswith("Also: no eval().")


# ---------------------------------------------------------------------------
# Persisted workflow catalog
# ---------------------------------------------------------------------------


@workflow(name="catalogued", description="A workflow worth publishing")
async def catalogued(ctx: Context, n: int) -> int:
    """Publishable."""
    return await ctx.step(tick, n)


@pytest.fixture(params=["memory", "store-memory", "store-sqlite"])
async def catalog(request):
    """Every registry implementation, all in-process."""
    if request.param == "memory":
        yield InMemoryWorkflowRegistry()
    elif request.param == "store-memory":
        yield StoreBackedWorkflowRegistry(MemoryStore())
    else:
        store = SQLiteStore(":memory:")
        try:
            yield StoreBackedWorkflowRegistry(store)
        finally:
            await store.close()


class TestWorkflowCatalog:
    async def test_put_and_get(self, catalog) -> None:
        await catalog.put(WorkflowRecord(name="a", version="1", code_hash="h1"))

        found = await catalog.get("a")
        assert found is not None and found.code_hash == "h1"

    async def test_get_unknown_returns_none(self, catalog) -> None:
        assert await catalog.get("nope") is None

    async def test_versions_coexist(self, catalog) -> None:
        await catalog.put(WorkflowRecord(name="a", version="1"))
        await catalog.put(WorkflowRecord(name="a", version="2"))

        assert len(await catalog.list()) == 2
        assert (await catalog.get("a", "1")).version == "1"

    async def test_get_without_a_version_returns_the_newest(self, catalog) -> None:
        from datetime import UTC, datetime, timedelta

        old = datetime.now(UTC) - timedelta(days=1)
        await catalog.put(WorkflowRecord(name="a", version="1", published_at=old))
        await catalog.put(WorkflowRecord(name="a", version="2"))

        assert (await catalog.get("a")).version == "2"

    async def test_put_is_an_upsert(self, catalog) -> None:
        await catalog.put(WorkflowRecord(name="a", version="1", description="first"))
        await catalog.put(WorkflowRecord(name="a", version="1", description="second"))

        assert len(await catalog.list()) == 1
        assert (await catalog.get("a")).description == "second"

    async def test_delete_one_version(self, catalog) -> None:
        await catalog.put(WorkflowRecord(name="a", version="1"))
        await catalog.put(WorkflowRecord(name="a", version="2"))

        await catalog.delete("a", "1")

        assert [r.version for r in await catalog.list()] == ["2"]

    async def test_delete_every_version(self, catalog) -> None:
        await catalog.put(WorkflowRecord(name="a", version="1"))
        await catalog.put(WorkflowRecord(name="a", version="2"))

        await catalog.delete("a")

        assert await catalog.list() == []

    async def test_listing_is_stable(self, catalog) -> None:
        for name in ("c", "a", "b"):
            await catalog.put(WorkflowRecord(name=name))

        assert [r.name for r in await catalog.list()] == ["a", "b", "c"]


class TestRecordFor:
    def test_captures_identity_and_source(self) -> None:
        record = record_for(catalogued, published_by="node-1")

        assert record.name == "catalogued"
        assert record.description == "A workflow worth publishing"
        assert record.code_hash == catalogued.code_hash
        assert record.source_file.endswith("test_p2_hardening.py")
        assert record.published_by == "node-1"

    def test_captures_the_input_schema(self) -> None:
        assert record_for(catalogued).input_schema.get("type") == "integer"

    def test_captures_triggers(self) -> None:
        from loom.triggers.specs import OnEvent

        @workflow(name="triggered", triggers=[OnEvent(topic="orders")])
        async def triggered(ctx: Context, _n: int) -> int:
            return 1

        assert record_for(triggered).triggers == ["event:default:orders"]


class TestRuntimePublish:
    async def test_publish_then_read_back(self) -> None:
        rt = Runtime(store=MemoryStore())
        record = await rt.publish(catalogued)

        assert record.key == "catalogued@1"
        assert [r.name for r in await rt.published()] == ["catalogued"]

    async def test_importing_a_module_publishes_nothing(self) -> None:
        """Publishing is explicit — registration must not write to storage."""
        rt = Runtime(store=MemoryStore())
        rt.register(catalogued)

        assert await rt.published() == []

    async def test_the_catalog_outlives_the_runtime(self) -> None:
        store = MemoryStore()
        await Runtime(store=store).publish(catalogued)

        # A fresh process that never imported the workflow still sees it listed.
        fresh = Runtime(store=store)
        published = await fresh.published()

        assert [r.name for r in published] == ["catalogued"]
        assert "catalogued" not in fresh.workflows

    async def test_extra_metadata_is_stored(self) -> None:
        rt = Runtime(store=MemoryStore())
        await rt.publish(catalogued, team="payments", generated_by="coding-agent")

        record = await rt.catalog.get("catalogued")
        assert record.metadata == {"team": "payments", "generated_by": "coding-agent"}

    async def test_provenance_links_a_run_to_its_code(self) -> None:
        rt = Runtime(store=MemoryStore())
        await rt.publish(catalogued)
        result = await rt.run(catalogued, 1)

        record = await rt.provenance(result.run_id)
        assert record is not None
        assert record.code_hash == catalogued.code_hash

    async def test_provenance_is_none_when_the_code_was_never_published(self) -> None:
        rt = Runtime(store=MemoryStore())
        result = await rt.run(catalogued, 1)

        assert await rt.provenance(result.run_id) is None

    async def test_provenance_of_an_unknown_run(self) -> None:
        assert await Runtime(store=MemoryStore()).provenance("nope") is None


class TestPublishedOverHttp:
    async def test_published_but_unimported_workflows_are_listed(self) -> None:
        import httpx

        from loom.server import LoomClient
        from loom.server.app import create_app

        store = MemoryStore()
        await Runtime(store=store).publish(catalogued)

        # This process never imported `catalogued`.
        serving = Runtime(store=store)
        transport = httpx.ASGITransport(app=create_app(serving))
        client = LoomClient(
            http=httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        )

        listed = await client.workflows()
        entry = next(w for w in listed if w["name"] == "catalogued")

        # Listed, but honest about not being runnable here.
        assert entry["executable"] is False
        assert entry["code_hash"] == catalogued.code_hash

    async def test_imported_workflows_are_marked_executable(self) -> None:
        import httpx

        from loom.server import LoomClient
        from loom.server.app import create_app

        rt = Runtime(store=MemoryStore())
        rt.register(catalogued)
        await rt.publish(catalogued)

        transport = httpx.ASGITransport(app=create_app(rt))
        client = LoomClient(
            http=httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        )

        entry = next(w for w in await client.workflows() if w["name"] == "catalogued")
        assert entry["executable"] is True
        assert entry["source_file"].endswith("test_p2_hardening.py")


# ---------------------------------------------------------------------------
# Public API sanity
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_context_methods_are_documented(self) -> None:
        """A public ctx.* method with no docstring is a gap in the SDK's contract."""
        undocumented = [
            name
            for name, member in inspect.getmembers(Context)
            if not name.startswith("_")
            and callable(member)
            and not (member.__doc__ or "").strip()
        ]
        assert undocumented == []

    async def test_agent_result_shape_is_stable(self) -> None:
        """Backends construct these positionally in adapters; keep the field."""
        result = AgentResult(output="x", agent="a")
        assert result.output == "x"
        assert result.messages == []
