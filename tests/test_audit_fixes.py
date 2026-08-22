"""End-to-end coverage for the P0/P1 audit fixes.

Every test here drives the capability through the *public* surface — Runtime,
Context, ctx.agent — rather than calling the implementing module directly. The
audit found five modules that were fully unit-tested and never actually reached
by the engine; module-level tests cannot catch that, and these are the shape of
test that can.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loom import Context, ExecutionStatus, Runtime, step, workflow
from loom.agents.result import AgentResult
from loom.core.exceptions import ConfigurationError, SerializationError
from loom.stores.memory import MemoryStore

# ---------------------------------------------------------------------------
# Journal payloads: no silent data loss, blob offload, binary round-trip
# ---------------------------------------------------------------------------


class Unserializable:
    """Not a model, not a dataclass, no pydantic schema — genuinely unjournalable."""

    def __init__(self) -> None:
        self.sock = object()


class TestDurablePayloads:
    async def test_unserializable_step_output_fails_loudly(self) -> None:
        """It used to be replaced by a placeholder and replayed as if real."""

        @step
        async def bad() -> Unserializable:
            return Unserializable()

        @workflow
        async def wf(ctx: Context, _input: str) -> str:
            await ctx.step(bad)
            return "unreachable"

        result = await Runtime(store=MemoryStore()).run(wf, "go")

        assert result.status is ExecutionStatus.FAILED
        assert "cannot journal" in result.error.message

    async def test_bytes_round_trip_through_the_journal(self) -> None:
        payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256))

        @step
        async def render() -> bytes:
            return payload

        @workflow
        async def wf(ctx: Context, _input: str) -> int:
            blob = await ctx.step(render)
            assert blob == payload
            return len(blob)

        rt = Runtime(store=MemoryStore())
        result = await rt.run(wf, "go")
        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == len(payload)

        # And it survives replay, which is the part that actually matters.
        replayed = await rt.replay(result.run_id)
        assert replayed.status is ExecutionStatus.COMPLETED

    async def test_large_payloads_offload_to_blob_storage(self, tmp_path: Path) -> None:
        from loom.blobs.blob import BlobService, LocalBlobBackend

        big = "x" * (400 * 1024)

        @step
        async def produce() -> str:
            return big

        @workflow
        async def wf(ctx: Context, _input: str) -> int:
            return len(await ctx.step(produce))

        blobs = BlobService(LocalBlobBackend(tmp_path))
        store = MemoryStore()
        rt = Runtime(store=store, blobs=blobs)
        result = await rt.run(wf, "go")
        assert result.output == len(big)

        # The journal row holds a reference, not 400 KB of payload.
        entry = next(e for e in await store.load_journal(result.run_id) if e.name == "produce")
        assert set(entry.output) == {"__blob__"}
        assert entry.output["__blob__"].startswith("blob:")

        # Replay follows the reference back to the real value.
        assert (await rt.replay(result.run_id)).output == len(big)

    async def test_blob_reference_without_a_blob_service_is_an_error(
        self, tmp_path: Path
    ) -> None:
        """Losing the blob service must not silently hand back the marker dict."""
        from loom.blobs.blob import BlobService, LocalBlobBackend

        @step
        async def produce() -> str:
            return "y" * (400 * 1024)

        @workflow
        async def wf(ctx: Context, _input: str) -> int:
            return len(await ctx.step(produce))

        store = MemoryStore()
        first = await Runtime(
            store=store, blobs=BlobService(LocalBlobBackend(tmp_path))
        ).run(wf, "go")

        forgetful = Runtime(store=store)  # same journal, no blobs configured
        forgetful.register(wf)
        result = await forgetful.replay(first.run_id)
        assert result.status is ExecutionStatus.FAILED
        assert "blob service" in result.error.message

    async def test_step_inputs_still_degrade_rather_than_fail(self) -> None:
        """Inputs are recorded for humans, never replayed — losing one is not fatal."""

        @step
        async def takes_anything(value: object) -> str:
            return "ok"

        @workflow
        async def wf(ctx: Context, _input: str) -> str:
            return await ctx.step(takes_anything, Unserializable())

        result = await Runtime(store=MemoryStore()).run(wf, "go")
        assert result.status is ExecutionStatus.COMPLETED


class TestSerdeDirect:
    def test_encode_raises_for_unserializable(self) -> None:
        from loom.core.serde import encode

        with pytest.raises(SerializationError):
            encode(Unserializable())

    def test_page_round_trips(self) -> None:
        from loom.core.serde import decode, encode
        from loom.core.types import Page

        page = Page(items=["a", "b"], cursor="2", has_more=True, total=7)
        restored = decode(encode(page), Page[str])

        assert isinstance(restored, Page)
        assert restored.items == ["a", "b"]
        assert restored.cursor == "2"
        assert restored.has_more is True
        assert restored.total == 7


# ---------------------------------------------------------------------------
# Toolset registry: one store for discovery and execution
# ---------------------------------------------------------------------------


@step
async def search_tickets(query: str) -> list:
    """Search tickets."""
    return []


@step
async def delete_ticket(key: str) -> bool:
    """Delete a ticket permanently."""
    return True


class TestUnifiedRegistry:
    def test_globally_registered_toolset_is_callable_from_a_runtime(self) -> None:
        """The split-brain bug: discoverable in one store, callable from another."""
        from loom.agents.tool_registry import Toolset
        from loom.toolsets.registry import register_toolset

        register_toolset(Toolset.from_steps("tickets", [search_tickets]))

        rt = Runtime(store=MemoryStore())
        assert "tickets" in rt.toolsets.list_toolsets()
        assert [t.name for t in rt.toolsets.resolve_tools(["tickets"])] == ["search_tickets"]

    def test_runtime_registrations_stay_local(self) -> None:
        from loom.agents.tool_registry import Toolset

        first = Runtime(store=MemoryStore())
        first.toolsets.register(Toolset.from_steps("local", [search_tickets]))

        assert "local" in first.toolsets.list_toolsets()
        assert "local" not in Runtime(store=MemoryStore()).toolsets.list_toolsets()

    def test_coding_agent_search_sees_the_runtime_registry(self) -> None:
        from loom.agents.tool_registry import Toolset

        rt = Runtime(store=MemoryStore())
        rt.toolsets.register(Toolset.from_steps("tickets", [search_tickets]))

        # First, not only. Search reaches the built-in tier as well now, and
        # "tickets" is a word HubSpot's summary uses — so what this asserts is
        # that a Runtime's own registration is found and ranks ahead of a
        # shipped toolset that merely mentions the term. See
        # tests/test_toolset_discovery.py for the tier rules.
        cards = rt.toolsets.search("tickets")
        assert cards[0].toolset_id == "tickets"

    def test_effect_filter_can_withhold_destructive_tools(self) -> None:
        from loom.agents.tool_registry import Toolset
        from loom.toolsets.manifest import EffectClass

        rt = Runtime(store=MemoryStore())
        rt.toolsets.register(Toolset.from_steps("tickets", [search_tickets, delete_ticket]))

        read_only = rt.toolsets.resolve_tools(effects={EffectClass.READ})
        assert [t.name for t in read_only] == ["search_tickets"]

    def test_explicit_effect_beats_the_name_heuristic(self) -> None:
        from loom.agents.tool_registry import Toolset
        from loom.toolsets.manifest import EffectClass

        # "search" reads as READ, but a billed scrape is not free to retry.
        ts = Toolset.from_steps(
            "scraper",
            [search_tickets],
            effects={"search_tickets": EffectClass.WRITE},
        )
        op = ts.manifest.find_operation("search_tickets")
        assert op.effect is EffectClass.WRITE
        assert op.idempotent is False

    def test_two_toolsets_for_one_service_do_not_silently_collide(self) -> None:
        from loom.agents.tool_registry import Toolset, ToolsetRegistry
        from loom.toolsets.kinds import ToolsetKind

        registry = ToolsetRegistry()
        registry.register(
            Toolset.from_steps("jira", [search_tickets], provider="loom")
        )
        with pytest.raises(ConfigurationError, match="already registered"):
            registry.register(
                Toolset.from_steps(
                    "jira", [search_tickets], kind=ToolsetKind.MCP, provider="atlassian"
                )
            )

    def test_qualified_id_distinguishes_kinds(self) -> None:
        from loom.agents.tool_registry import Toolset, ToolsetRegistry
        from loom.toolsets.kinds import ToolsetKind

        registry = ToolsetRegistry()
        registry.register(Toolset.from_steps("jira", [search_tickets], provider="loom"))
        registry.register(
            Toolset.from_steps(
                "mcp.jira", [search_tickets], kind=ToolsetKind.MCP, provider="atlassian"
            )
        )

        described = registry.describe()
        assert "app:loom:jira" in described
        assert "mcp:atlassian:mcp.jira" in described

    def test_mcp_prefix_classifies(self) -> None:
        from loom.toolsets.kinds import ToolsetKind, classify_toolset

        assert classify_toolset("mcp.jira") is ToolsetKind.MCP
        assert classify_toolset("jira") is ToolsetKind.APP


# ---------------------------------------------------------------------------
# Agent identity and conversation continuity
# ---------------------------------------------------------------------------


class EchoBackend:
    """Records what it was handed, and returns a growing transcript."""

    supports_history = True

    def __init__(self) -> None:
        self.seen_history: list[list] = []
        self.seen_agent_ids: list[str] = []

    async def run(
        self, prompt, *, tools=None, history=None, agent_id="", max_turns=None
    ) -> AgentResult:
        from loom.agents.messages import assistant, user

        self.seen_history.append(list(history or []))
        self.seen_agent_ids.append(agent_id)
        turns = [*(history or []), user(prompt), assistant(f"re: {prompt}")]
        return AgentResult(output=f"re: {prompt}", agent=agent_id, messages=turns)


class ForgetfulBackend:
    supports_history = False

    async def run(
        self, prompt, *, tools=None, history=None, agent_id="", max_turns=None
    ) -> AgentResult:
        return AgentResult(output="ok", agent=agent_id)


class TestAgentContinuity:
    async def test_same_session_carries_history_across_calls(self) -> None:
        backend = EchoBackend()

        @workflow
        async def chat(ctx: Context, _input: str) -> str:
            await ctx.agent("first", session_id="ticket-7", agent_id="support")
            second = await ctx.agent("second", session_id="ticket-7", agent_id="support")
            return second.output

        result = await Runtime(store=MemoryStore(), agent_backend=backend).run(chat, "go")

        assert result.status is ExecutionStatus.COMPLETED
        # First call starts empty; second sees the first exchange.
        assert backend.seen_history[0] == []
        assert [m.text() for m in backend.seen_history[1]] == ["first", "re: first"]

    async def test_distinct_sessions_do_not_bleed(self) -> None:
        backend = EchoBackend()

        @workflow
        async def chat(ctx: Context, _input: str) -> str:
            await ctx.agent("alpha", session_id="a", agent_id="support")
            await ctx.agent("beta", session_id="b", agent_id="support")
            return "done"

        await Runtime(store=MemoryStore(), agent_backend=backend).run(chat, "go")
        assert backend.seen_history[1] == []

    async def test_two_agents_sharing_a_session_id_keep_separate_memory(self) -> None:
        backend = EchoBackend()

        @workflow
        async def chat(ctx: Context, _input: str) -> str:
            await ctx.agent("hello", session_id="shared", agent_id="researcher")
            await ctx.agent("hello", session_id="shared", agent_id="writer")
            return "done"

        await Runtime(store=MemoryStore(), agent_backend=backend).run(chat, "go")
        assert backend.seen_agent_ids == ["researcher", "writer"]
        assert backend.seen_history[1] == []

    async def test_no_session_id_means_no_memory(self) -> None:
        backend = EchoBackend()

        @workflow
        async def chat(ctx: Context, _input: str) -> str:
            await ctx.agent("first")
            await ctx.agent("second")
            return "done"

        await Runtime(store=MemoryStore(), agent_backend=backend).run(chat, "go")
        assert backend.seen_history == [[], []]

    async def test_session_with_a_backend_that_cannot_honour_it_raises(self) -> None:
        @workflow
        async def chat(ctx: Context, _input: str) -> str:
            return (await ctx.agent("hi", session_id="x")).output

        result = await Runtime(
            store=MemoryStore(), agent_backend=ForgetfulBackend()
        ).run(chat, "go")

        assert result.status is ExecutionStatus.FAILED
        assert "does not support conversation history" in result.error.message

    async def test_max_turns_reaches_the_backend(self) -> None:
        seen: list[int | None] = []

        class Recorder(EchoBackend):
            async def run(self, prompt, *, tools=None, history=None, agent_id="",
                          max_turns=None):
                seen.append(max_turns)
                return await super().run(
                    prompt, tools=tools, history=history, agent_id=agent_id
                )

        @workflow
        async def chat(ctx: Context, _input: str) -> str:
            return (await ctx.agent("hi", max_turns=3)).output

        await Runtime(store=MemoryStore(), agent_backend=Recorder()).run(chat, "go")
        assert seen == [3]


class TestAgentSession:
    async def test_session_replays_prior_turns(self) -> None:
        from loom.agents.agent import Agent, PersistenceClass
        from loom.agents.executor import AgentContext
        from loom.agents.messages import assistant, user

        class Executor:
            agent_id = "recorder"

            def __init__(self) -> None:
                self.histories: list[list] = []

            async def execute(self, input, *, tools=None, output_type=None,
                             settings=None, context=None):
                ctx = context or AgentContext()
                self.histories.append(list(ctx.history))
                return AgentResult(
                    output=f"re: {input}",
                    messages=[*ctx.history, user(str(input)), assistant(f"re: {input}")],
                )

        executor = Executor()
        agent = Agent(
            name="support",
            executor=executor,
            persistence=PersistenceClass.SESSION,
        )
        chat = agent.session(key="ticket-7")

        await chat("my order is late")
        await chat("what was the tracking number?")

        assert executor.histories[0] == []
        assert [m.text() for m in executor.histories[1]] == [
            "my order is late",
            "re: my order is late",
        ]
        assert len(await chat.history()) == 4

        await chat.reset()
        assert await chat.history() == []

    def test_ephemeral_agent_refuses_a_session(self) -> None:
        from loom.agents.agent import Agent

        with pytest.raises(ConfigurationError, match="EPHEMERAL"):
            Agent(name="oneshot").session(key="k")


# ---------------------------------------------------------------------------
# Saga compensation and rotation reach the engine
# ---------------------------------------------------------------------------


class TestSagaAndRotation:
    async def test_compensations_run_when_a_workflow_fails(self) -> None:
        unwound: list[str] = []

        @step
        async def reserve(item: str) -> str:
            return item

        @step
        async def boom() -> str:
            raise RuntimeError("nope")

        async def release(item: str) -> None:
            unwound.append(item)

        @workflow
        async def saga(ctx: Context, _input: str) -> str:
            await ctx.step(reserve, "seat-1")
            await ctx.compensate(release, "seat-1")
            await ctx.step(boom)
            return "done"

        result = await Runtime(store=MemoryStore()).run(saga, "go")
        assert result.status is ExecutionStatus.FAILED
        assert unwound == ["seat-1"]

    async def test_compensations_run_on_cancellation(self) -> None:
        unwound: list[str] = []

        @step
        async def reserve(item: str) -> str:
            return item

        async def release(item: str) -> None:
            unwound.append(item)

        @workflow(name="saga_cancel")
        async def saga_cancel(ctx: Context, _input: str) -> str:
            from loom.core.exceptions import WorkflowCancelled

            await ctx.step(reserve, "seat-2")
            await ctx.compensate(release, "seat-2")
            raise WorkflowCancelled("operator stopped it")

        result = await Runtime(store=MemoryStore()).run(saga_cancel, "go")
        assert result.status is ExecutionStatus.CANCELLED
        assert unwound == ["seat-2"]


# ---------------------------------------------------------------------------
# Coding agent configurability
# ---------------------------------------------------------------------------


class TestCodingAgentConfig:
    def test_instructions_can_be_replaced(self) -> None:
        from loom.agents.coding_agent import (
            DEFAULT_SYSTEM_PROMPT,
            WorkflowCodingAgent,
        )

        agent = WorkflowCodingAgent(model=object(), instructions="Only write haiku.")
        prompt = agent.build_system_prompt()

        assert prompt.startswith("Only write haiku.")
        assert DEFAULT_SYSTEM_PROMPT not in prompt

    def test_extra_instructions_append(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent

        agent = WorkflowCodingAgent(
            model=object(), extra_instructions="House rule: always use SQLiteStore."
        )
        assert agent.build_system_prompt().endswith("House rule: always use SQLiteStore.")

    def test_allowed_packages_appear_in_the_prompt(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent

        agent = WorkflowCodingAgent(model=object(), allowed_packages={"httpx", "pydantic"})
        prompt = agent.build_system_prompt()

        assert "httpx, pydantic" in prompt
        assert "Available packages" in prompt

    def test_allowed_packages_are_enforced_by_the_validator(self) -> None:
        from loom.agents.validator import CodeValidator

        code = (
            "import httpx\n"
            "import pandas\n"
            "from loom import workflow\n"
            "@workflow\n"
            "async def wf(ctx, x):\n"
            "    return x\n"
        )
        issues = CodeValidator(allowed_packages={"httpx"}).validate(code)
        messages = [i.message for i in issues if i.category == "imports"]

        assert any("Import of 'pandas'" in m for m in messages)
        assert not any("Import of 'httpx'" in m for m in messages)

    def test_no_allowlist_means_no_import_restriction(self) -> None:
        from loom.agents.validator import CodeValidator

        code = (
            "import anything_at_all\n"
            "from loom import workflow\n"
            "@workflow\n"
            "async def wf(ctx, x):\n"
            "    return x\n"
        )
        issues = CodeValidator().validate(code)
        assert not [i for i in issues if i.category == "imports"]
