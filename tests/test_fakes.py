"""Stand-ins for toolset operations, built from their declared schemas.

The sandbox has no credentials, so an integration workflow could previously only
reach a 401 there — proving nothing, and tempting the repair loop into deleting
the integration to make the error go away. Faking the toolset removes the
network without removing the code path.

The fakes are generated rather than written: the manifest already declares each
operation's output schema, so a second, hand-maintained set of fixtures — and
the drift that comes with it — never exists.
"""

from __future__ import annotations

from typing import ClassVar

from workflow_builder.agents.fakes import fake_value, install_fakes
from workflow_builder.toolsets.manifest import OperationSpec, ToolsetManifest


class TestFakeValue:
    def test_scalars(self) -> None:
        assert isinstance(fake_value({"type": "string"}), str)
        assert isinstance(fake_value({"type": "integer"}), int)
        assert isinstance(fake_value({"type": "boolean"}), bool)
        assert fake_value({"type": "null"}) is None

    def test_an_object_gets_every_declared_property(self) -> None:
        built = fake_value(
            {
                "type": "object",
                "properties": {"key": {"type": "string"}, "count": {"type": "integer"}},
            }
        )
        assert set(built) == {"key", "count"}

    def test_an_array_holds_one_element(self) -> None:
        """Enough to exercise a loop, few enough to stay readable."""
        built = fake_value({"type": "array", "items": {"type": "string"}})
        assert len(built) == 1
        assert isinstance(built[0], str)

    def test_an_enum_takes_its_first_value(self) -> None:
        assert fake_value({"type": "string", "enum": ["open", "closed"]}) == "open"

    def test_a_default_is_honoured(self) -> None:
        assert fake_value({"type": "integer", "default": 42}) == 42

    def test_anyof_prefers_a_real_shape_over_null(self) -> None:
        """`.field` off None teaches a caller nothing."""
        built = fake_value({"anyOf": [{"type": "null"}, {"type": "string"}]})
        assert isinstance(built, str)

    def test_a_pydantic_ref_is_resolved(self) -> None:
        """Pydantic emits $ref/$defs for any nested model."""
        built = fake_value(
            {
                "$defs": {
                    "Inner": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    }
                },
                "type": "array",
                "items": {"$ref": "#/$defs/Inner"},
            }
        )
        assert built[0]["name"] == "sample"

    def test_a_real_model_schema_round_trips(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraIssue

        built = fake_value(JiraIssue.model_json_schema())
        assert JiraIssue(**built).key  # constructs without error

    def test_it_is_deterministic(self) -> None:
        """A varying fake would make the replay check blame the code."""
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        assert fake_value(schema) == fake_value(schema)

    def test_recursion_is_bounded(self) -> None:
        """A self-referential schema must not hang the sandbox."""
        schema = {"$defs": {"Node": {"type": "object", "properties": {}}}}
        schema["$defs"]["Node"]["properties"]["child"] = {"$ref": "#/$defs/Node"}
        schema.update({"$ref": "#/$defs/Node"})

        assert fake_value(schema) is not None


class TestInstallFakes:
    def test_it_replaces_the_declared_operations(self) -> None:
        from workflow_builder.toolsets.jira import tools
        from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST

        original = tools.jira_search_issues
        try:
            replaced = install_fakes(JIRA_MANIFEST)
            assert "jira_search_issues" in replaced
            assert tools.jira_search_issues is not original
        finally:
            tools.jira_search_issues = original

    async def test_the_stub_returns_the_declared_shape(self) -> None:
        from workflow_builder.toolsets.jira import tools
        from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST

        originals = {
            op.function: getattr(tools, op.function)
            for op in JIRA_MANIFEST.all_operations()
            if hasattr(tools, op.function)
        }
        try:
            install_fakes(JIRA_MANIFEST)
            issues = await tools.jira_search_issues("project = X")
            assert isinstance(issues, list)
            assert issues and hasattr(issues[0], "key"), "not the declared model"
        finally:
            for name, fn in originals.items():
                setattr(tools, name, fn)

    def test_a_manifest_without_a_module_is_left_alone(self) -> None:
        manifest = ToolsetManifest(
            id="x",
            version="1.0.0",
            summary="No python behind it",
            groups={"g": [OperationSpec(id="g.do", summary="does")]},
        )
        assert install_fakes(manifest) == []

    def test_an_unimportable_module_is_survivable(self) -> None:
        manifest = ToolsetManifest(
            id="x",
            version="1.0.0",
            summary="s",
            tools_module="not.a.real.module",
            groups={"g": [OperationSpec(id="g.do", function="do", summary="does")]},
        )
        assert install_fakes(manifest) == []


class TestSmokeRunsAgainstFakes:
    """The point of all of it: an integration workflow that actually executes."""

    def test_a_toolset_workflow_completes_without_credentials(self) -> None:
        from workflow_builder.agents.smoke import smoke_run

        code = '''
from workflow_builder import Context, step, workflow
from workflow_builder.toolsets.jira.tools import jira_search_issues

@step
async def find(jql: str) -> list:
    """Search."""
    return await jira_search_issues(jql, 5)

@workflow(name="report")
async def report(ctx: Context, jql: str) -> str:
    """Report what was found."""
    issues = await ctx.step(find, jql)
    return f"# {len(issues)} issue(s)\\n" + "\\n".join(f"- {i.key}" for i in issues)
'''
        result = smoke_run(
            code,
            workflow_input="project = X",
            fakes=[(
                "workflow_builder.toolsets.jira.tools",
                "workflow_builder.toolsets.jira.manifest.JIRA_MANIFEST",
            )],
        )

        assert result.ok, result.error
        assert result.status == "completed"
        assert result.steps_executed >= 1
        assert "issue(s)" in result.output_preview

    def test_the_run_uses_fake_data_rather_than_the_service(self) -> None:
        """Asserting on the data proves the substitution, without depending on
        whether this machine happens to hold credentials."""
        from workflow_builder.agents.smoke import smoke_run

        code = '''
from workflow_builder import Context, step, workflow
from workflow_builder.toolsets.jira.tools import jira_search_issues

@step
async def find(jql: str) -> list:
    """Search."""
    return await jira_search_issues(jql, 5)

@workflow(name="keys")
async def keys(ctx: Context, jql: str) -> str:
    """Report the keys found."""
    return ",".join(i.key for i in await ctx.step(find, jql))
'''
        result = smoke_run(
            code,
            workflow_input="project = X",
            fakes=[(
                "workflow_builder.toolsets.jira.tools",
                "workflow_builder.toolsets.jira.manifest.JIRA_MANIFEST",
            )],
        )

        assert result.ok, result.error
        # "sample" is what the schema-derived stub produces for a string field.
        assert "sample" in result.output_preview


class TestBothWaysIntoAToolsetAreFaked:
    """Generated code reaches a toolset two ways; the sandbox served one.

    A direct call inside a ``@step`` binds to the module attribute, which
    ``install_fakes`` replaces — that worked. ``ctx.agent(toolsets=["jira"])``
    never touches the module: it asks the registry for an *executable* toolset,
    and a subprocess registers none, so the run died with "no executable
    toolset 'jira' is registered".

    Which is the shape the coding agent's own resolution ladder tells the model
    to emit when an entity stays ambiguous. The sandbox was rejecting the
    pattern the prompt recommends — an incoherence between two parts of the
    system, not a defect in the generated code, and the reason "adjust the
    prompt" would have been the wrong fix.
    """

    FAKES: ClassVar[list[tuple[str, str]]] = [
        (
            "workflow_builder.toolsets.jira.tools",
            "workflow_builder.toolsets.jira.manifest.JIRA_MANIFEST",
        )
    ]

    DIRECT = (
        "from workflow_builder import Context, step, workflow\n"
        "from workflow_builder.toolsets.jira.tools import jira_search_issues\n\n"
        "@step\n"
        "async def fetch() -> list:\n"
        '    """Fetch."""\n'
        '    issues = await jira_search_issues("project = PA", max_results=10)\n'
        "    return [i.key for i in issues]\n\n"
        '@workflow(name="direct")\n'
        "async def direct(ctx: Context, _=None) -> str:\n"
        '    """Direct call inside a step."""\n'
        "    return str(await ctx.step(fetch))\n"
    )

    VIA_AGENT = (
        "from workflow_builder import Context, workflow\n\n"
        '@workflow(name="via_agent")\n'
        "async def via_agent(ctx: Context, _=None) -> str:\n"
        '    """What the ladder emits for an ambiguous entity."""\n'
        "    answer = await ctx.agent(\n"
        "        \"Which epic is 'saas'?\", toolsets=[\"jira\"]\n"
        "    )\n"
        "    return str(answer.output)\n"
    )

    def test_a_direct_call_inside_a_step_is_faked(self) -> None:
        from workflow_builder.agents.smoke import smoke_run

        result = smoke_run(self.DIRECT, fakes=self.FAKES)
        assert result.ok, result.error

    def test_an_agent_node_resolves_the_toolset_too(self) -> None:
        """The regression. Without the executable toolset this is the failure
        the cookbook hit: "no executable toolset 'jira' is registered"."""
        from workflow_builder.agents.smoke import smoke_run

        result = smoke_run(self.VIA_AGENT, fakes=self.FAKES)
        assert result.ok, result.error
        assert "no executable toolset" not in (result.error or "")

    async def test_both_paths_reach_the_same_stand_ins(self) -> None:
        """One source of fakes. A second set would drift from the first.

        Asserted by *calling* the resolved tool rather than comparing objects:
        ``coerce_tool`` wraps a step in a ``Tool``, so identity says nothing.
        What matters is that it answers with fake data instead of reaching for
        a credential.
        """
        from workflow_builder.agents.fakes import (
            executable_fake_toolset,
            install_fakes,
            uninstall_fakes,
        )
        from workflow_builder.agents.tools import ToolContext
        from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST

        try:
            install_fakes(JIRA_MANIFEST)
            toolset = executable_fake_toolset(JIRA_MANIFEST)
            assert toolset is not None

            answer = await toolset.resolve("issues.search").invoke(
                {"jql": "project = PA"}, ToolContext(agent_name="test")
            )
            assert answer is not None, "the fake produced nothing"
        finally:
            uninstall_fakes()

    def test_resolution_reads_the_module_at_call_time(self) -> None:
        """A toolset built before install_fakes must still resolve to the fake.

        Binding the function at construction would capture the real one, and
        the sandbox would quietly go to the network for whichever path was
        registered first.
        """
        from workflow_builder.agents.fakes import (
            executable_fake_toolset,
            install_fakes,
            uninstall_fakes,
        )
        from workflow_builder.toolsets.jira import tools
        from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST

        toolset = executable_fake_toolset(JIRA_MANIFEST)   # before faking
        real = tools.jira_search_issues
        try:
            install_fakes(JIRA_MANIFEST)
            assert tools.jira_search_issues is not real, "install_fakes did nothing"
            # Built earlier, yet it resolves through the module as it is now.
            assert toolset.resolve("issues.search") is not None
        finally:
            uninstall_fakes()

    def test_the_real_manifest_is_preserved(self) -> None:
        """Not a rebuilt one: pagination, resolvers, and effects must survive."""
        from workflow_builder.agents.fakes import executable_fake_toolset
        from workflow_builder.toolsets.jira.manifest import JIRA_MANIFEST

        toolset = executable_fake_toolset(JIRA_MANIFEST)

        assert toolset.manifest is JIRA_MANIFEST
        assert [op.id for op in toolset.manifest.paginated()]
        assert toolset.manifest.resolvers()

    def test_a_manifest_with_no_module_yields_nothing(self) -> None:
        """Same condition under which install_fakes does nothing."""
        from workflow_builder.agents.fakes import executable_fake_toolset
        from workflow_builder.toolsets.manifest import ToolsetManifest

        bare = ToolsetManifest(id="bare", version="1", summary="no module")
        assert executable_fake_toolset(bare) is None
