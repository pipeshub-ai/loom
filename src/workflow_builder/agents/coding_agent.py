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

import asyncio
import logging
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from workflow_builder.agents.checks import CheckContext, CheckPipeline
from workflow_builder.agents.smoke import SmokeResult, smoke_run
from workflow_builder.agents.stages import default_stages
from workflow_builder.agents.supervisor import CodeSupervisor, SupervisorVerdict
from workflow_builder.agents.validator import CodeIssue, CodeValidator

logger = logging.getLogger("workflow.coding_agent")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert workflow engineer for the LOOM workflow-builder SDK.
    Your job is to convert a natural-language workflow specification into a
    complete, runnable Python file.

    ## Your process

    1. DISCOVER: Call search_toolsets with keywords from the spec to find
       the integrations it needs.
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
    Start the file with:
        from workflow_builder import Context, Retry, step, workflow

    NEVER import Retry from workflow_builder.steps — it comes from
    workflow_builder directly. Other libraries (httpx, json) go at the top of
    the file or inside step bodies. Never import a store: where the journal
    lives is the host's choice, not the workflow's.

    ### Step functions
    - Decorate with @step for simple steps (no parentheses when no args)
    - Decorate with @step(retry=Retry(max_attempts=3)) for retryable I/O
    - Must be async def
    - Parameters are plain data (str, int, dict, list) — NO ctx parameter
    - Do all I/O inside step functions, never in the workflow body
    - Toolset tools are steps themselves: call them directly inside a
      @step function, not via ctx.step()

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
    Invokes the agent configured on the runtime; no framework import needed.
        result = await ctx.agent("Search for 5 recent AI articles")
        text = result.output  # str

    ### What the file contains
    Steps and workflows, and nothing else: no Runtime at import time, no store,
    no connections. Those are the host's decisions.

    End with a demo block so the file can be run directly:

        if __name__ == "__main__":
            import asyncio

            from workflow_builder import Runtime

            async def main():
                # Store comes from $LOOM_STORE; defaults to in-memory.
                rt = Runtime.from_env()
                result = await rt.run(workflow_fn, input_data)
                print(f"Status: {result.status.value}")
                if result.error:
                    print(f"Error: {result.error.message}")
                print(f"Output: {result.output}")

            asyncio.run(main())

    Keep the Runtime import inside that block.

    ### Return types
    Toolset tools return typed Pydantic models, not plain dicts.
    Use attribute access (result.field), not subscripting (result["field"]).

    ## What the workflow returns

    A markdown string, annotated `-> str`, unless the spec asks otherwise —
    the result is usually read by a person, not indexed into.

    Lead with the answer, then the detail. When the result is empty, say what
    was searched. Keep identifiers verbatim: keys, ids, URLs are what the
    reader acts on. Tables for rows, bullets for a few items, prose for one.

    Return structured data when the spec asks for it — "return a dict of...",
    "output JSON", or output that feeds a system rather than a person.

    ## Resolve every entity before you write code

    A spec names people, places, and states the way a person says them. APIs
    match identifiers and their own configured vocabulary. Filtering on the
    spec's words returns zero rows and no error, which reads as "nothing to do".

    Nothing the spec names may reach a query as raw text. For each one:

    1. **Look it up now** with `call_read_operation`. Search broadly, then
       narrow — a person's phrasing rarely matches a stored name exactly.
       **At most two lookups per entity.** Repeating a search that already
       answered does not make the answer clearer; if two attempts have not
       settled it, it is ambiguous, and rung 3 is where ambiguity goes.
    2. **One clear answer** → put the id in the code with the name it came from
       beside it in a comment. The workflow then does no lookup at run time.
    3. **Ambiguous after those two lookups** → do not call the toolset
       operation directly for it. Emit a `ctx.agent()` step that resolves it at run time,
       handing over the candidates you found and the toolsets it may use.
       Deciding which "xyz work" someone meant is a judgement about language,
       and a model makes it where a query cannot.
    4. **Nothing found** → return an error naming what was tried. Never fall
       back to the raw string; it will silently match nothing.

    The agent-node form, when rung 3 applies:

        resolved = await ctx.agent(
            "Which of these stories, epics is 'xyz work'? "
            "PA-1769 Launch Project 1; PA-1844 Project V2. "
            "Reply with the key alone.",
            toolsets=["<the toolset>"],
        )
        issues = await ctx.step(<search operation>, f"parent = {resolved.output}")

    Enumerated values — statuses, categories, labels — are usually per-account
    configuration, not constants. Read them before filtering on them rather
    than assuming the common defaults.

    Use `ctx.agent()` for judgement of this kind, and for genuine judgement
    tasks (which of these need a reply, draft this). Not for a lookup that
    already has one answer: that re-answers it, differently, on every run.

    ## Only the toolsets listed above exist

    If the spec needs another, do not write code against it — say so, naming
    what the task needs and what is available. Building the part you can, and
    stating what you could not, is fine. An invented import fails on its first
    line, whenever it eventually runs.

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
    smoke: SmokeResult | None = None
    """Outcome of actually executing the code, when smoke testing was enabled."""
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    """Every ``(tool, arguments)`` the agent made, in order.

    A log line is for a person watching; this is for a test asserting that an
    entity was actually resolved rather than assumed."""
    review: SupervisorVerdict | None = None
    """A second model's verdict, when a supervisor was configured."""

    @property
    def is_clean(self) -> bool:
        """True when the code validates, runs, and passes review.

        Static cleanliness alone is a weak claim — code can validate perfectly,
        run perfectly, and still charge a customer twice.
        """
        static_ok = not any(i.severity == "error" for i in self.issues)
        ran_ok = self.smoke is None or self.smoke.ok
        reviewed_ok = self.review is None or not self.review.blocking
        return static_ok and ran_ok and reviewed_ok

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
        Budget for repair rounds only. The turns before any code — search,
        inspect, read docs, resolve entities — come from
        ``max_discovery_turns``, so a spec needing several lookups does not
        starve the repair it then needs.
    max_discovery_turns:
        Turns allowed for discovery, inspection, entity resolution, writing,
        and validation. Raise it for a spec that names many entities; lower it
        to cap a run that wanders.
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
        max_discovery_turns: int = 20,
        tool_docs: list[str] | None = None,
        tool_registry: object | None = None,
        instructions: str | None = None,
        extra_instructions: str = "",
        allowed_packages: Iterable[str] | None = None,
        smoke_test: bool = True,
        smoke_input: Any = None,
        supervisor: CodeSupervisor | None = None,
        stages: list[Any] | None = None,
    ) -> None:
        self._model = model
        self._max_repair = max_repair_attempts
        self._max_discovery = max_discovery_turns
        """Turns allowed before repair: search, inspect, read docs, resolve the
        entities the spec names, write, validate. Separate from the repair
        budget because they are separate activities — folding them together
        means a spec naming three people starves the repair it then needs."""
        self._allowed_packages = None if allowed_packages is None else set(allowed_packages)
        available: list[str] | None = None
        if tool_registry is not None:
            try:
                available = list(tool_registry.list_toolsets())
            except Exception:  # a fake registry in a test need not support it
                available = None
        self._validator = CodeValidator(
            allowed_packages=self._allowed_packages,
            available_toolsets=available,
        )
        self._tool_docs = tool_docs or []
        self._tool_registry = tool_registry
        self._instructions = instructions
        self._extra_instructions = extra_instructions
        self._smoke_test = smoke_test
        """Execute the generated workflow once before returning it. On by
        default: static validation cannot tell working code from code that
        merely parses."""
        self._smoke_input = smoke_input
        self._supervisor = supervisor
        self._stages = stages
        self._pipeline = CheckPipeline(
            stages
            if stages is not None
            else default_stages(supervisor=None, smoke=smoke_test)
        )
        """Verification stages, cheapest first. Passing ``stages=`` replaces the
        default arrangement outright — a caller wanting a linter but no smoke
        run, or an extra rule of their own, composes the list rather than
        arguing with a flag."""
        """Optional second model that reviews the finished code. Off by default:
        it costs an extra call, and it is only worth it when the workflow does
        something you would want a colleague to look at."""

    def build_system_prompt(self) -> str:
        """Compose the full system prompt.

        Assembled in four parts, each independently controllable: the base
        instructions (``instructions=`` replaces :data:`DEFAULT_SYSTEM_PROMPT`
        outright), the environment's package allowlist, auto-generated toolset
        documentation from the registry, and finally ``extra_instructions``
        — house style, forbidden patterns, a preferred store.

        Public so callers (e.g. a CLI) can display what will be sent
        to the model before invoking ``generate()``.
        """
        parts = [self._instructions if self._instructions is not None else DEFAULT_SYSTEM_PROMPT]

        if self._allowed_packages is not None:
            listed = ", ".join(sorted(self._allowed_packages)) or "none"
            parts.append(
                "## Available packages\n\n"
                "Besides the Python standard library and workflow_builder, only "
                f"these third-party packages are installed: {listed}.\n"
                "Do NOT import anything else — validate_code will reject it."
            )

        # Index cards only. The operations are one tool call away, and pasting
        # every one of them here would make the prompt grow with the number of
        # integrations installed rather than with the task being asked for.
        if self._tool_registry is not None:
            desc = self._tool_registry.describe(detail="index")
            if desc:
                parts.append(desc)

        # Backward compat: append hand-written tool_docs if provided
        if self._tool_docs:
            parts.append(
                "## Additional Tool Documentation\n\n" + "\n\n".join(self._tool_docs)
            )

        if self._extra_instructions:
            parts.append(self._extra_instructions)

        return "\n\n".join(parts)

    # Keep old private name as alias
    _build_system_prompt = build_system_prompt

    async def _repair_until_it_runs(
        self, agent: Any, code: str
    ) -> tuple[str, SmokeResult, int]:
        """Execute the code, and on failure hand the traceback back to the model.

        Returns the best code produced, its smoke result, and how many repair
        rounds were spent. Gives up after ``max_repair_attempts`` rather than
        looping — a model that cannot fix a traceback in two tries is usually
        missing context that another round will not supply.
        """
        rounds = 0
        smoke = await asyncio.to_thread(smoke_run, code, self._smoke_input)
        logger.info("smoke | %s", _summarise_smoke(smoke))

        # A failure the sandbox caused is not a failure to repair. Asking the
        # model to fix a 401 it cannot fix invites it to delete the code that
        # needed the credential, and the gutted result passes.
        if smoke.environmental:
            logger.info("smoke | environmental failure, not repairing: %s", smoke.error[:120])
            return code, smoke, rounds

        while not smoke.ok and rounds < self._max_repair:
            rounds += 1
            logger.info("smoke_repair | round=%d %s", rounds, smoke.error[:120])

            try:
                retry = await agent(smoke.as_feedback(code))
            except Exception as exc:
                # The repair shares the agent's turn budget with the generation
                # that preceded it, so a long discovery phase can leave nothing
                # for a repair round. Keep the code already produced and report
                # the smoke result: raising here would throw away a working
                # candidate because the *fix* could not be attempted.
                logger.info("smoke_repair | giving up: %s", exc)
                break

            candidate = _extract_code(
                retry.output.code
                if isinstance(retry.output, CodingOutput)
                else str(retry.output or "")
            )
            if not candidate or candidate == code:
                # The model did not change anything; another round will not help.
                break

            code = candidate
            smoke = await asyncio.to_thread(smoke_run, code, self._smoke_input)
            logger.info("smoke | %s", _summarise_smoke(smoke))

        return code, smoke, rounds

    async def _revise_until_approved(
        self, agent: Any, spec: str, code: str
    ) -> tuple[str, SupervisorVerdict, int]:
        """Review, revise, re-review — up to the repair budget.

        Returns the best code, the final verdict, and how many revision rounds
        were spent. A revision that fails to run is discarded and the previous
        code kept: a reviewer's opinion does not outrank working code.
        """
        assert self._supervisor is not None
        rounds = 0
        verdict = await self._supervisor.review(spec, code)

        while verdict.blocking and rounds < self._max_repair:
            rounds += 1
            logger.info(
                "review_repair | round=%d findings=%d", rounds, len(verdict.findings)
            )

            revised = await agent(verdict.as_feedback(code))
            candidate = _extract_code(
                revised.output.code
                if isinstance(revised.output, CodingOutput)
                else str(revised.output or "")
            )
            if not candidate or candidate == code:
                break

            if self._smoke_test:
                check = await asyncio.to_thread(smoke_run, candidate, self._smoke_input)
                if not check.ok:
                    logger.info("review_repair | revision broke the run, keeping prior")
                    break

            code = candidate
            verdict = await self._supervisor.review(spec, code)

        return code, verdict, rounds

    def _check_context(self, spec: str) -> CheckContext:
        """Assemble what the stages need, including fakes for the toolsets."""
        toolsets: set[str] | None = None
        fakes: list[tuple[str, str]] = []
        if self._tool_registry is not None:
            try:
                toolsets = set(self._tool_registry.list_toolsets())
                for toolset_id in sorted(toolsets):
                    manifest = self._tool_registry.get(toolset_id)
                    module = getattr(manifest, "tools_module", "")
                    if module:
                        fakes.append((module, _manifest_path(manifest)))
            except Exception:  # a fake registry in a test need not support it
                toolsets = None

        return CheckContext(
            workflow_input=self._smoke_input,
            allowed_packages=self._allowed_packages,
            available_toolsets=toolsets,
            fakes=[f for f in fakes if f[1]],
            spec=spec,
        )

    async def _repair_from(
        self, agent: Any, code: str, report: Any, context: CheckContext
    ) -> tuple[str, int]:
        """Feed the pipeline's errors back to the model until they clear.

        One loop for every kind of failure. A type error and a traceback are
        both things the model can fix, and routing them through separate paths
        is how one of them ends up with no repair at all.
        """
        rounds = 0
        while report.errors and rounds < self._max_repair:
            if _is_unrepairable(report):
                break
            rounds += 1
            try:
                retry = await agent(_repair_prompt(report, code, context.spec))
            except Exception as exc:
                # The repair shares the agent's turn budget with the generation
                # before it. Keep the code rather than lose it to a fix that
                # could not be attempted.
                logger.info("repair | giving up: %s", exc)
                break

            candidate = _extract_code(
                retry.output.code
                if isinstance(retry.output, CodingOutput)
                else str(retry.output or "")
            )
            if not candidate or candidate == code:
                break

            attempt = await self._pipeline.run(candidate, context)
            if len(attempt.errors) >= len(report.errors) and not attempt.ok:
                # No better, and possibly prose where code should be. Keep what
                # we had: a repair round that regresses is worse than none.
                logger.info(
                    "repair | round %d did not improve (%d -> %d errors), keeping prior",
                    rounds,
                    len(report.errors),
                    len(attempt.errors),
                )
                break

            code, report = candidate, attempt

        return code, rounds

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

        max_turns = self._max_discovery + self._max_repair
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
            tools=build_coding_tools(
                registry=self._tool_registry, validator=self._validator
            ),
            output_type=CodingOutput,
            model_settings=ModelSettings(temperature=0.2),
            limits=UsageLimits(max_turns=max_turns),
        )

        try:
            result = await agent(spec)
        except Exception as exc:
            # Never propagate. A caller asked for code and is entitled to an
            # answer it can act on — an exception discards whatever the run
            # learned and gives them a stack trace instead of a reason.
            logger.info("generate | agent loop ended: %s", exc)
            return CodingResult(
                code="",
                issues=[
                    CodeIssue(
                        "unsupported",
                        f"The agent did not produce code: {exc}. If it ran out "
                        "of turns, raise max_discovery_turns or narrow the "
                        "spec — each entity it has to resolve costs a turn.",
                        "error",
                    )
                ],
                model_used=getattr(self._model, "model_name", ""),
            )

        # Extract code from structured output
        explanation = ""
        if isinstance(result.output, CodingOutput):
            code = result.output.code
            explanation = result.output.explanation
        elif isinstance(result.output, dict):
            code = result.output.get("code", "")
            explanation = str(result.output.get("explanation", ""))
        else:
            code = str(result.output or "")

        code = _extract_code(code)

        # No code is a refusal, not a broken generation — most often because the
        # task needs an integration this environment does not have. Reporting it
        # as "no @workflow found" and "missing import" buries the one thing the
        # caller needs to know under the symptoms of an empty file.
        if not code.strip():
            return CodingResult(
                code="",
                issues=[
                    CodeIssue(
                        "unsupported",
                        explanation.strip()
                        or "The agent returned no code and gave no reason.",
                        "error",
                    )
                ],
                model_used=getattr(self._model, "model_name", ""),
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )

        # One pipeline, cheapest stage first, stopping at the first blocking
        # failure. Repair is driven by whatever it reports, so a type error and
        # a traceback reach the model by the same path.
        context = self._check_context(spec)
        report = await self._pipeline.run(code, context)
        code, rounds = await self._repair_from(agent, code, report, context)
        report = await self._pipeline.run(code, context) if rounds else report

        issues = list(report.issues)
        errors = report.errors
        smoke = report.detail("smoke")

        # A second opinion, once the code runs. Code that validates and executes
        # can still do the wrong thing, and the author is the worst judge of it.
        review: SupervisorVerdict | None = None
        review_rounds = 0
        if self._supervisor is not None and not errors:
            code, review, review_rounds = await self._revise_until_approved(
                agent, spec, code
            )
            issues.extend(
                CodeIssue("review", f"{f.category}: {f.message}", f.severity)
                for f in review.findings
            )

        logger.info("checks | %s", report.summary)
        smoke_rounds = rounds

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
        repair_attempts = max(0, validate_calls - 1) + smoke_rounds + review_rounds

        return CodingResult(
            code=code,
            issues=issues,
            tool_calls=[(c.name, _brief_args(c.arguments)) for c in result.tool_calls],
            smoke=smoke,
            review=review,
            repair_attempts=repair_attempts,
            model_used=self._model.model_name,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _brief_args(arguments: Any) -> str:
    """Arguments as one short line, for a result a person may print."""
    import json

    text = " ".join(json.dumps(arguments, default=str).split())
    return text if len(text) <= 160 else text[:160] + "…"


def trace_tool_calls(level: int = logging.DEBUG) -> None:
    """Turn on per-tool-call logging for the agent runtime.

    A one-liner because the alternative — knowing which of several loggers
    carries tool traffic, and that it is separate from the agent's own — is
    exactly the thing someone debugging a loop does not yet know.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("  tool | %(message)s"))
    tools = logging.getLogger("workflow.agent.tools")
    tools.setLevel(level)
    tools.addHandler(handler)


def _manifest_path(manifest: Any) -> str:
    """Import path of a manifest object, so a subprocess can find it again.

    Derived from where the object actually lives rather than a naming
    convention, so a toolset outside this package works the same way.
    """
    module = type(manifest).__module__
    for candidate in (manifest.tools_module or "").rsplit(".", 1)[:1]:
        module = f"{candidate}.manifest"
        break
    import importlib

    try:
        found = importlib.import_module(module)
    except ImportError:
        return ""
    for name in dir(found):
        if getattr(found, name, None) is manifest:
            return f"{module}.{name}"
    return ""


def _is_unrepairable(report: Any) -> bool:
    """True when the errors are about the environment, not the code.

    Asking for a repair the model cannot make invites it to delete whatever the
    error mentioned — the fastest way to make the message go away, and the
    wrong one.
    """
    categories = {issue.category for issue in report.errors}
    return bool(categories) and categories <= {"toolset"}


def _repair_prompt(report: Any, code: str, spec: str = "") -> str:
    """Phrase every stage's findings as one repair instruction.

    Carries both the spec and the code. The agent is ephemeral — each round is
    a fresh conversation — so a round that sends only the errors asks the model
    to fix something it can neither see nor remember the purpose of, and it
    will reasonably ask what it was supposed to be building.
    """
    lines = [
        "The workflow you generated did not pass verification. Return the "
        "complete corrected file, and nothing else — no questions, no prose.",
        "",
        "## Problems",
    ]
    for issue in report.errors:
        lines.append(f"- [{issue.category}] {issue.message}")

    smoke = report.detail("smoke")
    trace = getattr(smoke, "traceback", "") if smoke is not None else ""
    if trace:
        lines += ["", "## Traceback", trace]

    if spec:
        lines += ["", "## What the workflow must do", spec]
    lines += ["", "## The code that failed", "```python", code, "```"]
    return "\n".join(lines)



def _summarise_smoke(result: SmokeResult) -> str:
    """One line for the log."""
    return f"{'ok' if result.ok else 'failed'} at {result.phase}"


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
