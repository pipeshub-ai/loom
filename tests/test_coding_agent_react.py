"""Tests for the refactored ReAct Workflow Coding Agent."""

from __future__ import annotations


class TestCodingAgentStructure:
    def test_coding_output_model(self) -> None:
        from loom.agents.coding_agent import CodingOutput

        out = CodingOutput(code="print('hi')", explanation="test")
        assert out.code == "print('hi')"
        assert out.explanation == "test"

    def test_coding_output_defaults(self) -> None:
        from loom.agents.coding_agent import CodingOutput

        out = CodingOutput(code="x = 1")
        assert out.explanation == ""

    def test_coding_result_is_clean(self) -> None:
        from loom.agents.coding_agent import CodingResult

        r = CodingResult(code="ok")
        assert r.is_clean

    def test_coding_result_not_clean(self) -> None:
        from loom.agents.coding_agent import CodingResult
        from loom.agents.validator import CodeIssue

        r = CodingResult(
            code="bad",
            issues=[CodeIssue("syntax", "broken", "error")],
        )
        assert not r.is_clean

    def test_build_system_prompt_without_docs(self) -> None:
        from loom.agents.coding_agent import (
            WorkflowCodingAgent,
        )

        class FakeModel:
            model_name = "test"

        agent = WorkflowCodingAgent(model=FakeModel())
        prompt = agent.build_system_prompt()
        assert "search_toolsets" in prompt
        assert "validate_code" in prompt
        assert "Pre-loaded" not in prompt

    def test_build_system_prompt_with_docs(self) -> None:
        from loom.agents.coding_agent import (
            WorkflowCodingAgent,
        )

        class FakeModel:
            model_name = "test"

        agent = WorkflowCodingAgent(
            model=FakeModel(),
            tool_docs=["## My Tool\nSome docs here"],
        )
        prompt = agent.build_system_prompt()
        assert "Additional Tool Documentation" in prompt
        assert "## My Tool" in prompt

    def test_extract_code_strips_fences(self) -> None:
        from loom.agents.coding_agent import _extract_code

        fenced = "```python\nprint('hi')\n```"
        assert _extract_code(fenced) == "print('hi')"

    def test_extract_code_plain(self) -> None:
        from loom.agents.coding_agent import _extract_code

        assert _extract_code("x = 1") == "x = 1"

    def test_a_preamble_before_the_fence_is_not_compiled_as_python(self) -> None:
        """The bug this replaced: fences were stripped only when the response
        *began* with one, so one sentence of preamble reached ``compile()``.

        It surfaced as ``invalid character '—' (U+2014) ... line 1``, because
        prose has em-dashes and Python may not have one outside a string — so a
        working model, working tools and working code presented as a syntax
        error in the generated workflow, and no repair round could converge on
        it because the code was never what was broken.
        """
        from loom.agents.coding_agent import _extract_code

        response = (
            "Here is the workflow — it queries the SaaS epic for overdue "
            "issues.\n\n```python\nprint('hi')\n```\n"
        )

        assert _extract_code(response) == "print('hi')"

    def test_trailing_prose_after_the_fence_is_dropped(self) -> None:
        """The same bug from the other end: the closing fence was only removed
        when it was the very last line."""
        from loom.agents.coding_agent import _extract_code

        response = "```python\nprint('hi')\n```\n\nThis uses JQL — see above."

        assert _extract_code(response) == "print('hi')"

    def test_prose_on_both_sides_is_dropped(self) -> None:
        from loom.agents.coding_agent import _extract_code

        response = (
            "I'll build this — first the imports.\n\n"
            "```python\nprint('hi')\n```\n\n"
            "Note the em-dash — it is only in the prose."
        )

        assert _extract_code(response) == "print('hi')"

    def test_a_fence_with_no_language_tag_still_works(self) -> None:
        from loom.agents.coding_agent import _extract_code

        assert _extract_code("Here:\n\n```\nprint('hi')\n```") == "print('hi')"

    def test_the_python_block_wins_over_an_illustrative_one(self) -> None:
        """A response that shows a JQL or JSON snippet before the workflow must
        not have the snippet compiled as the workflow."""
        from loom.agents.coding_agent import _extract_code

        response = (
            "The JQL is:\n\n```jql\nduedate < now()\n```\n\n"
            "And the workflow:\n\n```python\nprint('hi')\n```"
        )

        assert _extract_code(response) == "print('hi')"

    def test_the_larger_python_block_wins(self) -> None:
        """Shown-then-written: the real workflow is much the larger, and
        guessing wrong yields code that compiles and is wrong — which is worse
        than a syntax error, so the rule stays blunt."""
        from loom.agents.coding_agent import _extract_code

        response = (
            "A step looks like:\n\n```python\n@step\n```\n\n"
            "Here is the file:\n\n```python\n"
            "from loom import step, workflow\n\n\n@step\nasync def a() -> int:\n"
            "    return 1\n```"
        )

        extracted = _extract_code(response)
        assert extracted.startswith("from loom import step, workflow")
        assert "```" not in extracted, "prose and fences must not survive"

    def test_a_response_truncated_mid_block_keeps_what_arrived(self) -> None:
        """An unclosed fence means the response hit the token limit. Returning
        the partial code lets the repair loop see a truncated function rather
        than a stray ``` line."""
        from loom.agents.coding_agent import _extract_code

        response = "Here you go —\n\n```python\nfrom loom import workflow\ndef f():"

        extracted = _extract_code(response)
        assert extracted.startswith("from loom import workflow")
        assert "```" not in extracted

    def test_an_empty_block_is_not_chosen_over_a_real_one(self) -> None:
        from loom.agents.coding_agent import _extract_code

        response = "```python\n```\n\n```python\nprint('hi')\n```"

        assert _extract_code(response) == "print('hi')"

    def test_the_extracted_code_compiles(self) -> None:
        """The end of the chain, and the assertion the reported failure was:
        whatever comes out of here is what `compile()` sees."""
        from loom.agents.coding_agent import _extract_code
        from loom.agents.smoke import compile_check

        response = (
            "Here is the workflow — it finds overdue tickets.\n\n"
            "```python\nfrom loom import Context, workflow\n\n\n"
            "@workflow\nasync def overdue(ctx: Context, epic: str) -> list:\n"
            "    return []\n```\n\nIt uses JQL — `duedate < now()`."
        )

        assert compile_check(_extract_code(response)).ok


class TestCodingToolDocsIntegration:
    def test_jira_tool_docs_auto_generated(self) -> None:
        from loom.toolsets.jira.tools import JIRA_TOOL_DOCS

        # Must contain function names
        assert "jira_search_issues" in JIRA_TOOL_DOCS
        assert "jira_create_issue" in JIRA_TOOL_DOCS
        assert "jira_list_projects" in JIRA_TOOL_DOCS
        assert "jira_get_myself" in JIRA_TOOL_DOCS

        # Must contain typed model names
        assert "JiraIssue" in JIRA_TOOL_DOCS
        assert "CreatedIssue" in JIRA_TOOL_DOCS
        assert "JiraProject" in JIRA_TOOL_DOCS
        assert "JiraUser" in JIRA_TOOL_DOCS

        # Must contain import path
        assert "loom.toolsets.jira.tools" in JIRA_TOOL_DOCS

    def test_jira_tool_docs_fields_from_schema(self) -> None:
        from loom.toolsets.jira.tools import JIRA_TOOL_DOCS

        # Must list actual fields from the Pydantic model schemas
        assert "key" in JIRA_TOOL_DOCS
        assert "summary" in JIRA_TOOL_DOCS
        assert "status" in JIRA_TOOL_DOCS
        assert "account_id" in JIRA_TOOL_DOCS


class TestManifestUsesModelSchemas:
    def test_issue_search_output_schema_has_properties(self) -> None:
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        op = JIRA_MANIFEST.find_operation("issues.search")
        assert op is not None
        schema = op.output_schema
        assert schema["type"] == "array"
        items = schema["items"]
        assert "properties" in items
        assert "key" in items["properties"]
        assert "issue_type" in items["properties"]

    def test_create_issue_output_schema(self) -> None:
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        op = JIRA_MANIFEST.find_operation("issues.create")
        assert op is not None
        schema = op.output_schema
        assert "properties" in schema
        assert "key" in schema["properties"]
        assert "url" in schema["properties"]

    def test_users_myself_output_schema(self) -> None:
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        op = JIRA_MANIFEST.find_operation("users.myself")
        assert op is not None
        schema = op.output_schema
        assert "properties" in schema
        assert "account_id" in schema["properties"]
        assert "display_name" in schema["properties"]


class TestAFailureIsDiagnosedNotGuessed:
    """Advice that cannot help is worse than none.

    Every agent-loop failure got the same line — "raise max_discovery_turns or
    narrow the spec" — including a missing API key, where no turn budget helps
    and the spec was never the problem. Someone hitting it goes and rewrites a
    spec that was fine.

    Third place this distinction was needed: the smoke stage fed 401s into the
    repair loop until a workflow came back gutted, the docs runner grew its own
    marker list, and this. It now has one definition.
    """

    async def _issue(self, exc: Exception) -> str:
        from loom.agents.coding_agent import WorkflowCodingAgent

        class Exploding:
            model_name = "boom"

            async def complete(self, *args: object, **kwargs: object) -> object:
                raise exc

        result = await WorkflowCodingAgent(Exploding()).generate("do a thing")
        assert result.code == ""
        return result.issues[0].message

    async def test_a_missing_credential_points_at_the_credential(self) -> None:
        message = await self._issue(
            RuntimeError("Could not resolve authentication method. Expected api_key")
        )

        assert "environment, not the spec" in message
        assert "ANTHROPIC_API_KEY" in message
        assert "max_discovery_turns" not in message, "unhelpable advice"

    async def test_a_real_loop_failure_still_gets_the_turn_advice(self) -> None:
        """The original message is right for the case it was written for."""
        message = await self._issue(RuntimeError("agent exceeded max turns"))

        assert "max_discovery_turns" in message
        assert "environment, not the spec" not in message

    async def test_a_broken_import_is_not_called_environmental(self) -> None:
        """The failures the checks exist to catch stay repairable."""
        message = await self._issue(
            RuntimeError("cannot import name 'Retryy' from 'loom'")
        )

        assert "max_discovery_turns" in message

    def test_the_markers_live_in_one_place(self) -> None:
        from loom.agents.smoke import ENVIRONMENTAL_MARKERS, is_environmental

        assert "api key" in ENVIRONMENTAL_MARKERS
        assert is_environmental("HTTP 401 Unauthorized")
        assert not is_environmental("SyntaxError: invalid syntax")
