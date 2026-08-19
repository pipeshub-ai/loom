"""An identifier in the generated code that nothing looked up.

``ResolutionStage`` catches a spec's *word* reaching a query. This is the same
failure one step later, and it is the one custom-field support makes reachable:
asked for "issues over 8 story points", a model that skips the resolver writes
``customfield_10016`` — a number that is right on some Jira site and wrong on
this one. It compiles, it passes the smoke run (fakes ignore their arguments),
and in production it reads a field that means something else.

The hard part is that baking an id in is *correct*. The ladder in
``DEFAULT_SYSTEM_PROMPT`` says to resolve once and write the id into the code
with the name beside it, so a resolved id and an invented one are identical in
the file. The evidence is not the code but whether a resolver was actually
called, which is why ``CheckContext.resolved_kinds`` comes from the agent's own
tool calls and never from anything it reports.
"""

from __future__ import annotations

import pytest

from loom.agents.checks import CheckContext
from loom.agents.stages import IdentifierStage


@pytest.fixture
def registry():
    from loom.agents.tool_registry import ToolsetRegistry
    from loom.toolsets.jira.manifest import JIRA_MANIFEST
    from loom.toolsets.slack.manifest import SLACK_MANIFEST

    found = ToolsetRegistry()
    found.register(JIRA_MANIFEST)
    found.register(SLACK_MANIFEST)
    return found


GUESSED = '''
from loom import Context, step, workflow

@workflow(name="points")
async def points(ctx: Context, data: dict) -> str:
    issues = await ctx.step(
        jira_search_issues, "project = X", custom_fields=["customfield_10016"]
    )
    return str(len(issues))
'''


class TestAnInventedIdIsFlagged:
    async def test_an_unresolved_custom_field_is_reported(self, registry) -> None:
        result = await IdentifierStage(registry).run(
            GUESSED, CheckContext(spec="all issues over 8 story points")
        )

        assert [issue.category for issue in result.issues] == ["identifiers"]
        assert "customfield_10016" in result.issues[0].message

    async def test_the_issue_names_the_resolver_to_call(self, registry) -> None:
        result = await IdentifierStage(registry).run(
            GUESSED, CheckContext(spec="story points")
        )

        assert "jira_resolve_field" in result.issues[0].message

    async def test_it_warns_rather_than_blocks(self, registry) -> None:
        """Silence here is weak evidence, so a finding must not stop the run."""
        stage = IdentifierStage(registry)
        result = await stage.run(GUESSED, CheckContext(spec="story points"))

        assert not stage.blocking
        assert all(issue.severity == "warning" for issue in result.issues)

    async def test_each_id_is_reported_once(self, registry) -> None:
        code = GUESSED + '\nSECOND = "customfield_10016"\n'

        result = await IdentifierStage(registry).run(
            code, CheckContext(spec="story points")
        )

        assert len(result.issues) == 1

    async def test_a_slack_channel_id_is_flagged_too(self, registry) -> None:
        """Not a Jira feature: any toolset declaring a pattern gets the check."""
        code = 'CHANNEL = "C024BE91L"\n'

        result = await IdentifierStage(registry).run(
            code, CheckContext(spec="post to #incidents")
        )

        assert "slack_find_channel" in result.issues[0].message


class TestWhatItMustNotFlag:
    async def test_an_id_the_spec_supplied_is_left_alone(self, registry) -> None:
        """The caller knows it. That is not a guess."""
        result = await IdentifierStage(registry).run(
            GUESSED,
            CheckContext(spec="story points are in customfield_10016"),
        )

        assert not result.issues

    async def test_a_resolved_kind_is_left_alone(self, registry) -> None:
        """The whole point: a baked id is correct when it was looked up."""
        result = await IdentifierStage(registry).run(
            GUESSED,
            CheckContext(spec="story points", resolved_kinds={"field"}),
        )

        assert not result.issues

    async def test_resolving_a_different_kind_does_not_excuse_it(
        self, registry
    ) -> None:
        """Having resolved a *user* says nothing about where a field id came from."""
        result = await IdentifierStage(registry).run(
            GUESSED,
            CheckContext(spec="story points", resolved_kinds={"user"}),
        )

        assert result.issues

    @pytest.mark.parametrize(
        "word", ["CANCELLED", "COMPLETED", "CONFIRMED", "CRITICAL", "UNASSIGNED"]
    )
    async def test_an_uppercase_constant_is_not_a_slack_id(
        self, registry, word: str
    ) -> None:
        """Every one of these matched ``C[A-Z0-9]{7,}`` before the pattern
        demanded a digit. A check that flags ordinary constants is one people
        switch off, which costs more than the guesses it would have caught."""
        result = await IdentifierStage(registry).run(
            f'STATUS = "{word}"', CheckContext(spec="x")
        )

        assert not result.issues

    async def test_ordinary_code_is_untouched(self, registry) -> None:
        code = '''
QUERY = "project = XYZ AND status = 'In Progress'"
GREETING = "customer_id"
WHEN = "2024-01-01T00:00:00Z"
'''
        result = await IdentifierStage(registry).run(code, CheckContext(spec="x"))

        assert not result.issues

    async def test_an_identifier_in_a_comment_is_not_code(self, registry) -> None:
        """``_string_literals`` parses rather than greps, so the comment naming
        the field beside a resolved id does not itself trip the check."""
        code = 'FIELD = "summary"  # customfield_10016 is Story Points\n'

        result = await IdentifierStage(registry).run(code, CheckContext(spec="x"))

        assert not result.issues


class TestItSaysWhenItCannotCheck:
    async def test_no_registry_is_skipped_not_passed(self) -> None:
        result = await IdentifierStage(None).run("x = 1", CheckContext())

        assert result.skipped
        assert result.reason

    async def test_a_registry_with_no_patterns_is_skipped(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.confluence.manifest import CONFLUENCE_MANIFEST

        found = ToolsetRegistry()
        found.register(CONFLUENCE_MANIFEST)

        result = await IdentifierStage(found).run(GUESSED, CheckContext())

        assert result.skipped

    async def test_a_pattern_nothing_resolves_gives_no_advice_and_is_dropped(
        self,
    ) -> None:
        """An issue that cannot say what to do instead is noise."""
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest

        manifest = ToolsetManifest(
            id="acme",
            version="1.0.0",
            summary="No resolver for the kind its pattern names.",
            tools_module="acme.tools",
            opaque_ids={r"acme_\d+": "widget"},
            groups={
                "things": [
                    OperationSpec(
                        id="things.get",
                        function="acme_get",
                        summary="Get a thing.",
                        effect=EffectClass.READ,
                    )
                ]
            },
        )
        found = ToolsetRegistry()
        found.register(manifest)

        result = await IdentifierStage(found).run(
            'X = "acme_123"', CheckContext(spec="a thing")
        )

        assert result.skipped


class TestTheEvidenceComesFromToolCalls:
    """Never from a self-report: a model can claim a lookup as easily as it can
    invent the id, so a claim would certify the case this exists to catch."""

    class Model:
        """Enough of a ModelProvider to construct the agent; never called."""

        model_name = "test"

        async def complete(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("the identifier check calls no model")

    def _agent(self):
        from loom.agents.coding_agent import WorkflowCodingAgent
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        registry = ToolsetRegistry()
        registry.register(JIRA_MANIFEST)
        return WorkflowCodingAgent(model=self.Model(), tool_registry=registry)

    class Call:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    def test_a_resolver_call_counts(self) -> None:
        calls = [
            self.Call(
                "call_read_operation",
                {"op_path": "jira.fields.resolve", "arguments": {"field_name": "SP"}},
            )
        ]

        assert self._agent()._resolved_kinds(calls) == {"field"}

    def test_json_encoded_arguments_count_too(self) -> None:
        """Models send the object and sometimes the string; both are the call."""
        calls = [
            self.Call("call_read_operation", '{"op_path": "jira.users.resolve"}')
        ]

        assert self._agent()._resolved_kinds(calls) == {"user"}

    def test_a_non_resolver_read_does_not_count(self) -> None:
        """Browsing a toolset is not resolving an entity."""
        calls = [self.Call("call_read_operation", {"op_path": "jira.issues.search"})]

        assert self._agent()._resolved_kinds(calls) == set()

    def test_merely_showing_the_toolset_does_not_count(self) -> None:
        calls = [self.Call("show_toolset", {"toolset_id": "jira"})]

        assert self._agent()._resolved_kinds(calls) == set()

    def test_no_calls_resolve_nothing(self) -> None:
        assert self._agent()._resolved_kinds([]) == set()
        assert self._agent()._resolved_kinds(None) == set()


class TestItIsInTheDefaultPipeline:
    def test_the_stage_runs_by_default(self) -> None:
        from loom.agents.stages import default_stages

        names = [stage.name for stage in default_stages(smoke=False)]

        assert "identifiers" in names

    def test_it_runs_after_the_cheaper_stages(self) -> None:
        """Cheapest first: a syntax error should not be reported as a bad id."""
        from loom.agents.stages import default_stages

        stages = {stage.name: stage.cost for stage in default_stages(smoke=False)}

        assert stages["identifiers"] > stages["compile"]
        assert stages["identifiers"] > stages["resolution"]


class TestThePatternsDoNotFireOnCorrectCode:
    """The declared patterns, against the workflows this repo ships.

    Not a proof of no false positives — it is a corpus, not every string a
    model will write. It is the strongest available evidence, and a regression
    guard: an over-broad pattern added later lights up here rather than in
    somebody's generated file.
    """

    def _examples(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "examples"
        return sorted(root.rglob("*.py"))

    def test_the_corpus_is_not_empty(self) -> None:
        """A sweep over nothing passes over anything."""
        files = self._examples()

        assert len(files) > 20
        # And it does exercise the vendors that declare patterns, so a match
        # would have somewhere to happen.
        text = "\n".join(f.read_text() for f in files)
        assert "slack" in text and "jira" in text

    def test_no_shipped_example_trips_a_pattern(self) -> None:
        import ast
        import re

        from loom.toolsets.registry import get_catalog, register_available_toolsets

        register_available_toolsets()
        catalog = get_catalog()
        patterns = [
            (toolset_id, re.compile(pattern))
            for toolset_id in catalog.list_toolsets()
            for pattern in (catalog.get(toolset_id).opaque_ids or {})
        ]
        assert patterns, "nothing declares a pattern — this check is vacuous"

        hits: list[str] = []
        for path in self._examples():
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                for toolset_id, regex in patterns:
                    for found in regex.findall(node.value):
                        hits.append(f"{path.name}: {found!r} matched {toolset_id}")

        assert not hits, "an opaque-id pattern fires on hand-written code:\n  " + "\n  ".join(hits)


class TestTwoToolsetsMayShareAShape:
    """``re.compile`` caches, so an identical pattern from two toolsets is the
    *same object*. Keyed by it, the second declaration vanished silently."""

    async def test_both_declarations_survive(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest

        def manifest(toolset_id: str, kind: str) -> ToolsetManifest:
            return ToolsetManifest(
                id=toolset_id,
                version="1.0.0",
                summary=f"{toolset_id}, sharing an id shape.",
                tools_module=f"{toolset_id}.tools",
                opaque_ids={r"\bxx_[0-9]{4}\b": kind},
                groups={
                    "g": [
                        OperationSpec(
                            id="g.find",
                            function=f"{toolset_id}_find",
                            summary="Find one.",
                            effect=EffectClass.READ,
                            resolves=kind,
                        )
                    ]
                },
            )

        registry = ToolsetRegistry()
        registry.register(manifest("alpha", "widget"))
        registry.register(manifest("beta", "gadget"))

        stage = IdentifierStage(registry)
        assert len(stage._patterns(registry)) == 2

        result = await stage.run('X = "xx_1234"', CheckContext(spec="a thing"))

        # One id, reported once — but resolving either kind must clear it only
        # for the toolset that declared that kind.
        assert len(result.issues) == 1
