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
