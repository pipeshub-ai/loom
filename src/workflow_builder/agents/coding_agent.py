"""Workflow Coding Agent — ReAct loop powered by BuiltInAgentRuntime.

Takes a natural-language workflow specification and produces a ready-to-run
Python file using the workflow-builder SDK (@workflow, @step, ctx.*).

The agent operates as a proper ReAct loop:
1. DISCOVER — calls ``search_toolsets`` to find relevant integrations
2. INSPECT  — calls ``show_toolset`` / ``get_tool_contract`` for schemas
3. DOCS     — calls ``get_tool_docs`` for import paths and examples
4. GENERATE — writes complete Python workflow code
5. VALIDATE — calls ``validate_code`` for AST-based checking
6. FIX      — if errors, corrects and re-validates
7. SUBMIT   — returns code via ``final_output`` structured output

This reuses the existing ``BuiltInAgentRuntime`` turn loop — no duplication
of tool-calling, message management, or structured output handling.
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from workflow_builder.agents.validator import CodeIssue, CodeValidator

logger = logging.getLogger("workflow.coding_agent")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert workflow engineer for the LOOM workflow-builder SDK.
    Your job is to convert a natural-language workflow specification into a
    complete, runnable Python file.

    ## Your process

    1. DISCOVER: Call search_toolsets with keywords from the spec to find
       relevant integrations (e.g. "jira", "slack", "github").
    2. INSPECT: Call show_toolset to see available operations, then
       get_tool_contract for specific operations you need.
    3. DOCS: Call get_tool_docs to get exact import paths, function
       signatures, and usage examples for the toolset.
    4. GENERATE: Write the complete Python workflow code following the
       SDK rules below.
    5. VALIDATE: Call validate_code with your generated code.
    6. FIX: If validation finds errors, fix the code and validate again.
    7. SUBMIT: When the code validates cleanly, use the final_output
       tool to return the code and a brief explanation.

    If the spec does not mention any external integration, skip steps 1-3
    and go directly to code generation.

    ## SDK rules you MUST follow

    ### Imports — EXACT paths, no variations
    Always start the file with exactly these imports:
        from workflow_builder import Context, Retry, Runtime, step, workflow
        from workflow_builder.state.memory import MemoryStore

    NEVER import Retry from workflow_builder.steps — it comes from
    workflow_builder directly. Import extra libraries (httpx, json, etc.)
    at the top of the file or inside step bodies.

    ### Step functions
    - Decorate with @step for simple steps (no parentheses when no args)
    - Decorate with @step(retry=Retry(max_attempts=3)) for retryable I/O
    - Must be async def
    - Parameters are plain data (str, int, dict, list) — NO ctx parameter
    - Do all I/O inside step functions, never in the workflow body
    - When using toolset tools (e.g. jira_search_issues), call them
      directly inside a @step function — they ARE steps themselves

    ### Workflow function
    - Decorate with @workflow(name="<descriptive_name>")
    - Signature: async def my_workflow(ctx: Context, input_data) -> ReturnType
      where input_data is whatever the caller passes to rt.run()
    - NEVER use ctx.input — the input arrives as the second parameter
    - Call steps via: result = await ctx.step(step_fn, arg1, arg2)
    - For parallel steps: results = await ctx.gather(
          ctx.step(a, x), ctx.step(b, y))
    - For durable sleep: await ctx.sleep(timedelta(minutes=5))
    - For AI agent calls: result = await ctx.agent("prompt text")
      result.output is the agent's text response (str)
    - NEVER call datetime.now(), uuid.uuid4(), random.*() directly
    - NEVER do I/O in the workflow body

    ### ctx.agent() — AI agent calls
    Use ctx.agent(prompt) to invoke the AI agent configured on the runtime.
    The agent has access to tools (web search, web fetch, etc.) and decides
    which ones to call based on your prompt. You do NOT need to import any
    agent framework. Example:
        result = await ctx.agent("Search for 5 recent AI articles")
        text = result.output  # the agent's response (str)

    ### Entry point
    Always include a runnable main() at the bottom:

        async def main():
            rt = Runtime(store=MemoryStore())
            result = await rt.run(workflow_fn, input_data)
            print(f"Status: {result.status.value}")
            print(f"Output: {result.output}")

        if __name__ == "__main__":
            import asyncio
            asyncio.run(main())

    ### Return types
    Toolset tools return typed Pydantic models, not plain dicts.
    Use attribute access (issue.key, issue.status, project.name),
    not dict subscripting (issue["key"]).

    ## Output format
    Return the code via the final_output tool. The 'code' field must
    contain the complete Python source — no markdown fences.
""")


# ---------------------------------------------------------------------------
# Structured output model for the final_output tool
# ---------------------------------------------------------------------------


class CodingOutput(BaseModel):
    """Structured output from the workflow coding agent."""

    code: str = Field(
        description="Complete, runnable Python source code. No markdown fences."
    )
    explanation: str = Field(
        default="",
        description="Brief explanation of the design choices.",
    )


# ---------------------------------------------------------------------------
# Result dataclass (public API — unchanged)
# ---------------------------------------------------------------------------


@dataclass
class CodingResult:
    """Result from the Workflow Coding Agent."""

    code: str
    """The generated (and validated) Python source."""
    issues: list[CodeIssue] = field(default_factory=list)
    """Any remaining issues after validation."""
    repair_attempts: int = 0
    """How many self-correction rounds were needed."""
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def is_clean(self) -> bool:
        """True when no error-severity issues remain."""
        return not any(i.severity == "error" for i in self.issues)

    def save(self, path: str) -> None:
        """Write the generated code to *path*."""
        with open(path, "w") as f:
            f.write(self.code)
        print(f"Saved to {path}")


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class WorkflowCodingAgent:
    """LLM-powered agent that authors LOOM workflow code.

    Uses a ReAct loop via ``BuiltInAgentRuntime`` to discover toolsets,
    inspect schemas, generate code, validate it, and self-correct.

    Parameters
    ----------
    model:
        A ``ModelProvider`` instance (e.g. ``AnthropicProvider()``).
    max_repair_attempts:
        Budget for the ReAct loop. Translated to
        ``max_turns = max_repair_attempts + 6`` to account for
        discovery/inspection turns.
    tool_docs:
        Optional pre-loaded tool documentation strings.
        When provided, the agent can skip discovery and go straight
        to code generation. Backward compatible with the old API.
    """

    def __init__(
        self,
        model: object,
        *,
        max_repair_attempts: int = 2,
        tool_docs: list[str] | None = None,
        tool_registry: object | None = None,
    ) -> None:
        self._model = model
        self._max_repair = max_repair_attempts
        self._validator = CodeValidator()
        self._tool_docs = tool_docs or []
        self._tool_registry = tool_registry

    def build_system_prompt(self) -> str:
        """Compose the full system prompt.

        Tool documentation is auto-generated from the ``ToolsetRegistry``
        if provided. Falls back to ``tool_docs`` strings for backward compat.

        Public so callers (e.g. a CLI) can display what will be sent
        to the model before invoking ``generate()``.
        """
        prompt = _SYSTEM_PROMPT

        # Prefer auto-generated docs from the registry
        if self._tool_registry is not None:
            desc = self._tool_registry.describe()
            if desc:
                prompt += "\n\n" + desc

        # Backward compat: append hand-written tool_docs if provided
        if self._tool_docs:
            sections = "\n\n".join(self._tool_docs)
            prompt += (
                "\n\n## Additional Tool Documentation\n\n"
                + sections
            )
        return prompt

    # Keep old private name as alias
    _build_system_prompt = build_system_prompt

    async def generate(self, spec: str) -> CodingResult:
        """Generate a workflow from a natural-language *spec*.

        Constructs an ``Agent`` with ReAct tools and runs it via
        ``BuiltInAgentRuntime``. The agent discovers toolsets,
        generates code, validates it, and returns via structured output.

        Parameters
        ----------
        spec:
            Plain-English description of what the workflow should do.

        Returns
        -------
        CodingResult
            Contains the generated code, remaining issues, and usage.
        """
        from workflow_builder.agents.agent import Agent
        from workflow_builder.agents.coding_tools import build_coding_tools
        from workflow_builder.agents.limits import UsageLimits
        from workflow_builder.agents.models import ModelSettings

        max_turns = self._max_repair + 6
        logger.info(
            "generate | model=%s max_turns=%d pre_loaded_docs=%d",
            self._model.model_name,
            max_turns,
            len(self._tool_docs),
        )

        agent = Agent(
            name="workflow_coder",
            instructions=self.build_system_prompt(),
            model=self._model,
            tools=build_coding_tools(),
            output_type=CodingOutput,
            model_settings=ModelSettings(temperature=0.2),
            limits=UsageLimits(max_turns=max_turns),
        )

        result = await agent(spec)

        # Extract code from structured output
        if isinstance(result.output, CodingOutput):
            code = result.output.code
        elif isinstance(result.output, dict):
            code = result.output.get("code", "")
        else:
            code = str(result.output or "")

        code = _extract_code(code)

        # Final validation pass
        issues = self._validator.validate(code)
        errors = [i for i in issues if i.severity == "error"]

        logger.info(
            "generate_complete | turns=%d tool_calls=%d "
            "errors=%d tokens=%d",
            result.turns,
            len(result.tool_calls),
            len(errors),
            result.usage.input_tokens + result.usage.output_tokens,
        )

        # Count validate_code calls as repair attempts
        validate_calls = sum(
            1 for tc in result.tool_calls if tc.name == "validate_code"
        )
        repair_attempts = max(0, validate_calls - 1)

        return CodingResult(
            code=code,
            issues=issues,
            repair_attempts=repair_attempts,
            model_used=self._model.model_name,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_code(text: str) -> str:
    """Strip markdown code fences if the model wrapped the output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:] if lines[0].startswith("```") else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return text
