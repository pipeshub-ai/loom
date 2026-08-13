"""Resolving what a spec refers to, before code depends on it.

The failure this addresses returns zero rows and no error: a query filters on
the words a person used — a display name, a status they assume exists — while
the API matches identifiers and its own configured vocabulary. Nothing joins
the two, and "no results" reads as "nothing to do".
"""

from __future__ import annotations

import json

import pytest

from workflow_builder.agents.coding_tools import call_read_operation
from workflow_builder.agents.tool_registry import ToolsetRegistry
from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST
from workflow_builder.toolsets.manifest import (
    OperationSpec,
    ToolsetManifest,
)


@pytest.fixture(autouse=True)
def _jira_registered():
    """The global catalog is reset between tests; these need Jira in it."""
    from workflow_builder.toolsets.registry import get_catalog, register_toolset

    register_toolset(JIRA_MANIFEST)
    yield
    get_catalog().unregister(JIRA_MANIFEST.id)


def call(*args, **kwargs):
    """The tool's underlying coroutine, however it is wrapped."""
    fn = getattr(call_read_operation, "fn", call_read_operation)
    return fn(*args, **kwargs)


class TestResolversAreDeclarative:
    """The marker is generic, so the rule works for any toolset."""

    def test_a_manifest_reports_its_resolvers(self) -> None:
        assert JIRA_MANIFEST.resolvers()["user"].function == "jira_resolve_user"

    def test_a_toolset_with_none_reports_none(self) -> None:
        from workflow_builder.toolsets.google import GMAIL_MANIFEST

        assert GMAIL_MANIFEST.resolvers() == {}

    def test_any_toolset_can_declare_one(self) -> None:
        """Nothing here is Jira-specific."""
        manifest = ToolsetManifest(
            id="crm",
            version="1.0.0",
            summary="A CRM",
            tools_module="m",
            groups={
                "contacts": [
                    OperationSpec(
                        id="contacts.find",
                        function="crm_find_contact",
                        summary="Find a contact by name.",
                        resolves="contact",
                    )
                ]
            },
        )
        assert manifest.resolvers()["contact"].function == "crm_find_contact"

    def test_the_docs_tell_the_agent_to_resolve_first(self) -> None:
        registry = ToolsetRegistry()
        registry.register(JIRA_MANIFEST)
        docs = registry.describe()

        assert "Resolve a user with jira_resolve_user" in docs
        assert "matches ids, not display names" in docs


class TestAuthoringMayOnlyRead:
    """Authoring must not change the system it is writing code about."""

    @pytest.mark.parametrize(
        "op_path", ["jira.issues.create", "jira.issues.update", "jira.issues.assign"]
    )
    async def test_writes_are_refused(self, op_path: str) -> None:
        result = json.loads(await call(op_path, "{}"))
        assert "authoring may only read" in result["error"]

    async def test_destructive_is_refused(self) -> None:
        result = json.loads(await call("jira.issues.delete", '{"issue_key": "X-1"}'))
        assert "destructive" in result["error"]

    async def test_an_unknown_toolset_lists_the_real_ones(self) -> None:
        result = json.loads(await call("nope.things.get", "{}"))
        assert "available" in result

    async def test_an_unknown_operation_lists_the_real_ones(self) -> None:
        result = json.loads(await call("jira.issues.nope", "{}"))
        assert "issues.search" in result["available"]

    async def test_a_malformed_path_says_the_shape(self) -> None:
        result = json.loads(await call("jira", "{}"))
        assert "<toolset>.<operation>" in result["error"]

    async def test_both_argument_shapes_are_accepted(self) -> None:
        """A model emits an object; sometimes it sends the JSON as a string.

        Rejecting either turns one mistake into a retry loop that burns the
        whole turn budget without executing anything — observed: 8 attempts,
        1 execution, then the run died.
        """
        as_object = json.loads(await call("jira.users.resolve", {"name": "x"}))
        as_string = json.loads(await call("jira.users.resolve", '{"name": "x"}'))
        assert "error" not in as_object or "credential" in str(as_object).lower()
        assert as_object.keys() == as_string.keys()

    async def test_omitted_arguments_mean_none(self) -> None:
        result = json.loads(await call("jira.projects.list"))
        assert "arguments must be" not in str(result)

    async def test_malformed_json_string_is_reported(self) -> None:
        result = json.loads(await call("jira.users.resolve", "not json"))
        assert "not valid JSON" in result["error"]

    async def test_a_non_object_is_reported(self) -> None:
        result = json.loads(await call("jira.users.resolve", "[1, 2]"))
        assert "must be an object" in result["error"]

    async def test_a_credentials_failure_is_explained_not_blamed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing credentials while authoring is normal, not a broken operation."""
        monkeypatch.delenv("JIRA_URL", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

        import workflow_builder.toolsets.jira.client as client_module

        monkeypatch.setattr(client_module, "_default_client", None)

        result = json.loads(await call("jira.users.resolve", '{"name": "x"}'))
        assert "error" in result
        assert "resolves it at runtime" in result["note"]


def prompt_text() -> str:
    """The system prompt with whitespace collapsed.

    Assertions on wrapped prose otherwise break the moment a line is rewrapped,
    which says nothing about whether the instruction is still there.
    """
    from workflow_builder.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

    return " ".join(DEFAULT_SYSTEM_PROMPT.split())


class TestThePromptStaysGeneric:
    """No toolset or person should be named in instructions every spec pays for."""

    def test_it_names_no_specific_toolset_or_person(self) -> None:
        from workflow_builder.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        for specific in ("jira", "Jira", "slack", "Slack", "github", "Vishwjeet"):
            assert specific not in DEFAULT_SYSTEM_PROMPT, specific


class TestThePromptTeachesTheLadder:
    def test_it_names_each_rung(self) -> None:
        prompt = prompt_text()
        for phrase in (
            "zero rows and no error",
            "call_read_operation",
            "per-account configuration",
            "Nothing the spec names may reach a query as raw text",
            "Never fall back to the raw string",
        ):
            assert phrase in prompt, phrase

    def test_ambiguity_routes_to_an_agent_node(self) -> None:
        """The rule: a leftover ambiguity is resolved by a model at run time,
        not by a direct tool call the generator had to guess the arguments for."""
        prompt = prompt_text()
        assert "Ambiguous after those two lookups" in prompt
        assert "At most two lookups per entity" in prompt, (
            "without a stopping rule the agent searches until its budget dies"
        )
        assert "do not call the toolset operation directly for it" in prompt
        assert "ctx.agent()` step that resolves it at run time" in prompt

    def test_it_shows_the_agent_node_form(self) -> None:
        """A rule the model has to invent a shape for is a rule half given."""
        prompt = prompt_text()
        assert "resolved = await ctx.agent(" in prompt
        assert "toolsets=[" in prompt

    def test_it_still_keeps_settled_lookups_out_of_every_run(self) -> None:
        """An entity with one clear answer is baked in, not re-resolved."""
        prompt = prompt_text()
        assert "One clear answer" in prompt
        assert "does no lookup at run time" in prompt
        assert "re-answers it, differently, on every run" in prompt

    def test_the_tool_is_offered_to_the_agent(self) -> None:
        from workflow_builder.agents.coding_tools import build_coding_tools

        names = {t.name for t in build_coding_tools()}
        assert "call_read_operation" in names

        scoped = {t.name for t in build_coding_tools(registry=ToolsetRegistry())}
        assert "call_read_operation" in scoped


class TestToolsetsLoadOnDemand:
    """The prompt must grow with the task, not with what is installed.

    Pasting every operation of every registered integration into the system
    prompt costs thousands of tokens before the model has read the spec, and
    each new integration taxes every unrelated generation.
    """

    def _registry(self) -> ToolsetRegistry:
        from workflow_builder.toolsets.confluence.manifest import CONFLUENCE_MANIFEST
        from workflow_builder.toolsets.google import (
            GMAIL_MANIFEST,
            GOOGLE_CALENDAR_MANIFEST,
        )

        registry = ToolsetRegistry()
        for manifest in (
            JIRA_MANIFEST,
            CONFLUENCE_MANIFEST,
            GMAIL_MANIFEST,
            GOOGLE_CALENDAR_MANIFEST,
        ):
            registry.register(manifest)
        return registry

    def test_the_index_is_substantially_smaller(self) -> None:
        registry = self._registry()
        index, full = registry.describe(detail="index"), registry.describe()
        assert len(index) < len(full) * 0.6

    def test_the_index_cost_does_not_grow_with_operations(self) -> None:
        """The property that matters: a richer toolset must not tax the prompt.

        A ratio alone would pass while the index still carried per-operation
        detail; this compares a small toolset against a large one.
        """
        from workflow_builder.toolsets.manifest import OperationSpec, ToolsetManifest

        def manifest(op_count: int) -> ToolsetManifest:
            return ToolsetManifest(
                id=f"t{op_count}",
                version="1.0.0",
                summary="A toolset",
                groups={
                    "g": [
                        OperationSpec(
                            id=f"g.op{i}",
                            function=f"fn{i}",
                            summary="Does a thing with several parameters.",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    f"p{j}": {"type": "string"} for j in range(8)
                                },
                            },
                        )
                        for i in range(op_count)
                    ]
                },
            )

        small, large = ToolsetRegistry(), ToolsetRegistry()
        small.register(manifest(2))
        large.register(manifest(20))

        index_growth = len(large.describe(detail="index")) / len(
            small.describe(detail="index")
        )
        full_growth = len(large.describe()) / len(small.describe())

        # Names still scale, but nothing like signatures and schemas do.
        assert index_growth < full_growth / 2

    def test_the_index_still_names_every_toolset(self) -> None:
        """Cheaper, not blind: the agent must know what exists to search it."""
        index = self._registry().describe(detail="index")
        for toolset_id in ("jira", "confluence", "gmail", "google_calendar"):
            assert toolset_id in index

    def test_the_index_omits_operation_signatures(self) -> None:
        index = self._registry().describe(detail="index")
        assert "jira_search_issues(" not in index
        assert "gmail_send_message(" not in index

    def test_the_index_keeps_the_import_line(self) -> None:
        """The one line that stops an import being invented."""
        index = self._registry().describe(detail="index")
        assert "from workflow_builder.toolsets.jira.tools import" in index

    def test_the_index_says_how_to_get_the_detail(self) -> None:
        index = self._registry().describe(detail="index")
        assert 'show_toolset("jira")' in index
        assert 'get_tool_docs("jira")' in index

    def test_the_system_prompt_uses_the_index(self) -> None:
        from workflow_builder.agents.coding_agent import WorkflowCodingAgent

        prompt = WorkflowCodingAgent(
            object(), tool_registry=self._registry()
        ).build_system_prompt()

        assert "jira" in prompt
        assert "jira_search_issues(" not in prompt, "operations leaked into the prompt"

    def test_full_detail_is_still_available_on_request(self) -> None:
        """show_toolset and get_tool_docs still need the real thing."""
        full = self._registry().describe()
        assert "jira_search_issues(" in full


class TestUnregisteredToolsetsAreRefused:
    """Inventing an integration reads like completing the task.

    A spec needing Slack, generated where there is no Slack toolset, otherwise
    produces confident code that fails on its first line — at whatever hour it
    eventually runs.
    """

    def test_an_unavailable_toolset_is_an_error(self) -> None:
        from workflow_builder.agents.validator import CodeValidator

        issues = CodeValidator(available_toolsets={"jira"}).validate(
            "from workflow_builder.toolsets.slack.tools import slack_post\n"
        )
        toolset_issues = [i for i in issues if i.category == "toolset"]

        assert toolset_issues
        assert toolset_issues[0].severity == "error"
        assert "no 'slack' toolset" in toolset_issues[0].message
        assert "jira" in toolset_issues[0].message, "must name what IS available"

    def test_an_available_toolset_passes(self) -> None:
        from workflow_builder.agents.validator import CodeValidator

        issues = CodeValidator(available_toolsets={"jira"}).validate(
            "from workflow_builder.toolsets.jira.tools import jira_get_issue\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_google_toolsets_are_matched_at_the_right_depth(self) -> None:
        """Their modules nest one deeper than the others."""
        from workflow_builder.agents.validator import CodeValidator

        issues = CodeValidator(available_toolsets={"gmail"}).validate(
            "from workflow_builder.toolsets.google.gmail.tools import gmail_get_message\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_each_missing_toolset_is_reported_once(self) -> None:
        from workflow_builder.agents.validator import CodeValidator

        issues = CodeValidator(available_toolsets=set()).validate(
            "from workflow_builder.toolsets.slack.tools import a\n"
            "from workflow_builder.toolsets.slack.client import b\n"
        )
        assert len([i for i in issues if i.category == "toolset"]) == 1

    def test_the_check_is_off_by_default(self) -> None:
        """A caller who never said what exists should not be second-guessed."""
        from workflow_builder.agents.validator import CodeValidator

        issues = CodeValidator().validate(
            "from workflow_builder.toolsets.slack.tools import slack_post\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_the_agent_wires_the_registry_into_the_validator(self) -> None:
        from workflow_builder.agents.coding_agent import WorkflowCodingAgent

        registry = ToolsetRegistry()
        registry.register(JIRA_MANIFEST)
        agent = WorkflowCodingAgent(object(), tool_registry=registry)

        assert agent._validator.available_toolsets == {"jira"}

    def test_the_prompt_says_to_refuse(self) -> None:
        prompt = prompt_text()
        assert "do not write code against it" in prompt
        assert "Only the toolsets listed above exist" in prompt


class TestARefusalIsLegible:
    """No code is an answer, not a malfunction.

    A task needing an integration this environment lacks should say so. Running
    the ordinary validators over an empty string instead reports "no @workflow
    found" and "missing import" — symptoms of emptiness that bury the reason.
    """

    async def test_an_empty_result_reports_the_reason(self) -> None:
        from workflow_builder.agents.coding_agent import (
            CodingOutput,
            WorkflowCodingAgent,
        )

        class FakeAgentResult:
            output = CodingOutput(
                code="",
                explanation="This needs a Slack toolset, which is not configured here.",
            )
            usage = type("U", (), {"input_tokens": 10, "output_tokens": 2})()

        class FakeModel:
            model_name = "fake"

        agent = WorkflowCodingAgent(FakeModel(), smoke_test=False)

        import workflow_builder.agents.agent as agent_module

        class FakeAgent:
            def __init__(self, *a, **k) -> None: ...
            async def __call__(self, *a, **k):
                return FakeAgentResult()

        original = agent_module.Agent
        agent_module.Agent = FakeAgent
        try:
            result = await agent.generate("post to slack")
        finally:
            agent_module.Agent = original

        assert result.code == ""
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert issue.category == "unsupported"
        assert "Slack toolset" in issue.message
        assert not result.is_clean

        # The symptoms of emptiness must not drown the reason.
        messages = " ".join(i.message for i in result.issues)
        assert "No @workflow" not in messages


class TestMarkdownByDefault:
    """A workflow's result is usually read by a person, not indexed into.

    Handing back ``{'count': 3, 'issues': [...]}`` makes the reader do the
    formatting the workflow was in a position to do, with everything it knew.
    """

    def test_the_prompt_asks_for_markdown_by_default(self) -> None:
        prompt = prompt_text()
        assert "markdown string, annotated `-> str`" in prompt
        assert "unless the spec asks otherwise" in prompt

    def test_it_still_allows_structured_output_on_request(self) -> None:
        """An explicit ask must win — some outputs feed a system, not a person."""
        prompt = prompt_text()
        assert "Return structured data when the spec asks for it" in prompt
        assert "output JSON" in prompt

    def test_it_says_what_makes_the_markdown_useful(self) -> None:
        """Formatting rules, not decoration: the parts a reader acts on."""
        prompt = prompt_text()
        for rule in (
            "Lead with the answer",
            "When the result is empty, say what was searched",
            "Keep identifiers verbatim",
        ):
            assert rule in prompt, rule

    def test_the_prompt_stays_lean(self) -> None:
        """Every line is paid on every turn of every generation."""
        from workflow_builder.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert len(DEFAULT_SYSTEM_PROMPT) < 8000, "the prompt is drifting long"


class TestRepeatedLookupsAreStopped:
    """A repeated identical lookup cannot produce a different answer.

    Observed: 14 calls to the same operation in one generation, the model
    varying only the argument encoding — {} then null then "{}" then omitted —
    until the turn budget died. The encodings differ; the call does not, so the
    signature is taken after normalising.
    """

    async def test_the_third_identical_call_is_refused(self) -> None:
        from workflow_builder.agents.coding_tools import _call_read_operation

        seen: dict[str, int] = {}
        results = [
            json.loads(
                await _call_read_operation(
                    "jira.projects.list", {}, registry=None, seen=seen
                )
            )
            for _ in range(4)
        ]

        assert "already called" not in str(results[0])
        assert "already called" not in str(results[1])
        assert "already called" in results[2]["error"]
        assert "already called" in results[3]["error"]

    async def test_every_encoding_counts_as_the_same_call(self) -> None:
        """The encodings the model actually cycled through."""
        from workflow_builder.agents.coding_tools import _call_read_operation

        seen: dict[str, int] = {}
        for arguments in ({}, None, "{}", ""):
            await _call_read_operation(
                "jira.projects.list", arguments, registry=None, seen=seen
            )

        assert len(seen) == 1, f"encodings were counted separately: {seen}"
        assert sum(seen.values()) == 4

    async def test_the_refusal_says_what_to_do_instead(self) -> None:
        """Stopping a loop is only useful if it also names the way forward."""
        from workflow_builder.agents.coding_tools import _call_read_operation

        seen: dict[str, int] = {}
        for _ in range(3):
            out = await _call_read_operation(
                "jira.projects.list", {}, registry=None, seen=seen
            )

        message = json.loads(out)["error"]
        assert "ctx.agent()" in message
        assert "return the workflow" in message

    async def test_different_calls_are_counted_apart(self) -> None:
        from workflow_builder.agents.coding_tools import _call_read_operation

        seen: dict[str, int] = {}
        for _ in range(3):
            await _call_read_operation(
                "jira.users.resolve", {"name": "a"}, registry=None, seen=seen
            )
        out = await _call_read_operation(
            "jira.users.resolve", {"name": "b"}, registry=None, seen=seen
        )

        assert "already called" not in str(out), "a different query was blocked"

    async def test_counting_is_off_when_no_ledger_is_passed(self) -> None:
        """The module-level tool is stateless; only a bound one counts."""
        from workflow_builder.agents.coding_tools import _call_read_operation

        for _ in range(5):
            out = await _call_read_operation(
                "jira.projects.list", {}, registry=None, seen=None
            )
        assert "already called" not in str(out)


class TestBoundAndUnboundToolsAgree:
    """A bound tool must accept what its advertised schema promises.

    The failure this prevents was silent and total: the bound variant kept an
    older signature, so every entity lookup died with a TypeError the model
    could not see or fix. It retried until the turn budget was gone, and the
    workflow was written against entities that were never resolved. The schema
    the model reads comes from the unbound tool, so a divergence is invisible
    until something calls it.
    """

    def _by_name(self, **kwargs):
        from workflow_builder.agents.coding_tools import build_coding_tools

        return {tool.name: tool for tool in build_coding_tools(**kwargs)}

    def test_every_tool_keeps_its_schema_when_bound(self) -> None:
        from workflow_builder.agents.validator import CodeValidator

        unbound = self._by_name()
        bound = self._by_name(
            registry=ToolsetRegistry(), validator=CodeValidator()
        )

        assert set(unbound) == set(bound)
        for name, tool in unbound.items():
            assert tool.parameters == bound[name].parameters, name

    def test_every_bound_tool_accepts_its_own_schema(self) -> None:
        """The schema is a promise about the call signature; check it holds."""
        import inspect

        from workflow_builder.agents.validator import CodeValidator

        bound = self._by_name(registry=ToolsetRegistry(), validator=CodeValidator())
        for name, tool in bound.items():
            advertised = set(tool.parameters.get("properties", {}))
            accepted = set(inspect.signature(tool.fn).parameters)
            missing = advertised - accepted
            assert not missing, f"{name} advertises {missing} but will not accept it"

    async def test_the_bound_lookup_tool_actually_runs(self) -> None:
        """Calling it the way the model does, through its advertised schema."""
        from workflow_builder.agents.validator import CodeValidator

        registry = ToolsetRegistry()
        registry.register(JIRA_MANIFEST)
        tool = self._by_name(registry=registry, validator=CodeValidator())[
            "call_read_operation"
        ]

        result = json.loads(
            await tool.fn(op_path="jira.users.myself", arguments={})
        )
        assert "unexpected keyword" not in str(result)


class TestInventedModulePaths:
    """A toolset's id is not its module name.

    ``google_calendar`` lives at ``workflow_builder.toolsets.google.calendar``.
    A model that knows only the id builds the import from it, which resolves as
    a plausible path and passes an id-based check — then fails at import, deep
    inside the smoke run, long after the cheap stage that should have caught it.
    """

    MODULES = {  # noqa: RUF012 - test data
        "gmail": "workflow_builder.toolsets.google.gmail.tools",
        "google_calendar": "workflow_builder.toolsets.google.calendar.tools",
    }

    def test_a_path_built_from_the_id_is_rejected(self) -> None:
        from workflow_builder.agents.validator import CodeValidator

        issues = CodeValidator(toolset_modules=self.MODULES).validate(
            "from workflow_builder.toolsets.google_calendar import x\n"
        )
        errors = [i for i in issues if i.category == "toolset"]

        assert errors
        assert "no module" in errors[0].message
        assert "google.calendar.tools" in errors[0].message, "must name the real one"

    def test_the_real_path_is_accepted(self) -> None:
        from workflow_builder.agents.validator import CodeValidator

        issues = CodeValidator(toolset_modules=self.MODULES).validate(
            "from workflow_builder.toolsets.google.calendar.tools import x\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_a_submodule_of_a_known_module_is_accepted(self) -> None:
        from workflow_builder.agents.validator import CodeValidator

        issues = CodeValidator(toolset_modules=self.MODULES).validate(
            "from workflow_builder.toolsets.google.gmail.tools.extra import x\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_the_agent_supplies_the_real_paths(self) -> None:
        from workflow_builder.agents.coding_agent import WorkflowCodingAgent
        from workflow_builder.toolsets.google import (
            GMAIL_MANIFEST,
            GOOGLE_CALENDAR_MANIFEST,
        )

        class FakeModel:
            model_name = "fake"

        registry = ToolsetRegistry()
        registry.register(GMAIL_MANIFEST)
        registry.register(GOOGLE_CALENDAR_MANIFEST)
        agent = WorkflowCodingAgent(FakeModel(), tool_registry=registry)

        assert agent._validator.toolset_modules == self.MODULES
        assert agent._check_context("spec").toolset_modules == self.MODULES
