"""Tests for Phase 7 — Small Model Compatibility.

Covers: model capability detection, scaffolding engine, code validator,
schema simplifier.
"""

from __future__ import annotations

import pytest

from loom.agents.capability import (
    ModelTier,
    detect_capabilities,
    detect_tier,
)
from loom.agents.scaffolding import (
    ScaffoldingEngine,
    StepSkeleton,
    WorkflowSkeleton,
)
from loom.agents.schema_simplifier import SchemaSimplifier
from loom.agents.validator import (
    BARE_IO_CALLS,
    NONDETERMINISTIC_CALLS,
    CodeIssue,
    CodeValidator,
)

# ---------------------------------------------------------------------------
# Model Capability Detection
# ---------------------------------------------------------------------------


class TestModelTier:
    def test_gpt4_is_large(self) -> None:
        assert detect_tier("gpt-4-turbo") == ModelTier.LARGE

    def test_gpt4o_is_large(self) -> None:
        assert detect_tier("gpt-4o") == ModelTier.LARGE

    def test_claude_sonnet_is_large(self) -> None:
        assert detect_tier("claude-sonnet-4-20250514") == ModelTier.LARGE

    def test_claude_opus_is_large(self) -> None:
        assert detect_tier("claude-opus-4-20250514") == ModelTier.LARGE

    def test_claude_3_5_is_large(self) -> None:
        assert detect_tier("claude-3.5-sonnet") == ModelTier.LARGE

    def test_o1_is_large(self) -> None:
        assert detect_tier("o1-preview") == ModelTier.LARGE

    def test_o3_is_large(self) -> None:
        assert detect_tier("o3-mini") == ModelTier.LARGE

    def test_gemini_pro_is_large(self) -> None:
        assert detect_tier("gemini-2.5-pro") == ModelTier.LARGE

    def test_claude_haiku_is_medium(self) -> None:
        assert detect_tier("claude-haiku-3") == ModelTier.MEDIUM

    def test_gemini_flash_is_medium(self) -> None:
        assert detect_tier("gemini-2.0-flash") == ModelTier.MEDIUM

    def test_gpt35_is_medium(self) -> None:
        assert detect_tier("gpt-3.5-turbo") == ModelTier.MEDIUM

    def test_mixtral_is_medium(self) -> None:
        assert detect_tier("mixtral-8x7b") == ModelTier.MEDIUM

    def test_llama_70b_is_medium(self) -> None:
        assert detect_tier("llama-3-70b") == ModelTier.MEDIUM

    def test_llama_8b_is_small(self) -> None:
        assert detect_tier("llama-3-8b") == ModelTier.SMALL

    def test_mistral_7b_is_small(self) -> None:
        assert detect_tier("mistral-7b-instruct") == ModelTier.SMALL

    def test_phi3_is_small(self) -> None:
        assert detect_tier("phi-3-mini") == ModelTier.SMALL

    def test_gemma_2b_is_small(self) -> None:
        assert detect_tier("gemma-2b") == ModelTier.SMALL

    def test_unknown_defaults_medium(self) -> None:
        assert detect_tier("my-custom-model") == ModelTier.MEDIUM

    def test_case_insensitive(self) -> None:
        assert detect_tier("GPT-4-Turbo") == ModelTier.LARGE


class TestModelCapabilities:
    def test_large_capabilities(self) -> None:
        caps = detect_capabilities("gpt-4o")
        assert caps.tier == ModelTier.LARGE
        assert caps.supports_tool_use is True
        assert caps.supports_structured_output is True
        assert caps.supports_parallel_tools is True
        assert caps.max_reliable_output_tokens == 4096
        assert caps.json_mode_available is True

    def test_medium_capabilities(self) -> None:
        caps = detect_capabilities("gpt-3.5-turbo")
        assert caps.tier == ModelTier.MEDIUM
        assert caps.supports_tool_use is True
        assert caps.supports_structured_output is True
        assert caps.supports_parallel_tools is False
        assert caps.max_reliable_output_tokens == 2048
        assert caps.json_mode_available is True

    def test_small_capabilities(self) -> None:
        caps = detect_capabilities("phi-3-mini")
        assert caps.tier == ModelTier.SMALL
        assert caps.supports_tool_use is True
        assert caps.supports_structured_output is False
        assert caps.supports_parallel_tools is False
        assert caps.max_reliable_output_tokens == 1024
        assert caps.json_mode_available is False

    def test_frozen_dataclass(self) -> None:
        caps = detect_capabilities("gpt-4o")
        with pytest.raises(AttributeError):
            caps.tier = ModelTier.SMALL  # type: ignore[misc]

    def test_all_tiers_exist(self) -> None:
        assert len(ModelTier) == 3


# ---------------------------------------------------------------------------
# Scaffolding Engine
# ---------------------------------------------------------------------------


class TestScaffoldingEngine:
    def test_default_templates_registered(self) -> None:
        engine = ScaffoldingEngine()
        templates = engine.list_templates()
        assert len(templates) == 4

    def test_match_template_fetch(self) -> None:
        engine = ScaffoldingEngine()
        result = engine.match_template("fetch data and notify")
        assert result is not None
        assert result.name == "fetch_transform_notify"

    def test_match_template_webhook(self) -> None:
        engine = ScaffoldingEngine()
        result = engine.match_template("webhook processing")
        assert result is not None
        assert result.name == "webhook_process_store"

    def test_match_template_schedule(self) -> None:
        engine = ScaffoldingEngine()
        result = engine.match_template("schedule a scrape")
        assert result is not None
        assert result.name == "schedule_scrape_report"

    def test_match_template_ai(self) -> None:
        engine = ScaffoldingEngine()
        result = engine.match_template("ai pipeline for text")
        assert result is not None
        assert result.name == "ai_pipeline"

    def test_match_no_match(self) -> None:
        engine = ScaffoldingEngine()
        result = engine.match_template("zzz qqq xyz")
        assert result is None

    def test_build_skeleton_compiles(self) -> None:
        engine = ScaffoldingEngine()
        code = engine.build_skeleton(
            "process orders",
            [
                {"name": "validate", "description": "Validate order"},
                {"name": "charge", "description": "Charge customer"},
            ],
        )
        compile(code, "<test>", "exec")

    def test_build_skeleton_has_decorators(self) -> None:
        engine = ScaffoldingEngine()
        code = engine.build_skeleton(
            "process orders",
            [{"name": "validate", "description": "Validate order"}],
        )
        assert "@step" in code
        assert "@workflow" in code

    def test_build_skeleton_has_steps(self) -> None:
        engine = ScaffoldingEngine()
        code = engine.build_skeleton(
            "process orders",
            [
                {"name": "validate", "description": "Validate"},
                {"name": "charge", "description": "Charge"},
            ],
        )
        assert "async def validate" in code
        assert "async def charge" in code
        assert "await ctx.step(validate)" in code
        assert "await ctx.step(charge)" in code

    def test_build_skeleton_has_not_implemented(self) -> None:
        engine = ScaffoldingEngine()
        code = engine.build_skeleton(
            "test",
            [{"name": "s1", "description": "step one"}],
        )
        assert "raise NotImplementedError" in code

    def test_build_skeleton_has_import(self) -> None:
        engine = ScaffoldingEngine()
        code = engine.build_skeleton("test", [])
        assert "from loom import" in code

    def test_step_skeleton_defaults(self) -> None:
        s = StepSkeleton(name="x", description="do x")
        assert s.input_type == "dict"
        assert s.output_type == "dict"
        assert s.body_hint == ""

    def test_workflow_skeleton_defaults(self) -> None:
        w = WorkflowSkeleton(name="w", description="a flow")
        assert w.trigger == "manual"
        assert w.steps == []
        assert w.imports == []


# ---------------------------------------------------------------------------
# Code Validator
# ---------------------------------------------------------------------------


class TestCodeValidator:
    _VALID_CODE = '''
from loom import step, workflow, Context

@step(name="fetch")
async def fetch(ctx: Context) -> dict:
    """Fetch data."""
    return {"data": []}

@workflow(name="my_flow")
async def my_flow(ctx: Context) -> dict:
    """Run the flow."""
    result = await ctx.step(fetch)
    return result
'''

    def test_valid_code_no_issues(self) -> None:
        v = CodeValidator()
        issues = v.validate(self._VALID_CODE)
        assert issues == []

    def test_syntax_error(self) -> None:
        v = CodeValidator()
        issues = v.validate("def foo(\n")
        assert len(issues) == 1
        assert issues[0].category == "syntax"
        assert issues[0].severity == "error"

    def test_missing_workflow_decorator(self) -> None:
        v = CodeValidator()
        code = '''
from loom import step, Context

@step(name="s")
async def s(ctx: Context) -> dict:
    return {}
'''
        issues = v.validate(code)
        cats = [i.category for i in issues]
        assert "structure" in cats
        assert any("@workflow" in i.message for i in issues)

    def test_missing_step_decorator(self) -> None:
        v = CodeValidator()
        code = '''
from loom import workflow, Context

@workflow(name="w")
async def w(ctx: Context) -> dict:
    return {}
'''
        issues = v.validate(code)
        assert any("@step" in i.message for i in issues)
        # Missing step is a warning, not error
        step_issue = next(i for i in issues if "@step" in i.message)
        assert step_issue.severity == "warning"

    def test_bare_io_detected(self) -> None:
        v = CodeValidator()
        code = '''
from loom import workflow, step, Context

@step(name="s")
async def s(ctx: Context) -> dict:
    return {}

@workflow(name="w")
async def w(ctx: Context) -> dict:
    data = requests.get("https://example.com")
    return {}
'''
        issues = v.validate(code)
        assert any(i.category == "structure" and "requests.get" in i.message for i in issues)

    def test_a_name_in_the_wrong_module_says_which_module(self) -> None:
        """Observed: ``from loom import After``, and both repairs wasted.

        ``loom.__all__`` is a curated 36 symbols and every trigger sits
        outside it, so a model told to write ``triggers=[After(minutes=2)]``
        — with every other symbol in the prompt coming from ``loom`` — writes
        the import the rest of the prompt taught it. The message was
        ``'loom' has no attribute 'After'``: true, and nothing the repair loop
        can act on, so it rewrote the import twice and the job ended.
        """
        v = CodeValidator()
        code = """
from loom import Context, After, workflow


@workflow(name="w")
async def w(ctx: Context, input_data=None) -> str:
    return (await ctx.agent("joke")).text()
"""
        messages = [i.message for i in v.validate(code) if i.category == "imports"]

        assert len(messages) == 1
        assert "from loom.triggers import After" in messages[0], messages

    def test_the_corrected_import_is_clean(self) -> None:
        v = CodeValidator()
        code = """
from loom import Context, workflow
from loom.triggers import After


@workflow(name="w", triggers=[After(minutes=2)])
async def w(ctx: Context, input_data=None) -> str:
    return (await ctx.agent("joke")).text()
"""
        assert not [i for i in v.validate(code) if i.category == "imports"]

    def test_a_misspelling_still_wins_over_a_relocation(self) -> None:
        """Two different mistakes; the nearer one is the likelier fix."""
        v = CodeValidator()
        messages = [
            i.message
            for i in v.validate("from loom import Contextt\n")
            if i.category == "imports"
        ]

        assert "did you mean 'Context'?" in messages[0], messages

    def test_a_name_that_exists_nowhere_promises_nothing(self) -> None:
        v = CodeValidator()
        messages = [
            i.message
            for i in v.validate("from loom import CompletelyMadeUpThing\n")
            if i.category == "imports"
        ]

        assert messages
        assert "lives in" not in messages[0], messages
        assert "did you mean" not in messages[0], messages

    def test_a_body_whose_work_is_an_agent_call_needs_no_step(self) -> None:
        """The exemption toolset workflows already had, one call shape over.

        ``tell me a joke`` is one ``ctx.agent``. It is a journaled unit
        already, so there is no @step left to write, and naming one is work
        that does not exist — the nag the toolset exemption exists to avoid.
        """
        v = CodeValidator()
        code = """
from loom import Context, workflow


@workflow(name="w")
async def w(ctx: Context, input_data=None) -> str:
    return (await ctx.agent("tell me a joke")).text()
"""
        messages = [i.message for i in v.validate(code) if i.category == "structure"]

        assert not messages, messages

    def test_a_node_call_counts_the_same_way(self) -> None:
        v = CodeValidator()
        code = """
from loom import Context, workflow


@workflow(name="w")
async def w(ctx: Context, url) -> dict:
    return await ctx.node("io.http_request", {"url": url})
"""
        messages = [i.message for i in v.validate(code) if i.category == "structure"]

        assert not messages, messages

    def test_but_raw_io_in_the_body_still_warns(self) -> None:
        """The exemption must not become a way to skip steps entirely."""
        v = CodeValidator()
        code = """
import requests
from loom import Context, workflow


@workflow(name="w")
async def w(ctx: Context, url) -> dict:
    return requests.get(url).json()
"""
        messages = [i.message for i in v.validate(code) if i.category == "structure"]

        assert any("requests.get" in m for m in messages)
        assert any("No @step" in m for m in messages)

    def test_nondeterministic_call_detected(self) -> None:
        v = CodeValidator()
        code = '''
from loom import workflow, step, Context

@step(name="s")
async def s(ctx: Context) -> dict:
    return {}

@workflow(name="w")
async def w(ctx: Context) -> dict:
    now = datetime.now()
    return {"ts": str(now)}
'''
        issues = v.validate(code)
        assert any(
            i.category == "determinism" and "datetime.now" in i.message
            for i in issues
        )

    def test_missing_import(self) -> None:
        v = CodeValidator()
        code = '''
@workflow(name="w")
async def w(ctx) -> dict:
    return {}
'''
        issues = v.validate(code)
        assert any(i.category == "imports" for i in issues)

    def test_bare_io_constants_populated(self) -> None:
        assert "requests.get" in BARE_IO_CALLS
        assert "open" in BARE_IO_CALLS
        assert len(BARE_IO_CALLS) == 8

    def test_nondeterministic_constants_populated(self) -> None:
        assert "datetime.now" in NONDETERMINISTIC_CALLS
        assert "uuid.uuid4" in NONDETERMINISTIC_CALLS
        assert len(NONDETERMINISTIC_CALLS) == 6

    def test_code_issue_dataclass(self) -> None:
        issue = CodeIssue("syntax", "bad code", "error")
        assert issue.category == "syntax"
        assert issue.message == "bad code"
        assert issue.severity == "error"

    def test_multiple_issues(self) -> None:
        v = CodeValidator()
        # No workflow, no step, no import
        code = '''
async def run():
    pass
'''
        issues = v.validate(code)
        # At least: missing workflow (error) + missing step (warning) + missing import (error)
        assert len(issues) >= 3

    def test_flow_decorator_also_accepted(self) -> None:
        v = CodeValidator()
        code = '''
from loom import step, Context

@step(name="s")
async def s(ctx: Context) -> dict:
    return {}

@flow(name="w")
async def w(ctx: Context) -> dict:
    return {}
'''
        issues = v.validate(code)
        # Should not complain about missing @workflow since @flow is accepted
        assert not any("@workflow" in i.message for i in issues)


# ---------------------------------------------------------------------------
# Schema Simplifier
# ---------------------------------------------------------------------------


class TestSchemaSimplifier:
    def _sample_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results",
                },
                "format": {
                    "type": "string",
                    "description": "Output format",
                    "enum": ["json", "csv", "xml"],
                },
                "options": {
                    "type": "object",
                    "description": "Extra options",
                    "properties": {
                        "verbose": {"type": "boolean"},
                        "timeout": {"type": "integer"},
                    },
                },
            },
        }

    def test_large_returns_original(self) -> None:
        s = SchemaSimplifier()
        schema = self._sample_schema()
        result = s.simplify(schema, "large")
        assert result is schema  # same object, not copy

    def test_medium_inlines_enums(self) -> None:
        s = SchemaSimplifier()
        result = s.simplify(self._sample_schema(), "medium")
        fmt = result["properties"]["format"]
        assert "Allowed values:" in fmt["description"]
        assert "json" in fmt["description"]

    def test_medium_preserves_all_props(self) -> None:
        s = SchemaSimplifier()
        result = s.simplify(self._sample_schema(), "medium")
        assert "query" in result["properties"]
        assert "limit" in result["properties"]
        assert "format" in result["properties"]
        assert "options" in result["properties"]

    def test_small_strips_optional_non_common(self) -> None:
        s = SchemaSimplifier()
        result = s.simplify(self._sample_schema(), "small")
        # "limit" and "format" are optional and not in _COMMON_FIELDS → removed
        assert "limit" not in result["properties"]
        assert "format" not in result["properties"]
        # "query" is required → kept
        assert "query" in result["properties"]

    def test_small_flattens_nested_objects(self) -> None:
        s = SchemaSimplifier()
        schema = {
            "type": "object",
            "required": ["config"],
            "properties": {
                "config": {
                    "type": "object",
                    "description": "Config block",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer"},
                    },
                },
            },
        }
        result = s.simplify(schema, "small")
        cfg = result["properties"]["config"]
        assert cfg["type"] == "string"
        assert "JSON string with keys:" in cfg["description"]

    def test_small_adds_examples(self) -> None:
        s = SchemaSimplifier()
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "The name"},
            },
        }
        result = s.simplify(schema, "small")
        assert "examples" in result["properties"]["name"]

    def test_example_types(self) -> None:
        s = SchemaSimplifier()
        schema = {
            "type": "object",
            "required": ["a", "b", "c", "d"],
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "number"},
                "c": {"type": "boolean"},
                "d": {"type": "array"},
            },
        }
        result = s.simplify(schema, "small")
        props = result["properties"]
        assert props["a"]["examples"] == [1]
        assert props["b"]["examples"] == [1.0]
        assert props["c"]["examples"] == [True]
        assert props["d"]["examples"] == [[]]

    def test_does_not_duplicate_enum_inline(self) -> None:
        s = SchemaSimplifier()
        schema = {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "Mode. Allowed values: a, b",
                    "enum": ["a", "b"],
                },
            },
        }
        result = s.simplify(schema, "medium")
        desc = result["properties"]["mode"]["description"]
        # Should not double-append
        assert desc.count("Allowed values:") == 1

    def test_empty_schema(self) -> None:
        s = SchemaSimplifier()
        result = s.simplify({}, "small")
        assert isinstance(result, dict)

    def test_common_fields_kept_for_small(self) -> None:
        s = SchemaSimplifier()
        schema = {
            "type": "object",
            "required": [],
            "properties": {
                "input": {"type": "string", "description": "The input"},
                "name": {"type": "string", "description": "A name"},
                "verbose": {"type": "boolean", "description": "Verbose"},
            },
        }
        result = s.simplify(schema, "small")
        # "input" and "name" are in _COMMON_FIELDS → kept
        assert "input" in result["properties"]
        assert "name" in result["properties"]
        # "verbose" is not required and not common → stripped
        assert "verbose" not in result["properties"]
