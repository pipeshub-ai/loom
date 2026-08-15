"""A grant entry that matches nothing permits nothing — and looks identical.

``allows_operation`` never returns True for an unrecognized entry, so a typo
produces a workflow that reads as restricted to what it lists and is in fact
restricted to nothing. An operator reading ``grants=[...]`` cannot tell the two
apart from the outside; the failure arrives much later as an agent reporting
that it could not find a tool.
"""

from __future__ import annotations

from typing import Any

import pytest

from workflow_builder import Context, Runtime, workflow
from workflow_builder.agents.tool_registry import ToolsetRegistry
from workflow_builder.core.exceptions import ConfigurationError
from workflow_builder.security.grants import GrantSet
from workflow_builder.state.memory import MemoryStore
from workflow_builder.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest


def registry() -> ToolsetRegistry:
    """A registry holding one toolset with one group."""
    reg = ToolsetRegistry()
    reg.register(
        ToolsetManifest(
            id="jira",
            name="Jira",
            version="1",
            summary="Issue tracking",
            description="Issue tracking",
            groups={
                "issues": [
                    OperationSpec(
                        id="issues.search",
                        name="search",
                        summary="Search issues",
                        description="Search issues",
                        effect=EffectClass.READ,
                    )
                ]
            },
        )
    )
    reg.register(
        ToolsetManifest(
            id="slack",
            name="Slack",
            version="1",
            summary="Chat",
            description="Chat",
            groups={
                "chat": [
                    OperationSpec(
                        id="chat.post",
                        name="post",
                        summary="Post a message",
                        description="Post a message",
                        effect=EffectClass.WRITE,
                    )
                ]
            },
        )
    )
    return reg


class TestValidation:
    def test_a_correct_entry_has_nothing_to_report(self) -> None:
        assert GrantSet(toolsets=["jira.issues:write"]).validate_against(
            toolsets=registry()
        ) == []

    def test_a_bare_toolset_is_valid(self) -> None:
        assert GrantSet(toolsets=["jira"]).validate_against(toolsets=registry()) == []

    def test_an_unknown_toolset_is_reported_with_the_near_match(self) -> None:
        issues = GrantSet(toolsets=["jirra"]).validate_against(toolsets=registry())
        assert len(issues) == 1
        assert "jirra" in str(issues[0])
        assert "jira" in issues[0].suggestions

    def test_a_misspelled_effect_is_reported(self) -> None:
        """The case from the plan: ``jira.issues:writ`` permits nothing."""
        issues = GrantSet(toolsets=["jira.issues:writ"]).validate_against(
            toolsets=registry()
        )
        assert len(issues) == 1
        assert "unknown effect" in issues[0].reason
        assert "write" in issues[0].suggestions

    def test_an_unknown_group_is_reported(self) -> None:
        issues = GrantSet(toolsets=["jira.nope"]).validate_against(toolsets=registry())
        assert len(issues) == 1
        assert "no group" in issues[0].reason
        assert "jira.issues" in issues[0].suggestions

    def test_agents_are_checked_when_the_caller_knows_them(self) -> None:
        issues = GrantSet(agents=["triage", "ghost"]).validate_against(
            toolsets=registry(), agents={"triage"}
        )
        assert [i.entry for i in issues] == ["ghost"]

    def test_an_empty_registry_checks_nothing(self) -> None:
        """Toolsets load lazily; empty now says nothing about the entries.

        Flagging everything here would fail every workflow whose grants are
        declared before its integrations register.
        """
        assert GrantSet(toolsets=["jira", "anything"]).validate_against(
            toolsets=ToolsetRegistry()
        ) == []

    def test_validation_reads_manifests_only(self) -> None:
        """Layer 1 stays Layer 1 — checking a string imports no toolset code."""
        reg = registry()
        resolved: list[str] = []
        reg.resolve_tools = lambda *a, **k: resolved.append("resolved")  # type: ignore[assignment]

        GrantSet(toolsets=["jira.issues:write", "jirra"]).validate_against(toolsets=reg)

        assert resolved == []


class TestRegistrationRefuses:
    def test_a_typo_fails_at_registration(self) -> None:
        @workflow(name="typo_flow", grants=GrantSet(toolsets=["jira.issues:writ"]))
        async def flow(ctx: Context, _: object = None) -> str:
            return "x"

        rt = Runtime(store=MemoryStore(), toolsets=registry())
        with pytest.raises(ConfigurationError) as caught:
            rt.register(flow)

        message = str(caught.value)
        assert "typo_flow" in message
        assert "unknown effect" in message
        assert "permits nothing" in message

    def test_a_correct_grant_registers(self) -> None:
        @workflow(name="ok_flow", grants=GrantSet(toolsets=["jira.issues:read"]))
        async def flow(ctx: Context, _: object = None) -> str:
            return "x"

        rt = Runtime(store=MemoryStore(), toolsets=registry())
        assert rt.register(flow) is flow

    def test_a_workflow_without_grants_is_unaffected(self) -> None:
        @workflow(name="plain_flow")
        async def flow(ctx: Context, _: object = None) -> str:
            return "x"

        rt = Runtime(store=MemoryStore(), toolsets=registry())
        assert rt.register(flow) is flow

    def test_registration_uses_this_runtime_s_registry(self) -> None:
        """Which is why the check cannot happen at decoration time.

        ``rt.toolsets`` chains to the process-global registry, so whether an
        entry names something real is a per-Runtime answer.
        """

        @workflow(name="scoped_flow", grants=GrantSet(toolsets=["jira"]))
        async def flow(ctx: Context, _: object = None) -> str:
            return "x"

        Runtime(store=MemoryStore(), toolsets=registry()).register(flow)
        Runtime(store=MemoryStore(), toolsets=ToolsetRegistry()).register(flow)


class TestTheCodingAgentStage:
    @pytest.mark.asyncio
    async def test_it_flags_a_typo_in_generated_code(self) -> None:
        from workflow_builder.agents.checks import CheckContext
        from workflow_builder.agents.stages import GrantStage

        code = (
            "from workflow_builder import workflow\n"
            "from workflow_builder.security.grants import GrantSet\n"
            '@workflow(name="f", grants=GrantSet(toolsets=["jira.issues:writ"]))\n'
            "async def f(ctx):\n    return 'x'\n"
        )
        result = await GrantStage(registry()).run(code, CheckContext())

        assert len(result.issues) == 1
        assert "unknown effect" in result.issues[0].message
        assert result.issues[0].severity == "warning"

    @pytest.mark.asyncio
    async def test_it_skips_rather_than_passing_without_a_registry(self) -> None:
        """A check that cannot run has found nothing, which is not passing."""
        from workflow_builder.agents.checks import CheckContext
        from workflow_builder.agents.stages import GrantStage

        result = await GrantStage(None).run("x = 1", CheckContext())
        assert result.skipped
        assert result.issues == []

    @pytest.mark.asyncio
    async def test_unparsable_code_yields_nothing(self) -> None:
        """CompileStage owns syntax; this one must not double-report it."""
        from workflow_builder.agents.checks import CheckContext
        from workflow_builder.agents.stages import GrantStage

        result = await GrantStage(registry()).run("def (", CheckContext())
        assert result.issues == []

    def test_it_is_in_the_default_pipeline(self) -> None:
        from workflow_builder.agents.stages import default_stages

        names = [s.name for s in default_stages(smoke=False, registry=registry())]
        assert "grants" in names
        # Cheap and non-blocking: it runs before the expensive stages and does
        # not stop them, because the code is otherwise correct.
        assert names.index("grants") < names.index("lint")


class TestOnlyJournaledCallsAreGuarded:
    """Why the prompt tells generated code to call toolset tools via ctx.step.

    A toolset tool is a ``@step``, so it can also be awaited directly inside
    another ``@step`` — ``StepDefinition.__call__`` documents itself as
    "bypassing the journal". What that also bypasses is
    :meth:`DurableCall._resolve`, which is where ``broker.dispatch`` runs. So a
    direct call is not merely less granular on replay: it is never weighed
    against the workflow's ``GrantSet`` at all.

    Declaring grants and then calling around them is the failure this pins.
    """

    @pytest.mark.asyncio
    async def test_a_ctx_step_call_reaches_the_broker(self) -> None:
        from workflow_builder import step
        from workflow_builder.runtime.effects import DirectBroker

        seen: list[str] = []

        class Watching(DirectBroker):
            async def dispatch(self, call, authority):  # type: ignore[override]
                seen.append(call.target)
                return await super().dispatch(call, authority)

        @step
        async def fake_toolset_op(query: str) -> str:
            return "rows"

        @workflow(name="guarded_flow")
        async def flow(ctx: Context, _: object = None) -> str:
            return await ctx.step(fake_toolset_op, "q")

        rt = Runtime(store=MemoryStore(), broker=Watching())
        rt.register(flow)
        await rt.run(flow)

        assert seen == ["fake_toolset_op"]

    @pytest.mark.asyncio
    async def test_a_direct_call_inside_a_step_does_not(self) -> None:
        from workflow_builder import step
        from workflow_builder.runtime.effects import DirectBroker

        seen: list[str] = []

        class Watching(DirectBroker):
            async def dispatch(self, call, authority):  # type: ignore[override]
                seen.append(call.target)
                return await super().dispatch(call, authority)

        @step
        async def fake_toolset_op(query: str) -> str:
            return "rows"

        @step
        async def wrapper(query: str) -> str:
            # The shape the old prompt asked for.
            return await fake_toolset_op(query)

        @workflow(name="unguarded_flow")
        async def flow(ctx: Context, _: object = None) -> str:
            return await ctx.step(wrapper, "q")

        rt = Runtime(store=MemoryStore(), broker=Watching())
        rt.register(flow)
        await rt.run(flow)

        # Only the wrapper was weighed. The toolset call inside it was not.
        assert seen == ["wrapper"]
        assert "fake_toolset_op" not in seen


class TestThePromptTeachesTheGuardedForm:
    """The rule the two tests above exist to justify.

    Pinned like every other load-bearing phrase in the prompt, because it was
    once stated backwards: "call them directly inside a @step function, not via
    ctx.step()", while two code samples in the same prompt used ctx.step.
    """

    def test_it_asks_for_ctx_step(self) -> None:
        from workflow_builder.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        collapsed = " ".join(DEFAULT_SYSTEM_PROMPT.split())
        assert "Toolset tools ARE steps" in collapsed
        assert "call them with ctx.step(tool, ...)" in collapsed

    def test_it_says_what_a_direct_call_costs(self) -> None:
        from workflow_builder.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        collapsed = " ".join(DEFAULT_SYSTEM_PROMPT.split())
        assert "skips the journal" in collapsed
        assert "grant check" in collapsed

    def test_the_old_backwards_rule_is_gone(self) -> None:
        from workflow_builder.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert "not via ctx.step()" not in DEFAULT_SYSTEM_PROMPT

    def test_the_samples_agree_with_the_rule(self) -> None:
        """The contradiction was only visible by reading both at once."""
        from workflow_builder.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert "await ctx.step(<search operation>" in DEFAULT_SYSTEM_PROMPT


class TestPerCallNarrowing:
    """``ctx.agent(grants=…)`` narrows one call, and can only narrow.

    The workflow's declaration is the ceiling. A call that asks for something
    outside it gets nothing extra — which is what makes the parameter safe to
    put in front of generated code, since the worst a wrong value can do is
    take capability away.
    """

    def test_a_request_narrows_the_declaration(self) -> None:
        declared = GrantSet(toolsets=["jira", "slack"])
        assert declared.intersect(GrantSet(toolsets=["jira:read"])).toolsets == [
            "jira:read"
        ]

    def test_a_request_cannot_widen_it(self) -> None:
        declared = GrantSet(toolsets=["jira"])
        assert declared.intersect(GrantSet(toolsets=["github"])).toolsets == []

    @pytest.mark.asyncio
    async def test_the_effective_grant_is_the_intersection(self) -> None:

        seen: list[Any] = []

        @workflow(name="narrowing_flow", grants=GrantSet(toolsets=["jira", "slack"]))
        async def flow(ctx: Context, _: object = None) -> str:
            seen.append(ctx._effective_grant(None))
            seen.append(ctx._effective_grant(GrantSet(toolsets=["jira:read"])))
            return "x"

        rt = Runtime(store=MemoryStore(), toolsets=registry())
        rt.register(flow)
        await rt.run(flow)

        assert seen[0].toolsets == ["jira", "slack"]
        assert seen[1].toolsets == ["jira:read"]

    @pytest.mark.asyncio
    async def test_narrowing_reaches_the_broker_not_just_the_tool_list(self) -> None:
        """Filtering the tools an agent is handed is not enforcement.

        Tools are resolved once and held for the whole turn loop, so the check
        that matters is the one the broker makes per dispatch — which weighs
        the authority the call was given, not the workflow's declaration.
        """

        @workflow(name="authority_flow", grants=GrantSet(toolsets=["jira", "slack"]))
        async def flow(ctx: Context, _: object = None) -> str:
            wide = ctx._authority_with(ctx._effective_grant(None))
            narrow = ctx._authority_with(
                ctx._effective_grant(GrantSet(toolsets=["jira:read"]))
            )
            assert wide.grant.toolsets == ["jira", "slack"]
            assert narrow.grant.toolsets == ["jira:read"]
            # Narrowing one call leaves the context's own authority alone, so a
            # concurrent call under gather cannot observe it.
            assert ctx._authority.grant.toolsets == ["jira", "slack"]
            return "x"

        rt = Runtime(store=MemoryStore(), toolsets=registry())
        rt.register(flow)
        result = await rt.run(flow)
        assert result.output == "x"

    @pytest.mark.asyncio
    async def test_an_inherited_override_is_the_new_ceiling(self) -> None:
        """A narrowed grant, once in force, is what further calls narrow from."""

        @workflow(name="inherit_flow", grants=GrantSet(toolsets=["jira", "slack"]))
        async def flow(ctx: Context, _: object = None) -> str:
            ctx._grant_override = GrantSet(toolsets=["jira"])
            # slack is gone from the ceiling, so asking for it yields nothing.
            assert ctx._effective_grant(GrantSet(toolsets=["slack"])).toolsets == []
            assert ctx._effective_grant(GrantSet(toolsets=["jira"])).toolsets == ["jira"]
            return "x"

        rt = Runtime(store=MemoryStore(), toolsets=registry())
        rt.register(flow)
        await rt.run(flow)
