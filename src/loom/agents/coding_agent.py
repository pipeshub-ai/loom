"""Workflow Coding Agent — ReAct loop powered by BuiltInAgentRuntime.

Takes a natural-language workflow specification and produces a ready-to-run
Python file using the loomsdk SDK (@workflow, @step, ctx.*).

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
import json
import logging
import re
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from loom.agents.checks import CheckContext, CheckPipeline
from loom.agents.generation import Asking
from loom.agents.smoke import SmokeResult, smoke_run
from loom.agents.stages import ADVISORY_STAGES, default_stages
from loom.agents.supervisor import CodeSupervisor, SupervisorVerdict
from loom.agents.tidy import tidy
from loom.agents.validator import CodeIssue, CodeValidator

if TYPE_CHECKING:
    # Imported for annotation only. `object` was used here to keep the
    # module import-light, and the cost was that every attribute reached
    # through these — `list_toolsets`, `describe`, `prompt_block`,
    # `model_name` — went unchecked, including the `model=` handed to
    # `Agent`, whose parameter is not `object` at all.
    from loom.agents.models import ModelProvider
    from loom.agents.tool_registry import ToolsetRegistry
    from loom.nodes.catalog import NodeCatalog

logger = logging.getLogger("workflow.coding_agent")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert workflow engineer for the LOOM loomsdk SDK.
    Your job is to convert a natural-language workflow specification into a
    complete, runnable Python file.

    ## Your process

    1. DISCOVER: search_toolsets for keywords; search_operations when you know
       the task, not the operation. **Both empty means no integration — a
       normal answer.** Write it as plain Python in a `@step`, or drive the
       page.
    2. INSPECT: show_toolset for operations, get_tool_contract for the ones
       you need.
    3. DOCS: get_tool_docs for exact imports and signatures.
    4. RESOLVE: look up every entity the spec names and every enumerated
       value it filters on — see "Resolve every entity".
    5. PLAN: split the spec into nodes; decide per node whether code can do
       it or it needs judgement — see "Code or judgement". Return this in
       the final_output `plan` field.
    6. GENERATE: write the workflow, following the SDK rules below.
    7. VALIDATE: validate_code, correct what it reports, validate again.
    8. SUBMIT: final_output with the code, the plan, and a brief explanation.

    RESOLVE and PLAN come before GENERATE, always. Code written first is code
    built around a guess, and the guess is invisible in the finished file.

    Skip 1-3 when the spec needs no external integration.

    ## SDK rules you MUST follow

    ### Imports — EXACT paths, no variations
    Start the file with:
        from loom import Context, Retry, step, workflow

    NEVER import Retry from loom.steps. Other libraries (httpx,
    json) go at the top of the file or inside step bodies. Never import a
    store: where the journal lives is the host's choice, not the workflow's.

    ### Step functions
    - @step, or @step(retry=Retry(max_attempts=3)) for retryable I/O
    - Must be async def
    - Parameters are plain data (str, int, dict, list) — NO ctx parameter
    - Do all I/O inside step functions, never in the workflow body
    - Toolset tools ARE steps: call them with ctx.step(tool, ...) from the
      workflow body. Awaiting one inside another @step skips the journal, its
      retry policy, and the grant check

    ### Workflow function
    - Decorate with @workflow(name="<descriptive_name>")
    - Signature: async def my_workflow(ctx: Context, input_data) -> ReturnType
      where input_data is whatever the caller passes to rt.run()
    - NEVER use ctx.input — the input arrives as the second parameter
    - Call steps via: result = await ctx.step(step_fn, arg1, arg2)
    - For parallel steps: results = await ctx.gather(
          ctx.step(a, x), ctx.step(b, y))
    - For durable sleep: await ctx.sleep(timedelta(minutes=5))
    - For files: an Attachment carries bytes plus filename and mime.
      Stage then commit so a run can accumulate files before publishing:

        export = await ctx.step(gmail_get_attachment, message_id, att_id)
        await ctx.stage_artifact("invoice.pdf", export)
        version = await ctx.commit_staged("invoice.pdf")

      ctx.put_artifact(name, data) publishes immediately; ctx.artifact_url(name)
      mints a short-lived URL — never journal it. Never re-download an
      offloaded ref.
    - For AI agent calls: result = await ctx.agent("prompt text")
      result.text() is the reply as a string; result.output is it typed
    - NEVER call datetime.now(), uuid.uuid4(), random.*() directly

    ### What the file contains
    Steps and workflows, and nothing else: no Runtime at import time, no store,
    no connections. Those are the host's decisions.

    End with a demo block so the file runs directly — imports inside it, store
    from the environment:

        if __name__ == "__main__":
            import asyncio

            from loom import Runtime

            async def main():
                result = await Runtime.from_env().run(workflow_fn, input_data)
                print(f"Status: {result.status.value}")
                if result.error:
                    print(f"Error: {result.error.message}")
                print(f"Output: {result.output}")

            asyncio.run(main())

    ### What a toolset call hands back
    Typed Pydantic models, not dicts — attribute access (`result.field`), not
    subscripting.

    **The result is the data.** A field missing from it is asked for on the
    *read* — its fields/custom-fields argument — never from a model.

    A search or list returns at most its `max_results`/`limit`; the toolset
    pages the API to fill it. The result is a list that also knows whether it
    saw everything, and that survives being returned from a step:

        found = await ctx.step(<search operation>, query, max_results=200)
        if not found.complete:
            header = f"showing {found.summary()}"   # "200 of 312"

    Spec says **all / every / the full list** → raise the limit and report the
    coverage. A count over a capped fetch looks complete and is not. For a set
    with no natural bound — a mailbox, a log — bound the *window*, not the row
    count: a date range or status, watermark in `ctx.state`, run repeatedly. No
    read takes `cursor=`, and one call for 50,000 rows is one journal entry a
    crash refetches whole.

    **Filter in the query, not after it** — JQL, `q=`, `$filter`, SOQL all take
    one. Fetching everything and keeping six rows pages an unbounded amount of
    someone else's data, and a comprehension over a paged result is a plain
    list, so `.complete` is gone and a truncated fetch reads as complete. Use
    `.filtered(...)`/`.mapped(...)` when a predicate cannot go server-side.

    ## What the workflow returns

    A markdown string, annotated `-> str`, unless the spec asks otherwise —
    the result is usually read by a person, not indexed into. Lead with the
    answer, then the detail. When the result is empty, say what was searched.
    Keep identifiers verbatim: keys, ids, URLs are what the reader acts on.
    Tables for rows, bullets for a few, prose for one.

    Return structured data when the spec asks for it — "return a dict of...",
    "output JSON", or output that feeds a system rather than a person.

    ## Code or judgement: decide per node, before writing any

    For each node ask: **can I write a rule today that is right for every
    input the spec allows?**

    **Yes → `@step`.** Fetching, filtering on a resolved id, arithmetic,
    formatting, sorting, sending.

    **No, or unsure → `ctx.agent()`. When in doubt, use the agent.** A wrong
    rule fails silently. Use it for open-ended language (summarise, draft,
    rewrite), a judgement with no stated threshold ("needs attention",
    "important", "relevant"), classification the data does not carry
    (sentiment, intent, priority), and anything still ambiguous after
    resolution.

    The tell for a rule you should not write: a keyword list, a regex over
    prose, or a threshold you picked. If you invented the constant, the spec
    did not give you the rule — `if "urgent" in subject.lower()` is a guess
    wearing the clothes of logic.

    Part rule, part judgement → split it: a `@step` that fetches and narrows,
    a `ctx.agent()` that judges what is left, a `@step` that composes.

    **A model call runs on every run**, costs a request each time, and can
    drop, reorder or invent rows the code already holds. So it never renders
    the answer and never fetches: a return value coming out of `ctx.agent()`
    where the spec asked only for data is this mistake.

    **The exception:** a lookup with one right answer — a person, a project, a
    status name — is a query, not judgement. Resolve it below, at authoring
    time. "When in doubt use the agent" is about whether a *rule* can express
    the task, not about facts you can go and find. Handing one to an agent
    re-answers it, differently, on every run.

    ## Resolve every entity before you write code

    A spec names people and states the way a person says them; APIs match
    identifiers. Filtering on the spec's words returns zero rows and no error,
    which reads as "nothing to do". Nothing the spec names may reach a query as
    raw text. For each one:

    1. **Look it up now** with `call_read_operation`. A name lives in a
       *namespace* and a service has several — container (project, space,
       board), grouping (epic, label, component, tag), person, state, field.
       **One lookup per namespace**: an empty answer says nothing about the
       next namespace, so two of them are not a verdict. Stop when one answers
       or they run out; repeating a search that answered does not help.
    2. **One clear answer** → put the id in the code, the name beside it in a
       comment. The workflow then does no lookup at run time.
    3. **Two candidates, or two namespaces that both answer** → do not call
       the toolset operation directly for it. Emit a `ctx.agent()` step that
       resolves it at run time, handing over the candidates you found and the
       toolsets it may use. Deciding which "xyz work" someone meant is a
       judgement about language, and a model makes it where a query cannot.
    4. **No namespace has that name** → it is subject matter, not a thing,
       and matching text is then honest: keep it, and say so in what the
       workflow returns. Never fall back to the raw string as an identifier —
       `project = "<the word>"` silently matches nothing and reports "none".

    A **fuzzy text search is not a resolution**. `text ~ "..."`, `contains`,
    `LIKE` — the spec's own words in front of a match operator is rung 4
    claimed without doing rung 1: it looks like searching and it is guessing.
    Nor may you quietly fix a spelling: "sas" becoming "saas" is a guess about
    what someone meant, and guesses belong in rung 3, made out loud with the
    candidates in view.

    The agent-node form, when rung 3 applies:

        resolved = await ctx.agent(
            "Which of these is 'xyz work'? PROJ-11 Launch; PROJ-42 V2. "
            "Reply with the key alone.",
            toolsets=["<the toolset>"],
        )
        issues = await ctx.step(<search operation>, f"parent = {resolved.output}")

    Enumerated values and identifiers — statuses, labels,
    `customfield_10016`, `C024BE91L` — are per-account configuration, not
    constants. Read them before filtering: one you did not look up is right
    on another account and silently wrong here.

    ## Driving a web page

    Address a control the way a person reads it — `{"role": "textbox", "name":
    "Email"}` — and write targets from what `navigate` reports, not from what
    the page probably has. A selector is right on the render you copied it from
    and then matches nothing, silently. Two matches raise: pass `ordinal` when
    a page repeats a control.

    `effect` is `read` to fill or move, `write` to submit, `destructive` to
    delete; it cannot be inferred from the target. Reading a page means a
    `write` needs `ctx.node("human.approval", ...)` first — and an approval
    parks the run, so open with `scope="durable"`, pass `session` to every
    later call, and finish with `browser.close`.

    ## Only the toolsets listed above exist

    If the spec needs another, do not write code against it — say so, naming
    what the task needs and what is available. Building the part you can and
    saying what you could not is fine. An invented import fails on its first
    line, whenever it runs.

    ## Output format
    Return the code via the final_output tool. The 'code' field must
    contain the complete Python source — no markdown fences. The 'plan' field
    lists each node with the choice you made and why, so the choice can be
    reviewed rather than inferred from the finished code.
""")


ASK_USER_INSTRUCTIONS = """\
## Asking the user

You are talking to the person who wrote the spec. When a decision is theirs to
make and the spec does not make it, use `ask_user` rather than choosing for
them. Do not ask to confirm what the spec already states, do not ask which
toolset to use, and do not ask anything a lookup would answer — ask, and you
have spent their attention on something you could have found. Keep questions to
one sentence. Prefer `input_type="select"` with `options` whenever the choices
are known, because a list is answerable in a keystroke and free text is not.

**This changes rung 3 of the resolution ladder.** Two candidates from a lookup
is the case it was written for, and there are two of those:

- The ambiguous name **came from the spec** — "the saas epic", and the resolver
  returned two. That is a question about what the requester meant, it has one
  answer, and that answer does not change between runs. **Ask now** with the
  candidates as `options`, then bake the chosen id in as rung 2 says, with the
  name beside it in a comment. Emitting a `ctx.agent()` step here puts a model
  call into every run to re-answer a question a person could settle once, and
  the model answering it has less to go on than they do.
- The ambiguous value **arrives as workflow input** — a name the run is handed,
  different each time. Nobody can answer that at authoring time. That is what
  `ctx.agent()` is for, and rung 3 stands unchanged.

**Ask once.** Gather the decisions you need and send up to four questions in a
single `ask_user` call. One per turn costs a round trip each and pulls them back
to the terminal four times for what is one conversation.

**Make each one answerable in a keystroke.** Prefer `kind: "select"` with
`choices`, mark the option you would pick `recommended`, and say why in its
`description`. A question with no recommendation is one you have not thought
about, and it costs them a decision you could have made. An off-list answer is
always accepted, so a list does not have to be exhaustive to be useful.

**The three answers are not the same.**

- `accept` — they chose. Use it.
- `decline` — they left it to you: it carries your recommended choice when you
  gave one. Use that and note in a comment that it was your call, not theirs.
- `cancel` — nobody answered. Do not ask again and do not block: write what you
  would have written with no `ask_user` at all, which for an ambiguity means the
  `ctx.agent()` form.
"""


EDIT_INSTRUCTIONS = textwrap.dedent("""\
    You are editing a workflow that already exists and already works.

    ## What you are given
    A complete Python file, and one instruction describing a change to it.

    ## The rules of an edit, which are not the rules of writing one
    1. **Make the smallest change that satisfies the instruction.** Every line
       you touch is a line a reviewer has to read and a behaviour that might
       move. Renaming a variable you were not asked about, reformatting, or
       "improving" an unrelated step is not an edit — it is a rewrite wearing
       an edit's clothes, and it hides the change that was asked for.
    2. **Keep every step, node and workflow name.** A name is what the journal
       records, so changing one strands every run already in flight: on resume
       the engine looks for the recorded name at that position, does not find
       it, and the run diverges. If the instruction genuinely requires a
       rename, say so in your explanation rather than doing it silently.
    3. **Return the whole file**, not a diff and not a fragment. The `code`
       field must contain complete, runnable source.
    4. **Resolve before you filter**, exactly as when writing new code: if the
       instruction names a person, project, status or field, look it up with
       `call_read_operation` and bake in the id with the name in a comment.
       A word from the instruction reaching a query as raw text matches nothing
       and reports "none found".
    5. **Say what you changed** in the explanation, in terms of behaviour: what
       the workflow now does that it did not do before. Not a list of lines.

    ## When the instruction cannot be satisfied
    Say so and change nothing. An instruction needing an integration this
    environment does not have, or contradicting what the file does, is answered
    with an explanation — not with a guess. Returning the file unchanged is a
    valid answer and is better than a plausible edit that does the wrong thing.

    Every SDK rule from the authoring instructions still applies to the result:
    the file must still validate, still run, and still be projectable.
""")


# ---------------------------------------------------------------------------
# Structured output model for the final_output tool
# ---------------------------------------------------------------------------


class NodePlan(BaseModel):
    """One node, and why it is code or judgement."""

    node: str = Field(description="What this node does, in a few words.")
    kind: str = Field(
        description="'step' when a rule covers every input, 'agent' when it needs judgement."
    )
    why: str = Field(default="", description="One line: why that choice.")


class CodingOutput(BaseModel):
    """Structured output from the workflow coding agent."""

    code: str = Field(
        description="Complete, runnable Python source code. No markdown fences."
    )
    explanation: str = Field(
        default="",
        description="Brief explanation of the design choices.",
    )
    plan: list[NodePlan] = Field(
        default_factory=list,
        description=(
            "Each node with its step/agent choice and the reason. Decided "
            "before the code is written."
        ),
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
    plan: list[NodePlan] = field(default_factory=list)
    questions: list[Any] = field(default_factory=list)
    """Every ``AskedQuestion`` this generation put to a person, with its answer.

    On the result rather than only in the transcript, because the answers are
    *inputs to a build*: the same spec and the same answers reproduce the same
    file, which is what lets a generation that asked anything run in CI at all.
    Feed it back with ``loom author --answers``.
    """
    """Each node, classified as code or judgement, with the reason.

    Surfaced rather than left in the prompt because a rule the model is asked
    to follow silently is a rule nobody can check it followed. This is the
    artifact a reviewer reads to see *why* a threshold became an agent call —
    or to catch the reverse, an agent doing arithmetic.

    Empty when the model did not supply one; absence is not a claim that
    everything is deterministic."""

    @property
    def judgement_nodes(self) -> list[NodePlan]:
        """The nodes the agent decided needed judgement."""
        return [node for node in self.plan if node.kind == "agent"]

    def load(self) -> Any:
        """Import the generated code and return its ``WorkflowDefinition``.

        The point of generating a file is running it, and every caller that
        wants to otherwise writes the same importlib boilerplate — the cookbook
        had its own copy before this existed.

        Raises ``ValueError`` when there is no code, or none that declares a
        workflow, rather than returning ``None`` for the caller to trip over.
        """
        import importlib.util
        import tempfile
        from pathlib import Path

        from loom.runtime.workflow import WorkflowDefinition

        if not self.code.strip():
            raise ValueError(
                "no code to load"
                + (f": {self.issues[0].message}" if self.issues else "")
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated_workflow.py"
            path.write_text(self.code, encoding="utf-8")
            spec = importlib.util.spec_from_file_location(
                "loom_generated_workflow", path
            )
            if spec is None or spec.loader is None:
                raise ValueError("generated code could not be imported")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

        found = [
            value
            for value in vars(module).values()
            if isinstance(value, WorkflowDefinition)
        ]
        if not found:
            raise ValueError("the generated file declares no @workflow")
        return found[0]

    @property
    def is_clean(self) -> bool:
        """True when the code validates, runs, and passes review.

        Static cleanliness alone is a weak claim — code can validate perfectly,
        run perfectly, and still charge a customer twice.

        See :attr:`blockers` for *why* when this is ``False``.
        """
        return not self.blockers

    @property
    def blockers(self) -> list[str]:
        """Why :attr:`is_clean` is ``False`` — empty when it is ``True``.

        Added because "verified: False" on correct code is a dead end: the
        reason is spread across ``issues``, ``smoke``, and ``review``, and a
        caller has to know which to read.

        An **environmental** smoke failure is not a blocker. The smoke stage
        already treats it as a warning — the sandbox having no credential says
        nothing about the code — and ``is_clean`` used to disagree with the
        stage that produced the result, so a correct workflow that talks to a
        real service could never be clean.
        """
        found = [
            f"{issue.category}: {issue.message}"
            for issue in self.issues
            if issue.severity == "error"
        ]
        unverified = self.smoke is not None and (
            self.smoke.environmental or self.smoke.unverifiable
        )
        if self.smoke is not None and not self.smoke.ok and not unverified:
            found.append(f"smoke: {self.smoke.error}")
        if self.review is not None and self.review.blocking:
            found.append("review: the supervisor blocked it")
        return found

    def save(self, path: str) -> None:
        """Write the generated code to *path*."""
        with open(path, "w") as f:
            f.write(self.code)
        print(f"Saved to {path}")


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


@dataclass
class EditResult:
    """The outcome of changing a workflow that already exists.

    Separate from :class:`CodingResult` because an edit has a question a
    generation does not: *what moved*. A reviewer of new code reads the code; a
    reviewer of an edit reads the difference, and the two artifacts LOOM already
    generates at commit time — the graph and the narration — are the reviewable
    form of that difference for someone who does not read Python.
    """

    code: str
    """The complete edited source. Equal to the input when nothing changed."""
    issues: list[CodeIssue] = field(default_factory=list)
    explanation: str = ""
    """What the workflow now does that it did not before, in behaviour terms."""
    diff: str = ""
    """Unified diff against the original, for a reviewer."""
    changed: bool = False
    """``False`` when the model declined — which is a valid answer, and the one
    it is told to give when an instruction cannot be satisfied."""
    graph_changes: list[str] = field(default_factory=list)
    """Nodes added or removed, projected from both versions.

    The reviewable half of an edit: "three nodes changed" is a claim anybody can
    check, where a code diff needs Python."""
    repair_attempts: int = 0
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    questions: list[Any] = field(default_factory=list)
    """Every ``AskedQuestion`` this edit put to a person, with its answer.

    An edit asks for the same reason a generation does, and the record is worth
    the same thing: an instruction plus its answers reproduces the change.
    """
    smoke: SmokeResult | None = None

    @property
    def is_clean(self) -> bool:
        """No blocking issue survived the pipeline."""
        return not any(issue.severity == "error" for issue in self.issues)


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
    probes:
        Optional :class:`~loom.agents.probes.registry.ProbeRegistry`. Given one,
        the agent can *look* at the system it is writing code against — a URL's
        real response shape, a page's real controls — instead of inferring them
        from the spec. Left out, the ``observe_target`` tool is not offered and
        the agent behaves exactly as it did before, which is the intended
        degradation rather than a limitation to work around.
    tool_docs:
        Optional pre-loaded tool documentation strings.
        When provided, the agent can skip discovery and go straight
        to code generation. Backward compatible with the old API.
    """

    def __init__(
        self,
        model: ModelProvider,
        *,
        max_repair_attempts: int = 2,
        max_discovery_turns: int = 20,
        tool_docs: list[str] | None = None,
        tool_registry: ToolsetRegistry | None = None,
        node_registry: NodeCatalog | None = None,
        instructions: str | None = None,
        extra_instructions: str = "",
        allowed_packages: Iterable[str] | None = None,
        smoke_test: bool = True,
        smoke_input: Any = None,
        supervisor: CodeSupervisor | None = None,
        stages: list[Any] | None = None,
        executor: Any = None,
        user_interaction: Any | None = None,
        probes: Any | None = None,
        max_total_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> None:
        self._model = model
        self._executor = executor
        """Optional :class:`AgentExecutor` — LangGraph, Agno, Pydantic AI, or a
        host's own. ``None`` uses LOOM's built-in ReAct loop.

        Only the *turn loop* is swappable. Discovery tools, the verification
        pipeline, and repair stay here, because they are what makes generated
        code trustworthy and no framework supplies them. An executor that
        cannot honour ``output_type`` will lose the plan — the conformance
        suite is what stops one shipping that way."""
        self._max_repair = max_repair_attempts
        self._max_discovery = max_discovery_turns
        self._max_total_tokens = max_total_tokens
        self._max_cost_usd = max_cost_usd
        """Optional ceilings for the *job*, not for one call.

        Every ceiling used to be per invocation, because each call restarted
        the runner's turn counter and its usage accumulator — so a generation
        with three repair rounds got four independent budgets and nothing
        bounded the whole. See :class:`~loom.agents.generation.GenerationBudget`."""
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
            toolset_modules=_toolset_modules(tool_registry),
        )
        self._tool_docs = tool_docs or []
        self._tool_registry = tool_registry
        if node_registry is None:
            from loom.nodes.registry import (
                get_node_catalog,
                load_builtin_nodes,
            )

            load_builtin_nodes()
            node_registry = get_node_catalog()
        self._node_registry = node_registry
        self._probes = probes
        """The node catalog the agent browses. Pass ``rt.nodes`` so it finds
        exactly the nodes the generated workflow can call; the process-global
        catalog is the default and can be a superset."""
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
            else default_stages(
                supervisor=None, smoke=smoke_test, registry=self._tool_registry
            )
        )
        """Verification stages, cheapest first. Passing ``stages=`` replaces the
        default arrangement outright — a caller wanting a linter but no smoke
        run, or an extra rule of their own, composes the list rather than
        arguing with a flag."""
        """Optional second model that reviews the finished code. Off by default:
        it costs an extra call, and it is only worth it when the workflow does
        something you would want a colleague to look at."""
        self._user_interaction = _asking(user_interaction)
        self._ask_gate: Any | None = None
        self._asked: list[Any] = []
        if self._user_interaction is not None:
            from loom.agents.interaction import AskUserGate

            self._ask_gate = AskUserGate()

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
                "Besides the Python standard library and loom, only "
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

        # What can be looked at, when anything can. Named here rather than left
        # to the tool list: a capability nothing points at is one nobody uses.
        if self._probes is not None:
            block = self._probes.prompt_block()
            if block:
                parts.append(block)

        # Categories and counts only. Never the node list: the block must stay
        # O(categories) so registering the five-hundredth custom node does not
        # lengthen this prompt. Detail comes from search_nodes/node_contract.
        if self._node_registry is not None:
            block = self._node_registry.prompt_block()
            if block:
                parts.append(block)

        # Backward compat: append hand-written tool_docs if provided
        if self._tool_docs:
            parts.append(
                "## Additional Tool Documentation\n\n" + "\n\n".join(self._tool_docs)
            )

        if self._extra_instructions:
            parts.append(self._extra_instructions)

        if self._user_interaction is not None:
            parts.append(ASK_USER_INSTRUCTIONS)

        return "\n\n".join(parts)

    # Keep old private name as alias
    _build_system_prompt = build_system_prompt

    async def _repair_until_it_runs(
        self, session: Asking, code: str
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
                retry = await session.ask(smoke.as_feedback(code))
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
        self, session: Asking, spec: str, code: str
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

            try:
                revised = await session.ask(verdict.as_feedback(code))
            except Exception as exc:
                # Out of budget, or the loop failed. Keep the reviewed code:
                # a verdict nobody could act on does not make working code
                # worse.
                logger.info("review_repair | giving up: %s", exc)
                break
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

    def _resolved_kinds(self, tool_calls: Any) -> set[str]:
        """Entity kinds a resolver was actually executed for while authoring.

        Read from the calls the agent made, never from anything it says. The
        model can report having resolved a field as easily as it can invent
        the id, so a self-report would certify exactly the case it exists to
        catch.

        Only ``call_read_operation`` counts, and only against an operation the
        manifest marks ``resolves=``. Browsing a toolset is not resolving an
        entity.
        """
        registry = self._tool_registry
        if registry is None or not tool_calls:
            return set()

        kinds: set[str] = set()
        for call in tool_calls:
            if getattr(call, "name", "") != "call_read_operation":
                continue
            arguments = getattr(call, "arguments", None) or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    continue
            if not isinstance(arguments, dict):
                continue
            op_path = str(arguments.get("op_path", ""))
            toolset_id, _, operation_id = op_path.partition(".")
            if not operation_id:
                continue
            try:
                manifest = registry.get(toolset_id)
                spec_ = manifest.find_operation(operation_id) if manifest else None
            except Exception:
                continue
            if spec_ is not None and spec_.resolves:
                kinds.add(spec_.resolves)
        return kinds

    def _check_context(self, spec: str, tool_calls: Any = None) -> CheckContext:
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
            toolset_modules=_toolset_modules(self._tool_registry),
            fakes=[f for f in fakes if f[1]],
            spec=spec,
            resolved_kinds=self._resolved_kinds(tool_calls),
        )

    async def _repair_from(
        self, session: Asking, code: str, report: Any, context: CheckContext
    ) -> tuple[str, int, bool]:
        """Feed the pipeline's errors back to the model until they clear.

        One loop for every kind of failure. A type error and a traceback are
        both things the model can fix, and routing them through separate paths
        is how one of them ends up with no repair at all.

        Returns the code, how many rounds were spent, and whether the model
        **declined** — returned the file unchanged, which three stages tell it
        in as many words is the accepted way to say "I checked, and the finding
        is wrong here". The caller needs that third value: ending the loop is
        only half of the bargain those messages strike, and without it the
        finding stays an error and correct code is reported as broken.
        """
        rounds = 0
        declined = False
        while report.errors and rounds < self._max_repair:
            if _is_unrepairable(report):
                break
            rounds += 1
            try:
                retry = await session.ask(
                    _repair_prompt(report, code, context.spec)
                )
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
            # Compared stripped on both sides. `_extract_code` strips its
            # result and the incoming code may not be, so a byte-identical
            # reply could read as *changed* on trailing whitespace alone —
            # which would defeat the escape hatch these findings depend on and
            # spend a repair round doing it.
            if not candidate or candidate.strip() == code.strip():
                declined = True
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

        return code, rounds, declined

    async def edit(self, source: str, instruction: str) -> EditResult:
        """Change a workflow that already exists, and verify the result.

        The capability the product did not have. ``generate`` was the only
        entry point, so every change to a workflow meant regenerating it from a
        spec — and n8n, Zapier and Make have all shipped conversational
        editing, Zapier with checkpoint versioning and one-click rollback.

        What makes this worth more than their equivalent is that the machinery
        it reuses can *verify* the edit: the same sixteen stages run on the
        result, so a change that breaks entity resolution, silently caps a
        fetch, or puts the answer behind a model call the instruction never
        asked for is caught before it is offered. A visual editor cannot do
        that, because it has no notion of what the workflow was asked to do.

        Returns the file unchanged, with ``changed=False``, when the model
        declines — which the instructions above tell it to do rather than guess.
        """
        from loom.agents.agent import Agent
        from loom.agents.coding_tools import build_coding_tools
        from loom.agents.generation import CodingSession, GenerationBudget
        from loom.agents.models import ModelSettings

        budget = GenerationBudget.for_agent(
            self._model.model_name,
            max_turns=self._max_discovery + self._max_repair,
            max_total_tokens=self._max_total_tokens,
            max_cost_usd=self._max_cost_usd,
        )
        agent: Agent[Any] = Agent(
            name="workflow_editor",
            instructions=self.build_system_prompt() + "\n\n" + EDIT_INSTRUCTIONS,
            model=self._model,
            tools=build_coding_tools(
                registry=self._tool_registry,
                validator=self._validator,
                node_registry=self._node_registry,
                probes=self._probes,
            ),
            output_type=CodingOutput,
            model_settings=ModelSettings(temperature=0.2),
            executor=self._executor,
        )
        session = CodingSession(agent, budget)

        try:
            reply = await session.ask(_edit_prompt(source, instruction))
        except Exception as exc:
            logger.info("edit | agent loop ended: %s", exc)
            return EditResult(
                code=source,
                issues=[
                    CodeIssue(
                        "unsupported",
                        f"The agent did not produce an edit: {exc}",
                        "error",
                    )
                ],
                model_used=getattr(self._model, "model_name", ""),
                       questions=list(self._asked),
                   )

        code, explanation = _code_and_explanation(reply.output)
        code = _extract_code(code) or source

        if code.strip() == source.strip():
            # Declining is a valid answer and the one the instructions ask for
            # when the change cannot be made. Running the pipeline over
            # unchanged code would report the *original* file's findings as
            # though this edit had introduced them.
            return EditResult(
                code=source,
                explanation=explanation
                or "The agent left the workflow unchanged and gave no reason.",
                changed=False,
                model_used=self._model.model_name,
                input_tokens=budget.spent.input_tokens,
                output_tokens=budget.spent.output_tokens,
                       questions=list(self._asked),
                   )

        # The instruction is the spec for this change, so the stages that need
        # one — coverage, resolution, judgement — have something to weigh
        # against. Passing the original spec would ask them about a question
        # nobody just asked.
        code = tidy(code).code

        context = self._check_context(instruction, getattr(reply, "tool_calls", None))
        report = await self._pipeline.run(code, context)
        code, rounds, declined = await self._repair_from(
            session, code, report, context
        )
        if rounds:
            report = await self._pipeline.run(code, context)

        return EditResult(
            code=code,
            issues=_settle_advisories(list(report.issues), declined=declined),
            explanation=explanation,
            diff=unified_diff_of(source, code),
            changed=True,
            graph_changes=graph_delta(source, code),
            repair_attempts=rounds,
            model_used=self._model.model_name,
            input_tokens=budget.spent.input_tokens,
            output_tokens=budget.spent.output_tokens,
            smoke=report.detail("smoke"),
                   questions=list(self._asked),
               )

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
        from loom.agents.agent import Agent
        from loom.agents.coding_tools import build_coding_tools
        from loom.agents.generation import CodingSession, GenerationBudget
        from loom.agents.models import ModelSettings

        if self._user_interaction is not None:
            from loom.agents.interaction import AskUserGate

            self._ask_gate = AskUserGate()
        # Per generation, not per agent: a reused agent must not report a
        # previous run's answers as this one's provenance.
        self._asked = []

        # One budget for the job. Repair and review rounds draw on the same
        # allowance the discovery phase drew on, which is what the old comment
        # here claimed and the code did not do.
        budget = GenerationBudget.for_agent(
            self._model.model_name,
            max_turns=self._max_discovery + self._max_repair,
            max_total_tokens=self._max_total_tokens,
            max_cost_usd=self._max_cost_usd,
        )
        logger.info(
            "generate | model=%s budget=%d turns pre_loaded_docs=%d",
            self._model.model_name,
            budget.max_turns,
            len(self._tool_docs),
        )

        agent: Agent[Any] = Agent(
            name="workflow_coder",
            instructions=self.build_system_prompt(),
            model=self._model,
            tools=build_coding_tools(
                registry=self._tool_registry,
                validator=self._validator,
                node_registry=self._node_registry,
                interaction=self._user_interaction,
                probes=self._probes,
                gate=self._ask_gate,
                asked=self._asked,
            ),
            output_type=CodingOutput,
            model_settings=ModelSettings(temperature=0.2),
            executor=self._executor,
        )
        session = CodingSession(agent, budget)

        try:
            result = await session.ask(spec)
        except Exception as exc:
            # Never propagate. A caller asked for code and is entitled to an
            # answer it can act on — an exception discards whatever the run
            # learned and gives them a stack trace instead of a reason.
            logger.info("generate | agent loop ended: %s", exc)
            attempted = [
                (call.name, _brief_args(call.arguments))
                for call in getattr(exc, "tool_calls", []) or []
            ]
            # Advice that cannot help is worse than none: a missing API key
            # is not fixed by raising the turn budget, and telling someone to
            # narrow their spec sends them to rewrite a spec that was fine.
            from loom.agents.smoke import is_environmental

            if is_environmental(str(exc)):
                remedy = (
                    "This is about the environment, not the spec — a missing "
                    "credential, service, or network. Set the provider's API "
                    "key (ANTHROPIC_API_KEY, OPENAI_API_KEY, …); a shell does "
                    "not read .env the way the cookbooks do."
                )
            else:
                remedy = (
                    "If it ran out of turns, raise max_discovery_turns or "
                    "narrow the spec — each entity it has to resolve costs a "
                    "turn."
                )
            return CodingResult(
                code="",
                issues=[
                    CodeIssue(
                        "unsupported",
                        f"The agent did not produce code: {exc}. {remedy}",
                        "error",
                    )
                ],
                model_used=getattr(self._model, "model_name", ""),
                # What the run actually spent before it gave up. This path
                # reported zeroes, so a generation that burned a budget and
                # four minutes came back reading as free — and the one number
                # that would tell you to raise the budget, or that raising it
                # will be expensive, was the number that had been discarded.
                # The budget has held the real figures since the job started.
                input_tokens=budget.spent.input_tokens,
                output_tokens=budget.spent.output_tokens,
                repair_attempts=0,
                # What it managed before giving up. "0 tool calls" reads as
                # "it did nothing"; seventeen reads as "it never converged",
                # and those want opposite responses from whoever is looking.
                tool_calls=attempted,
                questions=list(self._asked),
            )

        # Extract code from structured output
        explanation = ""
        plan: list[NodePlan] = []
        if isinstance(result.output, CodingOutput):
            code = result.output.code
            explanation = result.output.explanation
            plan = list(result.output.plan)
        elif isinstance(result.output, dict):
            code = result.output.get("code", "")
            explanation = str(result.output.get("explanation", ""))
            plan = [
                NodePlan.model_validate(entry)
                for entry in result.output.get("plan") or []
                if isinstance(entry, dict)
            ]
        else:
            code = str(result.output or "")

        if plan:
            logger.info(
                "generate | plan: %s",
                ", ".join(f"{node.node}={node.kind}" for node in plan),
            )

        code = _extract_code(code)

        # Repair and smoke must not block on a human. Flip the gate off
        # before either re-invokes the same agent object.
        if self._ask_gate is not None:
            self._ask_gate.enabled = False

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
                questions=list(self._asked),
            )

        # Mechanical fixes first, so no repair round is spent on one. An
        # unused import is not a judgement call, and the SDK manufactures that
        # particular finding by telling the model to import `Retry`
        # unconditionally — see `loom.agents.tidy`.
        tidied = tidy(code)
        if tidied.changed:
            logger.info("tidy | fixed %d finding(s) without a model call", tidied.fixed)
        code = tidied.code

        # One pipeline, cheapest stage first, stopping at the first blocking
        # failure. Repair is driven by whatever it reports, so a type error and
        # a traceback reach the model by the same path.
        context = self._check_context(spec, getattr(result, "tool_calls", None))
        report = await self._pipeline.run(code, context)
        code, rounds, declined = await self._repair_from(
            session, code, report, context
        )
        report = await self._pipeline.run(code, context) if rounds else report

        issues = _settle_advisories(list(report.issues), declined=declined)
        errors = [issue for issue in issues if issue.severity == "error"]
        smoke = report.detail("smoke")

        # A second opinion, once the code runs. Code that validates and executes
        # can still do the wrong thing, and the author is the worst judge of it.
        review: SupervisorVerdict | None = None
        review_rounds = 0
        if self._supervisor is not None and not errors:
            code, review, review_rounds = await self._revise_until_approved(
                session, spec, code
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
            plan=plan,
            tool_calls=[(c.name, _brief_args(c.arguments)) for c in result.tool_calls],
            smoke=smoke,
            review=review,
            repair_attempts=repair_attempts,
            model_used=self._model.model_name,
            # The *job's* totals, not the first call's. Every round after the
            # first was previously uncounted, so a generation that repaired
            # three times reported the cost of one.
            input_tokens=budget.spent.input_tokens,
            output_tokens=budget.spent.output_tokens,
            questions=list(self._asked),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _edit_prompt(source: str, instruction: str) -> str:
    """What the model is asked, with the file it is editing."""
    return (
        f"Here is the workflow as it stands:\n\n```python\n{source}\n```\n\n"
        f"Change it so that: {instruction}\n\n"
        "Return the complete edited file via final_output. If the change "
        "cannot be made, return the file unchanged and say why."
    )


def _code_and_explanation(output: Any) -> tuple[str, str]:
    """Pull ``(code, explanation)`` out of whatever the model returned."""
    if isinstance(output, CodingOutput):
        return output.code, output.explanation
    if isinstance(output, dict):
        return str(output.get("code", "")), str(output.get("explanation", ""))
    return str(output or ""), ""


def unified_diff_of(before: str, after: str) -> str:
    """A reviewable diff between two versions of a workflow."""
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="before.py",
            tofile="after.py",
            n=3,
        )
    )


def graph_delta(before: str, after: str) -> list[str]:
    """Nodes added and removed, projected from both versions of the source.

    The half of an edit a non-programmer can review. Best-effort: source that
    cannot be projected yields no delta rather than an error, because a diff is
    a courtesy and the code is the artifact.
    """
    # Both sides must at least compile. The extractor tolerates source it
    # cannot project by returning nothing, which would report a broken edit as
    # "every node removed" — a delta that is not merely unhelpful but wrong.
    for source in (before, after):
        try:
            compile(source, "<delta>", "exec")
        except (SyntaxError, ValueError):
            return []
    try:
        was = _projected_labels(before)
        now = _projected_labels(after)
    except Exception:  # pragma: no cover - projection is best-effort here
        return []
    added = sorted(now - was)
    removed = sorted(was - now)
    return [f"+{label}" for label in added] + [f"-{label}" for label in removed]


def _projected_labels(source: str) -> set[str]:
    """Every node label the WGIR extractor finds in *source*."""
    import tempfile
    from pathlib import Path

    from loom.graph.pipeline import build_graph, flows_in

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "flow.py"
        path.write_text(source, encoding="utf-8")
        labels: set[str] = set()
        for flow_id in flows_in(path) or [""]:
            graph = build_graph(path, flow_id=flow_id)
            labels.update(node.label for node in graph.nodes)
        return labels


def _asking(interaction: Any) -> Any:
    """*interaction* if something can actually answer, else ``None``.

    A **capability** check, not a timeout, and the difference is what failure
    costs: a tool that is offered and cannot be answered blocks until it gives
    up, while a tool that is never offered costs nothing at all. ``None`` omits
    ``ask_user`` from the tool list entirely — the rule ``observe_target``
    follows, for the reason it follows it: a tool a model can see is a tool it
    will spend a turn on.

    Sampled once, when the agent is built. An interaction that reports itself
    available and then cannot answer still returns ``cancel`` rather than
    hanging, so this is the cheap check and not the only one.
    """
    if interaction is None:
        return None
    available = getattr(interaction, "available", None)
    if available is None:
        # A host's own implementation predating `available` is taken at its
        # word: it was passed in deliberately, which is itself the claim.
        return interaction
    try:
        return interaction if available() else None
    except Exception:
        return None


def _settle_advisories(issues: list[CodeIssue], *, declined: bool) -> list[CodeIssue]:
    """Accept the findings a model was invited to decline, once it has.

    Three stages — outcome, resolution, judgement — raise their findings as
    *errors* only because ``report.errors`` is what drives the repair loop, and
    each tells the model that returning the file unchanged is the accepted
    answer. The loop honoured half of that: it stopped. The finding stayed an
    error, so ``is_clean`` was ``False`` and callers refused to run the code.

    A workflow that had walked every rung of the resolution ladder and written
    down each namespace it checked came back reported as broken. This keeps the
    other half of the bargain: the finding is preserved, at the severity it
    always deserved, with the model's judgement recorded beside it.

    Only when the model *declined*. A repair that ran out of turns, or one whose
    every attempt regressed, has not judged anything — and downgrading there
    would turn "could not fix it" into "decided it was fine".
    """
    if not declined:
        return issues
    settled: list[CodeIssue] = []
    for issue in issues:
        if issue.severity == "error" and issue.category in ADVISORY_STAGES:
            settled.append(
                CodeIssue(
                    issue.category,
                    f"{issue.message} [The model reviewed this and stood by "
                    f"the code, which the check invites. Reported rather than "
                    f"blocking — read it before shipping.]",
                    "warning",
                )
            )
        else:
            settled.append(issue)
    return settled


def _toolset_modules(registry: Any | None) -> dict[str, str]:
    """Toolset id to the module it is really imported from."""
    if registry is None:
        return {}
    try:
        modules = {}
        for toolset_id in registry.list_toolsets():
            manifest = registry.get(toolset_id)
            module = getattr(manifest, "tools_module", "")
            if module:
                modules[toolset_id] = module
        return modules
    except Exception:  # a fake registry in a test need not support it
        return {}


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

    It carries the run's **output** for the same reason it carries the
    traceback. The pipeline had it all along — ``smoke_run`` records
    ``output_preview`` — and handed it only to the replay comparison, so a
    model repairing "you returned nothing useful" could not see what it had
    returned. Verification the agent is not told about is verification that
    cannot correct anything.
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

    preview = getattr(smoke, "output_preview", "") if smoke is not None else ""
    if preview:
        lines += ["", "## What your code returned when it ran", preview]

    if spec:
        lines += ["", "## What the workflow must do", spec]
    lines += ["", "## The code that failed", "```python", code, "```"]
    return "\n".join(lines)



def _summarise_smoke(result: SmokeResult) -> str:
    """One line for the log."""
    return f"{'ok' if result.ok else 'failed'} at {result.phase}"


#: One fenced block, wherever it sits in the response. Non-greedy, so several
#: blocks in one answer stay separate rather than merging into one.
_FENCE = re.compile(r"```[ \t]*([\w+.-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)

#: An opening fence with nothing closing it — what a response truncated at the
#: token limit looks like.
_OPEN_FENCE = re.compile(r"```[ \t]*([\w+.-]*)[ \t]*\r?\n")

_PYTHON_TAGS = frozenset({"", "python", "py", "python3"})


def _extract_code(text: str) -> str:
    """The Python out of a model response, fenced or not.

    Tolerant about *where* the fence is, and that is the whole point. An earlier
    version stripped fences only when the response **began** with one, so a
    single sentence of preamble — which every model writes sometimes, and which
    no prompt reliably suppresses — was handed to ``compile()`` as if it were
    Python.

    The symptom was memorable and pointed at the wrong thing: ``invalid
    character '—' (U+2014) ... line 1``. Prose contains em-dashes; Python may
    not, outside a string. So a working model, working tools and working code
    presented as a syntax error in the generated workflow, and the repair loop
    could not converge on it because the code was never what was broken.

    Trailing prose was the same bug from the other end: the closing fence was
    only dropped when it was the very last line, so "This uses JQL — …" after
    the block was compiled too.
    """
    text = text.strip()

    blocks: list[tuple[str, str]] = [
        (str(tag).lower(), str(body))
        for tag, body in _FENCE.findall(text)
        if str(body).strip()
    ]
    if blocks:
        python = [body for tag, body in blocks if tag in _PYTHON_TAGS]
        # Prefer a block the model *labelled* python. Among several, the longest
        # — a response that shows a snippet before the real workflow puts the
        # workflow last and makes it much the larger. Guessing wrong here yields
        # code that compiles and is wrong, which is worse than a syntax error,
        # so the rule stays blunt and explicable rather than clever.
        candidates: list[str] = python or [body for _, body in blocks]
        return max(candidates, key=len).strip()

    opening = _OPEN_FENCE.search(text)
    if opening is not None:
        # An unclosed fence: the response was cut off at the token limit. What
        # follows is still the best available code, and returning it lets the
        # repair loop see a truncated function rather than a stray ``` line.
        return text[opening.end() :].strip()

    return text
