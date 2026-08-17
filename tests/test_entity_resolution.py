"""Resolving what a spec refers to, before code depends on it.

The failure this addresses returns zero rows and no error: a query filters on
the words a person used — a display name, a status they assume exists — while
the API matches identifiers and its own configured vocabulary. Nothing joins
the two, and "no results" reads as "nothing to do".
"""

from __future__ import annotations

import json

import pytest

from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT
from loom.agents.coding_tools import call_read_operation
from loom.agents.tool_registry import ToolsetRegistry
from loom.toolsets.jira.manifest import JIRA_MANIFEST
from loom.toolsets.manifest import (
    OperationSpec,
    ToolsetManifest,
)


@pytest.fixture(autouse=True)
def _jira_registered():
    """The global catalog is reset between tests; these need Jira in it."""
    from loom.toolsets.registry import get_catalog, register_toolset

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
        """Meet, because every input it takes is a resource name produced by
        another call — there is no human-written name to resolve.

        This was Gmail until Gmail gained a *label* resolver: labelling takes
        ``Label_7`` and a person says "Urgent", so it turned out to have the
        very problem this marker exists for.
        """
        from loom.toolsets.google import GOOGLE_MEET_MANIFEST

        assert GOOGLE_MEET_MANIFEST.resolvers() == {}

    def test_gmail_resolves_a_label(self) -> None:
        """Passing a label *name* where an id belongs is not an error — Gmail
        accepts the call and applies nothing."""
        from loom.toolsets.google import GMAIL_MANIFEST

        assert GMAIL_MANIFEST.resolvers()["label"].function == "gmail_find_label"

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

        import loom.toolsets.jira.client as client_module

        monkeypatch.setattr(client_module, "_default_client", None)

        result = json.loads(await call("jira.users.resolve", '{"name": "x"}'))
        assert "error" in result
        assert "resolves it at runtime" in result["note"]


def prompt_text() -> str:
    """The system prompt with whitespace collapsed.

    Assertions on wrapped prose otherwise break the moment a line is rewrapped,
    which says nothing about whether the instruction is still there.
    """
    from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

    return " ".join(DEFAULT_SYSTEM_PROMPT.split())


class TestThePromptStaysGeneric:
    """No toolset or person should be named in instructions every spec pays for."""

    def test_it_names_no_specific_toolset_or_person(self) -> None:
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

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
        from loom.agents.coding_tools import build_coding_tools

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
        from loom.toolsets.confluence.manifest import CONFLUENCE_MANIFEST
        from loom.toolsets.google import (
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
        from loom.toolsets.manifest import OperationSpec, ToolsetManifest

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
        assert "from loom.toolsets.jira.tools import" in index

    def test_the_index_says_how_to_get_the_detail(self) -> None:
        index = self._registry().describe(detail="index")
        assert 'show_toolset("jira")' in index
        assert 'get_tool_docs("jira")' in index

    def test_the_system_prompt_uses_the_index(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent

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
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(available_toolsets={"jira"}).validate(
            "from loom.toolsets.slack.tools import slack_post\n"
        )
        toolset_issues = [i for i in issues if i.category == "toolset"]

        assert toolset_issues
        assert toolset_issues[0].severity == "error"
        assert "no 'slack' toolset" in toolset_issues[0].message
        assert "jira" in toolset_issues[0].message, "must name what IS available"

    def test_an_available_toolset_passes(self) -> None:
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(available_toolsets={"jira"}).validate(
            "from loom.toolsets.jira.tools import jira_get_issue\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_google_toolsets_are_matched_at_the_right_depth(self) -> None:
        """Their modules nest one deeper than the others."""
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(available_toolsets={"gmail"}).validate(
            "from loom.toolsets.google.gmail.tools import gmail_get_message\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_each_missing_toolset_is_reported_once(self) -> None:
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(available_toolsets=set()).validate(
            "from loom.toolsets.slack.tools import a\n"
            "from loom.toolsets.slack.client import b\n"
        )
        assert len([i for i in issues if i.category == "toolset"]) == 1

    def test_the_check_is_off_by_default(self) -> None:
        """A caller who never said what exists should not be second-guessed."""
        from loom.agents.validator import CodeValidator

        issues = CodeValidator().validate(
            "from loom.toolsets.slack.tools import slack_post\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_the_agent_wires_the_registry_into_the_validator(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent

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
        from loom.agents.coding_agent import (
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

        import loom.agents.agent as agent_module

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
        """Every line is paid on every turn of every generation.

        A drift guard, not a physical limit — so it moves when a genuinely new
        required behaviour lands, and only then. It moved once from 8000 to
        8500, to pay for three rules added after real generations went wrong:
        classifying each node as code or judgement, reporting what a capped
        list covers, and refusing a fuzzy text match as a substitute for
        resolution. The space was earned first by deleting a duplicated
        ``ctx.agent()`` section and three restated paragraphs.

        It moved a second time, from 8500 to 8900, after the prompt reached
        9007 and the same search was run again. That search found 159
        characters of real restatement, all deleted: an I/O rule stated twice
        ("Do all I/O inside step functions" and "NEVER do I/O in the workflow
        body"), VALIDATE and FIX as two steps of one loop, and four artifact
        rules spread across eleven lines of prose.

        Seven further candidates turned out not to be restatement. Every one is
        pinned by a test that records why the phrase exists —
        ``test_it_names_each_rung``, ``test_ambiguity_routes_to_an_agent_node``,
        ``test_the_demo_block_prints_the_error`` (because "Status: failed /
        Output: None" says nothing a reader can act on),
        ``test_the_prompt_forbids_choosing_one``, and three more. Deleting them
        shortened nothing and broke the suite, which is the signal that the
        search is finished: what is left either carries a rule or carries the
        reason a rule is followed.

        It moved a third time, from 8900 to 9000, to pay for one corrected
        rule. The step-functions section had told the model to call toolset
        tools *inside* another ``@step`` rather than through ``ctx.step`` —
        which two of the prompt's own code samples contradicted, and which
        ``TestOnlyJournaledCallsAreGuarded`` shows is not merely less granular
        on replay: ``broker.dispatch`` runs inside ``DurableCall._resolve``, so
        a direct call is never weighed against the workflow's ``GrantSet`` at
        all. The correction states the rule and the three things a direct call
        skips.

        It moved a fourth time, from 9000 to 9400, to pay for two more. The
        paging advice told the model to "take one page per step with
        ``cursor=``" — and an introspection sweep of every shipped toolset
        found 82 paged reads and 82 without a ``cursor`` parameter, so code
        following the instruction raised ``TypeError``. That is replaced with
        advice that can be carried out: bound the window, not the row count.
        The second is the placement rule — filter in the query rather than
        after it. Nothing anywhere said so, and ``PlacementStage`` exists
        because a workflow that pages an entire project and keeps six rows in
        a comprehension passed all ten stages while reporting a truncated
        fetch as a complete answer.

        The margin above the current length is about one sentence wide on
        purpose, so the next addition has to run this search too.
        """
        from loom.agents.coding_agent import DEFAULT_SYSTEM_PROMPT

        assert len(DEFAULT_SYSTEM_PROMPT) < 9400, "the prompt is drifting long"


class TestRepeatedLookupsAreStopped:
    """A repeated identical lookup cannot produce a different answer.

    Observed: 14 calls to the same operation in one generation, the model
    varying only the argument encoding — {} then null then "{}" then omitted —
    until the turn budget died. The encodings differ; the call does not, so the
    signature is taken after normalising.
    """

    async def test_the_third_identical_call_is_refused(self) -> None:
        from loom.agents.coding_tools import _call_read_operation

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
        from loom.agents.coding_tools import _call_read_operation

        seen: dict[str, int] = {}
        for arguments in ({}, None, "{}", ""):
            await _call_read_operation(
                "jira.projects.list", arguments, registry=None, seen=seen
            )

        assert len(seen) == 1, f"encodings were counted separately: {seen}"
        assert sum(seen.values()) == 4

    async def test_the_refusal_says_what_to_do_instead(self) -> None:
        """Stopping a loop is only useful if it also names the way forward."""
        from loom.agents.coding_tools import _call_read_operation

        seen: dict[str, int] = {}
        for _ in range(3):
            out = await _call_read_operation(
                "jira.projects.list", {}, registry=None, seen=seen
            )

        message = json.loads(out)["error"]
        assert "ctx.agent()" in message
        assert "return the workflow" in message

    async def test_different_calls_are_counted_apart(self) -> None:
        from loom.agents.coding_tools import _call_read_operation

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
        from loom.agents.coding_tools import _call_read_operation

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
        from loom.agents.coding_tools import build_coding_tools

        return {tool.name: tool for tool in build_coding_tools(**kwargs)}

    def test_every_tool_keeps_its_schema_when_bound(self) -> None:
        from loom.agents.validator import CodeValidator

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

        from loom.agents.validator import CodeValidator

        bound = self._by_name(registry=ToolsetRegistry(), validator=CodeValidator())
        for name, tool in bound.items():
            advertised = set(tool.parameters.get("properties", {}))
            accepted = set(inspect.signature(tool.fn).parameters)
            missing = advertised - accepted
            assert not missing, f"{name} advertises {missing} but will not accept it"

    async def test_the_bound_lookup_tool_actually_runs(self) -> None:
        """Calling it the way the model does, through its advertised schema."""
        from loom.agents.validator import CodeValidator

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

    ``google_calendar`` lives at ``loom.toolsets.google.calendar``.
    A model that knows only the id builds the import from it, which resolves as
    a plausible path and passes an id-based check — then fails at import, deep
    inside the smoke run, long after the cheap stage that should have caught it.
    """

    MODULES = {  # noqa: RUF012 - test data
        "gmail": "loom.toolsets.google.gmail.tools",
        "google_calendar": "loom.toolsets.google.calendar.tools",
    }

    def test_a_path_built_from_the_id_is_rejected(self) -> None:
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(toolset_modules=self.MODULES).validate(
            "from loom.toolsets.google_calendar import x\n"
        )
        errors = [i for i in issues if i.category == "toolset"]

        assert errors
        assert "no module" in errors[0].message
        assert "google.calendar.tools" in errors[0].message, "must name the real one"

    def test_the_real_path_is_accepted(self) -> None:
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(toolset_modules=self.MODULES).validate(
            "from loom.toolsets.google.calendar.tools import x\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_a_submodule_of_a_known_module_is_accepted(self) -> None:
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(toolset_modules=self.MODULES).validate(
            "from loom.toolsets.google.gmail.tools.extra import x\n"
        )
        assert not [i for i in issues if i.category == "toolset"]

    def test_the_agent_supplies_the_real_paths(self) -> None:
        from loom.agents.coding_agent import WorkflowCodingAgent
        from loom.toolsets.google import (
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


class TestToolsetModulesOutsideLoom:
    """A custom toolset's ``tools_module`` need not live under ``loom`` at
    all -- a host application bridging its own tool-execution layer in (the
    shape ``docs/guides/embedding.md`` describes) necessarily declares a
    module at its own root. ``_check_toolsets`` already special-cases
    ``loom.toolsets.*`` imports against ``toolset_modules``; this pins that
    ``_check_allowed_packages`` -- the *other* check a strict
    ``allowed_packages`` activates -- honours the same allowlist instead of
    rejecting the declared import outright because its root package was
    never named in ``allowed_packages``.
    """

    MODULES = {"host_bridge": "myapp.integrations.bridge"}  # noqa: RUF012

    def test_the_declared_module_is_importable_under_a_strict_allowlist(self) -> None:
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(
            allowed_packages=frozenset(), toolset_modules=self.MODULES,
        ).validate("import loom\nfrom myapp.integrations.bridge import call_tool\n")

        assert not [i for i in issues if i.category == "imports"]

    def test_a_submodule_of_the_declared_module_is_also_importable(self) -> None:
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(
            allowed_packages=frozenset(), toolset_modules=self.MODULES,
        ).validate("import loom\nfrom myapp.integrations.bridge.extra import call_tool\n")

        assert not [i for i in issues if i.category == "imports"]

    def test_a_different_module_under_the_same_root_is_still_rejected(self) -> None:
        """The allowance is for the exact declared path, never the bare
        root -- otherwise naming one toolset's module would quietly widen
        every other import from the same host package."""
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(
            allowed_packages=frozenset(), toolset_modules=self.MODULES,
        ).validate("import loom\nfrom myapp.secrets import credentials\n")

        errors = [i for i in issues if i.category == "imports" and "myapp" in i.message]
        assert errors

    def test_with_no_toolset_modules_declared_the_same_import_is_still_rejected(self) -> None:
        """Behaviour is unchanged for callers that pass no `toolset_modules`
        at all -- this is additive, not a general relaxation of
        `allowed_packages`."""
        from loom.agents.validator import CodeValidator

        issues = CodeValidator(allowed_packages=frozenset()).validate(
            "import loom\nfrom myapp.integrations.bridge import call_tool\n"
        )

        assert [i for i in issues if i.category == "imports" and "myapp" in i.message]


class TestCodeOrJudgement:
    """Every node is either a rule or a judgement, and the choice is stated.

    The failure this guards against is the quiet one: a model asked to decide
    which emails "need attention" writes ``if "urgent" in subject.lower()``,
    which compiles, runs, passes a smoke test, and is wrong on exactly the
    email that mattered. A keyword list is a rule the spec never gave.

    So the prompt has to say three things — how to tell the cases apart, which
    way to fall when unsure, and that the choice must be reported — and the
    result has to carry the answer somewhere a reviewer can read it.
    """

    def test_the_prompt_gives_one_test_for_the_decision(self) -> None:
        prompt = DEFAULT_SYSTEM_PROMPT

        assert "Code or judgement" in prompt
        assert "right for every" in prompt and "input the spec allows" in prompt

    def test_doubt_resolves_toward_the_agent(self) -> None:
        """The instruction that decides the ambiguous half of the cases."""
        prompt = DEFAULT_SYSTEM_PROMPT

        assert "When in doubt, use the agent" in prompt
        assert "unsure → `ctx.agent()`" in prompt

    def test_the_prompt_names_the_tell_for_an_invented_rule(self) -> None:
        """Abstract advice does not survive contact with a concrete spec."""
        prompt = DEFAULT_SYSTEM_PROMPT

        assert "keyword list" in prompt
        assert 'if "urgent" in subject.lower()' in prompt
        assert "invented the constant" in prompt

    def test_judgement_without_a_stated_threshold_goes_to_the_agent(self) -> None:
        prompt = DEFAULT_SYSTEM_PROMPT

        for phrase in ("needs attention", "important", "relevant"):
            assert phrase in prompt, phrase
        assert "no stated threshold" in prompt

    def test_a_lookup_is_still_not_judgement(self) -> None:
        """"When in doubt use the agent" must not undo entity resolution.

        These two rules pull opposite ways and the prompt carries both, so the
        boundary between them is the thing worth pinning: doubt about whether a
        *rule* fits sends you to the agent; doubt about *what something is*
        sends you to a lookup at authoring time.
        """
        prompt = DEFAULT_SYSTEM_PROMPT

        assert "is a query, not judgement" in prompt
        assert "not about facts you can go and find" in prompt
        assert "re-answers it, differently, on every run" in prompt

    def test_hybrid_nodes_are_split_rather_than_forced(self) -> None:
        prompt = DEFAULT_SYSTEM_PROMPT

        assert "Part rule, part judgement" in prompt
        assert "fetches and narrows" in prompt

    def test_resolution_and_planning_precede_generation(self) -> None:
        """Ordering is the requirement, not just the presence of the steps."""
        prompt = DEFAULT_SYSTEM_PROMPT

        assert prompt.index("4. RESOLVE") < prompt.index("6. GENERATE")
        assert prompt.index("5. PLAN") < prompt.index("6. GENERATE")
        assert "RESOLVE and PLAN come before GENERATE" in prompt

    def test_the_plan_is_asked_for_in_the_structured_output(self) -> None:
        """A rule followed silently is a rule nobody can check was followed."""
        from loom.agents.coding_agent import CodingOutput

        assert "plan" in CodingOutput.model_fields
        described = CodingOutput.model_json_schema()["properties"]["plan"]
        assert "step/agent" in str(described)

    def test_a_plan_survives_onto_the_result(self) -> None:
        from loom.agents.coding_agent import CodingResult, NodePlan

        result = CodingResult(
            code="x = 1",
            plan=[
                NodePlan(node="fetch unread mail", kind="step", why="an API call"),
                NodePlan(node="pick what needs a reply", kind="agent", why="judgement"),
                NodePlan(node="send the summary", kind="step", why="an API call"),
            ],
        )

        assert [node.node for node in result.judgement_nodes] == [
            "pick what needs a reply"
        ]

    def test_no_plan_is_not_a_claim_that_nothing_is_probabilistic(self) -> None:
        """An absent plan means unreported, not "all deterministic"."""
        from loom.agents.coding_agent import CodingResult

        assert CodingResult(code="x = 1").plan == []
        assert CodingResult(code="x = 1").judgement_nodes == []

    def test_a_plan_arriving_as_plain_dicts_is_still_read(self) -> None:
        """Small models return the shape without the type.

        The structured-output layer coerces where it can; a plan dropped on the
        floor because it arrived as dicts would make the field look unused and
        get deleted.
        """
        from loom.agents.coding_agent import NodePlan

        parsed = [
            NodePlan.model_validate(entry)
            for entry in [{"node": "summarise", "kind": "agent", "why": "open-ended"}]
        ]
        assert parsed[0].kind == "agent"


class TestResolutionStage:
    """Catching the guess that looks like a search.

    From a real generation. Asked for "all the stories in **sas work**", the
    agent wrote ``text ~ "saas"`` — it corrected the spelling on its own and
    fuzzy-matched free text rather than resolving "sas work" to a project or an
    epic. The prompt already forbade this; nothing checked it.
    """

    GUESSED = (
        "from loom.toolsets.jira.tools import jira_search_issues\n"
        "async def fetch() -> list:\n"
        "    return await jira_search_issues(\n"
        "        'issuetype = Story AND text ~ \"saas\" ORDER BY created DESC'\n"
        "    )\n"
    )
    SPEC = "show all the stories in sas work"

    async def _run(self, code: str, spec: str = SPEC):
        from loom.agents.checks import CheckContext
        from loom.agents.stages import ResolutionStage

        return await ResolutionStage().run(code, CheckContext(spec=spec))

    async def test_it_catches_the_generation_that_prompted_it(self) -> None:
        issues = (await self._run(self.GUESSED)).issues

        assert len(issues) == 1
        assert issues[0].category == "resolution"
        assert "respelled" in issues[0].message, "the silent correction is the tell"
        assert "call_read_operation" in issues[0].message, "name the fix"

    async def test_a_resolved_id_passes(self) -> None:
        """The shape the prompt asks for: an id, the name in a comment."""
        resolved = (
            "async def fetch() -> list:\n"
            '    # PA-1844 = "sas work"\n'
            "    return await jira_search_issues('parent = PA-1844')\n"
        )
        assert not (await self._run(resolved)).issues

    async def test_an_exact_comparison_is_left_alone(self) -> None:
        """``status = "In Progress"`` is a plausible resolved value.

        Only a *match* operator is evidence of a guess. Flagging equality would
        fire on every correctly-resolved workflow and get the check ignored.
        """
        exact = (
            "async def fetch() -> list:\n"
            "    return await jira_search_issues('status = \"In Progress\"')\n"
        )
        assert not (await self._run(exact, "tickets in progress")).issues

    async def test_the_spec_s_word_used_verbatim_is_caught_too(self) -> None:
        """Not only respellings — the exact word is the plainer version."""
        verbatim = (
            "async def fetch() -> list:\n"
            "    return await jira_search_issues('text ~ \"onboarding\"')\n"
        )
        issues = (await self._run(verbatim, "show the onboarding stories")).issues

        assert len(issues) == 1
        assert "the spec's 'onboarding'" in issues[0].message

    async def test_words_the_spec_uses_about_the_task_are_not_entities(self) -> None:
        """"show all the stories" names nothing; it describes the request."""
        about = (
            "async def fetch() -> list:\n"
            "    return await jira_search_issues('text ~ \"stories\"')\n"
        )
        assert not (await self._run(about, "show all the stories")).issues

    async def test_a_fuzzy_match_on_something_not_in_the_spec_passes(self) -> None:
        """A term the author chose deliberately is not a guess about the spec."""
        deliberate = (
            "async def fetch() -> list:\n"
            "    return await jira_search_issues('text ~ \"regression\"')\n"
        )
        assert not (await self._run(deliberate, "show all the stories in sas work")).issues

    async def test_a_tilde_outside_a_string_is_not_a_query(self) -> None:
        """Parsed, not grepped — a comment is not code."""
        commented = "# text ~ \"saas\" would be wrong here\nx = 1\n"
        assert not (await self._run(commented)).issues

    async def test_unparseable_code_is_not_this_stage_s_problem(self) -> None:
        assert not (await self._run("def (")).issues

    async def test_it_reports_at_most_a_few(self) -> None:
        """A wall of near-identical warnings is one warning nobody reads."""
        many = "\n".join(
            f"q{i} = 'text ~ \"saas\"'" for i in range(10)
        )
        assert len((await self._run(many)).issues) <= 3

    async def test_it_is_a_warning_and_runs_before_the_expensive_stages(self) -> None:
        from loom.agents.stages import ResolutionStage, default_stages

        assert ResolutionStage().blocking is False
        names = [stage.name for stage in default_stages()]
        assert names.index("resolution") < names.index("smoke")

    def test_the_prompt_names_the_disguise(self) -> None:
        assert "fuzzy text search is not a resolution" in DEFAULT_SYSTEM_PROMPT
        flat = " ".join(DEFAULT_SYSTEM_PROMPT.split())
        assert "Nor may you quietly fix a spelling" in flat
        assert '"sas" becoming "saas" is a guess' in flat


class TestGeneratedCodeIsScannedForDangerousCalls:
    """`CodeValidator` was a determinism-and-structure linter, nothing more.

    The premise of the whole coding-agent design is that generated code is
    untrusted. The sandbox contains *execution*; refusing at authoring time is
    the cheaper layer, and it produces something a person can act on rather than
    a runtime failure discovered later against real credentials.
    """

    def _security(self, code: str):
        from loom.agents.validator import CodeValidator

        return [i for i in CodeValidator().validate(code) if i.category == "security"]

    WORKFLOW = (
        "from loom import Context, step, workflow\n"
        "@step\n"
        "async def s(n: int) -> int:\n"
        "    {body}\n"
        "@workflow(name='x')\n"
        "async def x(ctx: Context, n: int) -> str:\n"
        "    return str(await ctx.step(s, n))\n"
    )

    def test_eval_is_an_error(self) -> None:
        issues = self._security(self.WORKFLOW.format(body="return eval('1+1')"))
        assert issues and issues[0].severity == "error"

    def test_exec_is_an_error(self) -> None:
        assert self._security(self.WORKFLOW.format(body="exec('x=1')\n    return n"))

    def test_shelling_out_is_an_error(self) -> None:
        code = "import os\n" + self.WORKFLOW.format(body="os.system('ls')\n    return n")
        errors = [i for i in self._security(code) if i.severity == "error"]
        assert errors

    def test_a_step_body_is_scanned_too(self) -> None:
        """Whole-module, not workflow-body-only.

        The determinism checks look only at the orchestration body, because
        determinism is a property of that body. This is a different question —
        the artefact is about to run somewhere — and `subprocess.run` inside a
        `@step` is exactly as much of a decision as one in the body.
        """
        code = "import subprocess\n" + self.WORKFLOW.format(
            body="subprocess.run(['ls'])\n    return n"
        )
        assert [i for i in self._security(code) if i.severity == "error"]

    def test_a_risky_import_is_a_warning_not_a_refusal(self) -> None:
        """`socket` inside a step can be the job — flag it, do not fail it."""
        code = "import socket\n" + self.WORKFLOW.format(body="return n")
        issues = self._security(code)
        assert issues
        assert all(i.severity == "warning" for i in issues)

    def test_a_risky_import_is_reported_once(self) -> None:
        code = "import socket\nimport socket\n" + self.WORKFLOW.format(body="return n")
        assert len(self._security(code)) == 1

    def test_ordinary_generated_code_is_clean(self) -> None:
        """The negative control — a scanner that fires on normal work is noise."""
        assert not self._security(self.WORKFLOW.format(body="return n * 2"))
