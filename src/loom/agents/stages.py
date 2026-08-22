"""The verification stages themselves.

Each wraps a capability that already exists — compiling, the AST validator, the
smoke runner, the supervisor — behind the one :class:`Check` interface, so the
generator composes them instead of knowing about each in turn.

Nothing here is specific to an integration or a model. A stage is added by
writing a class and putting it in the list.
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from loom.agents.checks import CheckContext, CheckResult
from loom.agents.validator import CodeIssue, CodeValidator

__all__ = [
    "BrowserEffectStage",
    "CataloguePreferenceStage",
    "CompileStage",
    "CritiqueStage",
    "LintStage",
    "ObservedTargetsStage",
    "OutcomeStage",
    "ProjectionStage",
    "ReplayStage",
    "SelectorStage",
    "SmokeStage",
    "StaticStage",
    "TypeStage",
    "default_stages",
]


#: Stages whose findings are errors *so the repair loop sees them*, not because
#: they block anything.
#:
#: `report.errors` is what drives repair, so a finding a model should be asked
#: about has to be an error even when it is a judgement call rather than a
#: defect. Each of these tells the model, in as many words, that returning the
#: code unchanged is the accepted answer — which is how a model says "I checked,
#: and the finding is wrong here".
#:
#: What was missing is the other half of that bargain. The loop ended, and the
#: finding stayed an error: `is_clean` was `False`, and a caller refused to run
#: correct code. A workflow that had walked every rung of the resolution ladder
#: and documented each namespace it checked was reported as broken.
#:
#: Named as data so `TestTheEscapeHatchIsHonoured` can assert that every stage
#: here actually makes the promise, and that no stage makes it without being
#: here.
ADVISORY_STAGES: frozenset[str] = frozenset({
    "outcome",
    "resolution",
    "judgement",
})

#: The phrase each advisory stage uses to offer the escape hatch. Matched rather
#: than restated, so the set above cannot drift from the promise.
ESCAPE_HATCH = "return the code unchanged"


@dataclass
class CompileStage:
    """Does it compile? Free, and everything after it assumes so."""

    name: str = "compile"
    cost: int = 0
    blocking: bool = True

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        from loom.agents.smoke import compile_check

        outcome = compile_check(code)
        if outcome.ok:
            return CheckResult(self.name)
        return CheckResult(
            self.name, issues=[CodeIssue("syntax", outcome.error, "error")]
        )


@dataclass
class StaticStage:
    """The AST rules: structure, determinism, imports, store, toolsets."""

    name: str = "static"
    cost: int = 10
    blocking: bool = True

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        validator = CodeValidator(
            allowed_packages=context.allowed_packages,
            available_toolsets=context.available_toolsets,
            toolset_modules=context.toolset_modules,
        )
        return CheckResult(self.name, issues=validator.validate(code))


@dataclass
class LintStage:
    """Ruff, when the environment has it.

    Catches the mechanical faults that compile fine — an unused import, a name
    used before assignment, a bare except. Optional by design: a user's
    environment need not carry a linter, and a missing one is not a defect in
    the generated code.
    """

    name: str = "lint"
    cost: int = 20
    blocking: bool = False
    select: str = "F,E9"
    """Errors and undefined names only. Style is not correctness, and a model
    should not spend a repair round on line length."""

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        executable = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
        if not Path(executable).exists():
            return CheckResult(
                self.name, skipped=True, reason="ruff is not installed"
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated.py"
            path.write_text(code, encoding="utf-8")
            try:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    [executable, "check", "--select", self.select,
                     "--output-format", "json", "--no-cache", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=context.timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return CheckResult(self.name, skipped=True, reason=f"ruff failed: {exc}")

        try:
            findings = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            return CheckResult(
                self.name, skipped=True, reason="ruff produced no usable output"
            )

        issues = [
            CodeIssue(
                "lint",
                f"{f.get('code', '')}: {f.get('message', '')} "
                f"(line {(f.get('location') or {}).get('row', '?')})",
                "error",
            )
            for f in findings
        ]
        return CheckResult(self.name, issues=issues)


@dataclass
class TypeStage:
    """Mypy, when the environment has it.

    The stage that catches a step called with the wrong arity or a model field
    that does not exist — mistakes which compile, lint clean, and fail at run
    time on someone else's schedule.
    """

    name: str = "types"
    cost: int = 30
    blocking: bool = False

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        executable = shutil.which("mypy") or str(Path(sys.executable).parent / "mypy")
        if not Path(executable).exists():
            return CheckResult(self.name, skipped=True, reason="mypy is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated.py"
            path.write_text(code, encoding="utf-8")
            try:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    [
                        executable,
                        "--no-error-summary",
                        "--no-color-output",
                        "--ignore-missing-imports",
                        # The version *running* this, not whatever floor a
                        # host's mypy config declares. `pyproject.toml` pins
                        # 3.11 because that is LOOM's own support floor, and
                        # inheriting it here makes mypy parse this
                        # environment's site-packages stubs under a grammar
                        # they were not written for.
                        "--python-version",
                        f"{sys.version_info.major}.{sys.version_info.minor}",
                        # A generated file is not part of any project, so a
                        # discovered config would apply somebody else's rules
                        # to it.
                        "--no-site-packages",
                        # Check this file, not the library it imports. Without
                        # this, mypy follows into loom itself and
                        # reports its internals as defects in the generated
                        # code — dozens of warnings about lines the model never
                        # wrote, which is worse than no type checking at all.
                        "--follow-imports=silent",
                        # The file is generated, not authored: annotations are a
                        # nice-to-have, and demanding them would bury the real
                        # findings under noise the model cannot act on usefully.
                        "--disable-error-code", "no-untyped-def",
                        "--disable-error-code", "annotation-unchecked",
                        # `ctx: Context` is the documented form; demanding
                        # `Context[Any, Any]` would flag every workflow written
                        # exactly as instructed.
                        "--disable-error-code", "type-arg",
                        # The demo block the prompt asks for calls an unannotated
                        # main(); flagging it nags about boilerplate we specified.
                        "--disable-error-code", "no-untyped-call",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=max(context.timeout, 60.0),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return CheckResult(self.name, skipped=True, reason=f"mypy failed: {exc}")

        return self._read(completed.stdout, path.name)

    def _read(self, output: str, filename: str) -> CheckResult:
        """Only what mypy said about *this* file, and only if it finished.

        Two failures this prevents, both of them the environmental-failure trap
        in a new costume — the one that "fed 401s into the repair loop until a
        workflow came back gutted".

        **An error from another file is not a defect in the generated code.**
        The previous version kept every ``: error:`` line and split the filename
        off, so a complaint about a *dependency's* stubs arrived as a complaint
        about the model's workflow, with the evidence of its origin removed. In
        this repo that is not hypothetical: numpy's stubs use PEP 695 ``type``
        statements, which are a syntax error below 3.12, so any environment
        with numpy installed handed the repair loop
        ``737: error: Type statement is only supported in Python 3.12`` as
        something to fix in a file that has no line 737.

        **And mypy stopping early means nothing was checked.** "errors
        prevented further checking" is the same signal ``scripts/typecheck.py``
        exits 2 for, and for the same reason: a gate that did not run must be
        distinguishable from one that passed. Reporting clean there would
        certify code nothing looked at.
        """
        prefix = f"{filename}:"
        issues: list[CodeIssue] = []
        foreign = 0
        for line in output.splitlines():
            if ": error:" not in line:
                continue
            head, _, rest = line.partition(":")
            if head.endswith(prefix.rstrip(":")):
                issues.append(CodeIssue("types", rest.strip(), "warning"))
            else:
                foreign += 1

        if "errors prevented further checking" in output:
            return CheckResult(
                self.name, skipped=True,
                reason="mypy stopped before checking this file — its "
                       "environment has a dependency it cannot parse")
        if foreign and not issues:
            return CheckResult(
                self.name, skipped=True,
                reason=f"mypy reported {foreign} error(s), all in other files")
        return CheckResult(self.name, issues=issues)


@dataclass
class SmokeStage:
    """Actually run it, against fakes, in a subprocess."""

    name: str = "smoke"
    cost: int = 50
    blocking: bool = True

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        from loom.agents.smoke import smoke_run

        outcome = await asyncio.to_thread(
            smoke_run,
            code,
            context.workflow_input,
            timeout=context.timeout,
            fakes=context.fakes,
            recording=context.recording,
        )
        if outcome.ok:
            return CheckResult(self.name, detail=outcome)

        if outcome.environmental or outcome.unverifiable:
            # The sandbox could not authenticate. Unverified is not broken, and
            # asking for a repair invites the model to delete the integration.
            return CheckResult(
                self.name,
                issues=[
                    CodeIssue(
                        "runtime",
                        "could not verify by running: "
                        + (
                            "no sample input matches this workflow's declared "
                            "input type, so the harness invented one"
                            if outcome.unverifiable
                            else "the sandbox has no credentials for this "
                            "integration"
                        )
                        + f" ({(outcome.error or '')[:120]}). "
                        "The code was not executed end to end.",
                        "warning",
                    )
                ],
                detail=outcome,
            )

        return CheckResult(
            self.name,
            issues=[
                CodeIssue(
                    "runtime",
                    f"smoke run failed during {outcome.phase}: {outcome.error}",
                    "error",
                )
            ],
            detail=outcome,
        )


@dataclass
class ProjectionStage:
    """Does every durable call this code makes appear in the graph?

    The graph is *projected* from the code rather than authored beside it, and
    everything downstream leans on that being complete: the canvas, the run
    trace, the narration, and the ``graph.json`` diff a reviewer reads instead
    of the Python. A durable operation the projection misses is not a cosmetic
    gap — it is a workflow that does something its own documentation does not
    mention, and the diff stays clean while it happens.

    Two ways that goes wrong, and this catches the second:

    * A ``ctx`` method the extractor does not model at all. ``ctx.node`` was in
      that state for the node system's whole life. A stage cannot catch it —
      every workflow is equally wrong — so ``DURABLE_CTX_CALLS`` and a unit test
      hold that line instead.
    * A method the extractor *does* model, in a code shape its walker never
      reaches. ``return await ctx.step(f, x)`` was that until recently: the
      return statement swallowed the call, and the step showed up in the graph
      only by accident, via a registry pass that put every step in the module
      into every flow in it.

    A warning, not an error, and non-blocking. A hole here is a defect in the
    *extractor*, not in the generated code, so asking the model to repair it
    invites it to rewrite correct code into a shape that happens to project —
    the same reasoning that keeps environmental failures out of the repair loop.
    What it buys is that the hole is reported at the moment it appears, against
    the workflow that revealed it.
    """

    name: str = "projection"
    cost: int = 18
    """Pure AST, so it sits with the other static passes."""
    blocking: bool = False

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        from loom.graph.extractor import (
            _CTX_CALL_MAP,
            durable_ctx_calls,
            extract_from_source,
        )

        declared = durable_ctx_calls(code)
        if not declared:
            return CheckResult(self.name)

        drawable = set(_CTX_CALL_MAP.values())
        projected = sum(
            1 for node in extract_from_source(code).nodes if node.kind in drawable
        )
        if projected >= len(declared):
            return CheckResult(self.name)

        missing = len(declared) - projected
        where = ", ".join(
            sorted({f"ctx.{method} (line {line})" for method, line in declared})
        )
        return CheckResult(
            self.name,
            issues=[
                CodeIssue(
                    "projection",
                    f"{missing} durable call(s) do not appear in the projected "
                    f"graph, so the canvas and the description will not show "
                    f"them. The code declares {where}. This is a gap in graph "
                    f"extraction rather than in the workflow — report it "
                    f"instead of reshaping the code around it.",
                    "warning",
                )
            ],
            detail={"declared": len(declared), "projected": projected},
        )


@dataclass
class CataloguePreferenceStage:
    """Work a catalogued node already does, written out by hand.

    A node is a *shareable contract*: typed, versioned, searchable by the
    coding agent, contract-rendered, and drawn on the canvas as itself. A step
    that does the same job by hand is none of those — it draws as an opaque
    effect, so the reviewer sees "call_the_api" where the node would have said
    ``io.http_request``, and nothing tells the next author the node existed.

    That gap widens as the agent gets more capable. An agent that can reach the
    network to *look* at something will reach for the same library to *use* it,
    and every time it does, the catalogue that makes loom legible to a visual
    builder gets one workflow less relevant.

    A warning, and only where the node is actually registered here: advice to
    use something this environment does not have is worse than no advice. The
    rules are deliberately few. A long list of near-misses trains people to
    ignore the stage, and the two below are the cases where the node is a
    straight replacement rather than a judgement call.
    """

    registry: Any = None
    name: str = "catalogue"
    cost: int = 19
    blocking: bool = False

    #: ``(modules that do the job by hand, the node that does it, what it is)``
    RULES: ClassVar[tuple[tuple[frozenset[str], str, str], ...]] = (
        (
            frozenset({"httpx", "requests", "aiohttp", "urllib3"}),
            "io.http_request",
            "an HTTP request",
        ),
        (
            frozenset({"pypdf", "PyPDF2", "docx", "pdfplumber"}),
            "transform.parse_document",
            "reading a document",
        ),
    )

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return CheckResult(self.name, skipped=True, reason="does not parse")

        catalogued = self._catalogued()
        if catalogued is None:
            return CheckResult(
                self.name, skipped=True, reason="no node catalogue to compare against"
            )

        used = _called_modules(tree)
        issues = []
        for modules, node_id, doing in self.RULES:
            hit = sorted(used & modules)
            if not hit or node_id not in catalogued:
                continue
            issues.append(
                CodeIssue(
                    "catalogue",
                    f"{doing} is done here with {hit[0]}, and '{node_id}' is "
                    f"catalogued for it. A node is typed, versioned and drawn "
                    f"on the canvas as itself; a hand-rolled step draws as an "
                    f"opaque effect. Use ctx.node('{node_id}', …) unless it "
                    f"cannot express what this needs.",
                    "warning",
                )
            )
        return CheckResult(self.name, issues=issues)

    def _catalogued(self) -> set[str] | None:
        """What this environment can actually offer, or ``None`` to stay silent.

        Reads the catalogue; deliberately does not *populate* it. Both paths
        that reach this stage have already loaded the built-ins —
        ``WorkflowCodingAgent.__init__`` and ``build_coding_tools`` — so
        loading here changes nothing where it matters and imports a package
        tree where it does not. A verification stage that mutates
        process-global state to do its job is one whose findings depend on
        who ran first.
        """
        registry = self.registry
        if registry is None:
            try:
                from loom.nodes.registry import get_node_catalog

                registry = get_node_catalog()
            except Exception:
                return None
        try:
            catalogued = {card.id for card in registry.search("", limit=500)}
        except Exception:
            return None
        return catalogued or None


def _called_modules(tree: ast.Module) -> set[str]:
    """Top-level module names that appear as the receiver of a call.

    ``httpx.get(...)`` and ``httpx.AsyncClient(...)`` both count; a bare
    ``import httpx`` used only for a type annotation does not, because the
    question is what the code *does*, not what it mentions.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        receiver = node.func
        while isinstance(receiver, ast.Attribute):
            receiver = receiver.value
        if isinstance(receiver, ast.Name):
            found.add(receiver.id)
    return found


@dataclass
class OutcomeStage:
    """Did the run produce an answer, or only finish?

    ``SmokeStage`` asks whether the code ran. Until this stage, nothing asked
    what it *returned*: the output was recorded on the smoke result and read
    only by ``ReplayStage``, which compares two runs for **equality** and never
    for sense. So a workflow answering "0 fields" to a spec that asked for
    "every visible form control" passed every gate — it compiled, ran,
    completed, and replayed identically — and the model was never told what it
    had produced.

    Two conditions coincide before this says anything, the discipline
    ``CoverageStage`` uses and for the same reason: an empty result is very
    often the correct one.

    * The spec asked for completeness. The vocabulary is ``CoverageStage``'s
      own, imported rather than copied, so the two cannot drift apart.
    * And the run returned an empty collection anyway.

    **An error rather than a warning**, which is the one surprising choice
    here. A warning drives no repair, so the model would never see it, and a
    finding nobody sees is the state this stage exists to end. What makes that
    safe is already in the repair loop: code returned *unchanged* ends it. A
    model that judges the empty result correct says so by leaving the file
    alone, and the message tells it that in as many words.

    Deliberately silent on a synthetic input. When the harness invented the
    input from a type annotation, an empty result says nothing about the code —
    the same reason ``SmokeStage`` treats that run as unverifiable rather than
    failed.

    It reads ``SmokeResult.empty_paths`` rather than parsing ``output_preview``.
    The preview is capped at 400 characters, and the first version of this stage
    did parse it — which passed its own tests and then skipped the real workflow
    it was written for, whose empty ``fields`` list sat behind 1500 characters of
    page text. The fact is computed where the output is whole.
    """

    name: str = "outcome"
    cost: int = 55
    """After smoke (50), which produces what this reads; before replay (60)."""
    blocking: bool = False

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        smoke = context.prior.detail("smoke") if context.prior is not None else None
        if smoke is None or not getattr(smoke, "ok", False):
            return CheckResult(
                self.name, skipped=True, reason="no completed run to read"
            )
        if getattr(smoke, "synthetic_input", False):
            return CheckResult(
                self.name, skipped=True, reason="the input was invented, not supplied"
            )

        spec = (context.spec or "").lower()
        if not any(word in spec for word in CoverageStage.WANTS_ALL):
            return CheckResult(self.name)

        empty = list(getattr(smoke, "empty_paths", ()))
        if not empty:
            return CheckResult(self.name)

        where = ", ".join(empty[:3])
        more = f" (and {len(empty) - 3} more)" if len(empty) > 3 else ""
        return CheckResult(
            self.name,
            issues=[
                CodeIssue(
                    "outcome",
                    f"the spec asks for completeness, and the run came back "
                    f"empty at {where}{more}. Either the code looks in the "
                    f"wrong place, or nothing was there to find. If nothing "
                    f"was there, return the code unchanged — that is accepted "
                    f"and ends the repair.",
                    "error",
                )
            ],
            detail=empty,
        )


@dataclass
class ReplayStage:
    """Run it twice and compare — determinism, observed rather than argued.

    The static lint catches the obvious sources (``datetime.now()``,
    ``random``); this catches the rest, including a body that iterates a set or
    depends on dict ordering across processes.
    """

    name: str = "replay"
    cost: int = 60
    blocking: bool = False

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        from loom.agents.smoke import smoke_run

        first, second = await asyncio.gather(
            asyncio.to_thread(
                smoke_run, code, context.workflow_input,
                recording=context.recording,
                timeout=context.timeout, fakes=context.fakes,
            ),
            asyncio.to_thread(
                smoke_run, code, context.workflow_input,
                recording=context.recording,
                timeout=context.timeout, fakes=context.fakes,
            ),
        )

        if not (first.ok and second.ok):
            return CheckResult(
                self.name, skipped=True, reason="the run did not complete twice"
            )
        if first.output_preview == second.output_preview:
            return CheckResult(self.name)

        return CheckResult(
            self.name,
            issues=[
                CodeIssue(
                    "determinism",
                    "two runs with the same input produced different output "
                    f"({first.output_preview[:80]!r} vs "
                    f"{second.output_preview[:80]!r}). A workflow body must be "
                    "reproducible: take time from ctx.now(), ids from "
                    "ctx.uuid4(), and do not iterate an unordered collection.",
                    "error",
                )
            ],
        )


@dataclass
class CritiqueStage:
    """A second model reads the finished code. Optional, and the most expensive."""

    name: str = "critique"
    cost: int = 100
    blocking: bool = False
    supervisor: Any = None

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        if self.supervisor is None:
            return CheckResult(self.name, skipped=True, reason="no supervisor configured")

        verdict = await self.supervisor.review(context.spec, code)
        issues = [
            CodeIssue("review", finding, "warning")
            for finding in getattr(verdict, "findings", [])
        ]
        return CheckResult(self.name, issues=issues, detail=verdict)


@dataclass
class CoverageStage:
    """Did the code answer the question the spec actually asked?

    Written for one observed failure. Asked to "show **all** the stories in X",
    the agent generated ``max_results=100`` and reported
    ``f"({len(issues)} found)"``. Both lines are individually reasonable and the
    result is a report that says "42 found" when 312 matched — wrong in the way
    that survives review, because the number is real and only the framing lies.

    Two conditions have to coincide, so this stays quiet on the many workflows
    where a cap is exactly right:

    * the spec asked for completeness — "all", "every", "the full list",
    * and the code caps a fetch without ever asking whether it got everything.

    **A cap need not be written down to exist.** Every paged read has a default
    one, so `await ctx.step(jira_search_issues, jql)` with no keyword at all is
    capped just as firmly as `max_results=100` — and it was the one shape this
    stage could not see, because there was no literal to find. The registry is
    what closes it: an operation whose manifest declares `pagination` is a
    capped fetch by construction. Read from the registry rather than listed
    here, so a host's own toolset is as findable as a shipped one.

    A warning, not an error. Deciding that 100 is enough is a legitimate call;
    making it *without noticing* is the defect, and the fix is usually one line.
    """

    name: str = "coverage"
    cost: int = 15
    blocking: bool = False

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry

    #: Words that make completeness part of the request rather than a detail.
    WANTS_ALL: ClassVar[tuple[str, ...]] = (
        "all ", "every ", "each ", "entire", "complete list", "full list",
        "everything", "no limit", "exhaustive",
    )

    #: Reading any of these is evidence the question was asked.
    COVERAGE_CHECKS: ClassVar[tuple[str, ...]] = (
        "complete", "truncated", "summary", "total",
    )

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        spec = (context.spec or "").lower()
        if not any(word in spec for word in self.WANTS_ALL):
            return CheckResult(self.name)

        capped = _capped_calls(code)
        # A paged read with no cap keyword is still capped — by the operation's
        # own default. Named separately from the literal caps so the message can
        # say which kind was found; "raise the limit" is wrong advice for a call
        # that never passed one.
        defaulted = self._paged_calls_without_a_cap(code)
        if not capped and not defaulted:
            return CheckResult(self.name)
        if _reads_coverage(code, self.COVERAGE_CHECKS):
            return CheckResult(self.name)

        where = ", ".join(sorted(capped) + sorted(defaulted))
        return CheckResult(
            self.name,
            issues=[
                CodeIssue(
                    "coverage",
                    f"The spec asks for all of them, but {where} caps the fetch "
                    "and nothing checks whether that was all. Raise the limit, "
                    "and inside the step read `.complete` — a result reshaped "
                    "or returned from a step is a plain list and has lost it. "
                    "Carry the coverage out with the data and say what the "
                    "output covers, e.g. `results.summary()` -> '100 of 312'.",
                    "warning",
                )
            ],
        )

    def _paged_calls_without_a_cap(self, code: str) -> set[str]:
        """Paged operations called with no row cap at all.

        The manifest is the only place that knows an operation pages — derived
        there from the return type, so a toolset author writes it once. Without
        the registry this stage had to look for a literal, and a call that
        simply never passed one was invisible.
        """
        registry = self._registry
        if registry is None or not getattr(registry, "list_toolsets", None):
            return set()
        try:
            paged = {
                op.function
                for toolset_id in registry.list_toolsets()
                for op in (registry.get(toolset_id).all_operations() or [])
                if op.pagination and op.function
            }
        except Exception:
            # A fake registry in a test need not answer this, and a stage that
            # cannot look something up has found nothing — not a finding.
            return set()
        if not paged:
            return set()

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return set()

        found: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            named = _called_operation(node)
            if named not in paged:
                continue
            if any(kw.arg in CAP_KEYWORDS for kw in node.keywords):
                continue
            found.add(f"{named}() (paged, no limit given)")
        return found


#: Every keyword a shipped toolset uses to cap a read. Recognising only
#: `max_results` and `limit` left `num_results` (Exa) invisible, and a cap this
#: does not know is a cap this stage cannot see.
CAP_KEYWORDS: frozenset[str] = frozenset(
    {"max_results", "limit", "page_size", "per_page", "num_results", "top", "count"}
)


def _called_operation(node: ast.Call) -> str:
    """The operation a call names, through ``ctx.step`` as well as directly.

    ``ctx.step(jira_search_issues, jql)`` calls ``step``; the name that matters
    is its first argument. Both forms are sanctioned by the prompt, so a check
    that saw only one would be half a check.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if func.attr in ("step", "node") and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name):
                return first.id
        return func.attr
    return ""

def _capped_calls(code: str) -> set[str]:
    """Calls that pass a row cap, by keyword name.

    Parsed rather than pattern-matched: ``max_results=100`` in a comment or a
    docstring is not a cap, and a string search cannot tell the difference.

    A cap bound to a name — ``CAP = 100`` then ``limit=CAP`` — counts too. Only
    literals were recognised before, so lifting the number into a constant, the
    ordinary thing a person does while tidying, silently disarmed the check.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in CAP_KEYWORDS:
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(
                keyword.value.value, int
            ):
                found.add(f"{keyword.arg}={keyword.value.value}")
            elif isinstance(keyword.value, ast.Name):
                found.add(f"{keyword.arg}={keyword.value.id}")
    return found


def _reads_coverage(code: str, markers: tuple[str, ...]) -> bool:
    """Whether the code actually reads a coverage field, as an attribute or call.

    A substring scan over the whole file stood here, and ordinary code disarmed
    it: ``task.completed`` — a real field on Asana and ClickUp rows — contains
    ``.complete``, and ``(end - start).total_seconds()`` contains ``.total``.
    Either one silenced the entire stage while saying nothing about coverage.
    Matching attribute *names* exactly, on parsed attribute access, is the
    difference between "this file mentions the word" and "this code asks the
    question".
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # A file that does not parse cannot be judged. `compile` is blocking and
        # runs first, so this only happens when the pipeline was assembled
        # without it — say nothing rather than pass it.
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in markers:
            return True
        # `summary()` reaches here as the func of a Call, already covered above;
        # a bare name is the destructured form: `complete = results.complete`.
        if isinstance(node, ast.Name) and node.id in markers:
            return True
    return False


#: See :attr:`ResolutionStage.COMMON`.
COMMON_SPEC_WORDS = frozenset({
    "a", "all", "an", "and", "any", "are", "as", "at", "be", "by", "each",
    "every", "find", "for", "from", "get", "give", "her", "his", "in", "into",
    "is", "issue", "issues", "it", "its", "list", "me", "my", "of", "on", "or",
    "our", "out", "show", "status", "stories", "story", "task", "tasks", "the",
    "their", "them", "there", "these", "this", "those", "ticket", "tickets",
    "to", "us", "was", "were", "what", "when", "which", "who", "with", "work",
    "workflow", "you", "your",
})


def _query_literals(code: str) -> list[str]:
    """String constants that reach a call, directly or through one assignment.

    A query is a string *passed somewhere*. Prose is a string returned to a
    person. Scanning every literal cannot tell those apart, and the cost of not
    trying was a check that flagged a workflow's own explanation of itself —
    the explanation the resolution ladder had just told it to write.

    One hop is deliberate. ``jql = "..."`` then ``ctx.step(search, jql)`` is how
    a readable workflow is written, and following it costs a dict; following
    arbitrary dataflow would need a solver, and everything past one hop is a
    guess about intent rather than a fact about the code.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    assigned: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(
            node.value, ast.Constant
        ):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                assigned.setdefault(target.id, []).append(node.value.value)

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for argument in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(argument, ast.Constant) and isinstance(
                argument.value, str
            ):
                found.append(argument.value)
            elif isinstance(argument, ast.Name):
                found.extend(assigned.get(argument.id, ()))
    return found


#: Where one clause of a query ends and the next begins. Shared by the operand
#: and the scope, so the two can never disagree about the boundary.
_CLAUSE_END = r"\b(?:and|or|order\s+by)\b|[)\]]"


@dataclass(frozen=True, slots=True)
class FuzzyMatch:
    """A match operator's operand, and what the rest of its query says."""

    word: str
    """The thing being matched on — the half that came from the spec."""

    scope: frozenset[str]
    """Every word in the query's *other* clauses, field names and values alike.

    Not operands, and never reported as such: evidence about which namespace
    the operand is being matched *within*. Both sides are kept because either
    can carry the namespace — ``project = PA`` names it on the left,
    ``issuetype = Epic`` on the right — and which one does is a property of the
    query language rather than of this check.
    """


def _fuzzy_operands(
    literal: str, operators: tuple[str, ...]
) -> list[FuzzyMatch]:
    """The words a match operator is matching *on*, in *literal*.

    ``text ~ "saas"`` yields ``saas``; the field it matches against, and every
    other clause in the query, is not an operand and is not returned. That
    distinction is the whole of the check: the field name comes from the
    system's schema and the operand comes from whoever wrote the spec.

    The other clauses come back as ``scope`` rather than being discarded,
    because dropping them made two different pieces of code identical here.
    ``summary ~ "saas"`` is a guess; ``issuetype = Epic AND summary ~ "saas"``
    is a search of one namespace by name, which is what resolution *is* for an
    entity whose service exposes no other lookup — a Jira epic is an issue, so
    there is no epic endpoint to call instead. Reading only the operand flagged
    both, and the second has no repair: the ladder's own advice produces it,
    and its escape hatch ("nothing bears that name") is false when something
    does. Whether a scope word names a real namespace is decided by the caller,
    against the resolvers the registry declares — nothing here knows the name
    of any namespace, or of any service.
    """
    lowered = literal.lower()

    # Each operand's span, so the scope can be the query with all of them
    # removed. Taking "everything but this clause" per operator instead would
    # let one fuzzy operand land in another's scope — `summary ~ "project x"
    # AND text ~ "saas"` would read as scoped to a project, by a word that is
    # itself an unresolved guess.
    spans: list[tuple[int, int]] = []
    for operator in operators:
        start = 0
        while (found := lowered.find(operator, start)) != -1:
            start = found + len(operator)
            # The clause ends at a boolean keyword, a closing bracket, or the
            # end of the string; what follows belongs to the next clause.
            end = start + len(re.split(_CLAUSE_END, lowered[start:], maxsplit=1)[0])
            spans.append((start, end))

    if not spans:
        return []

    kept = list(lowered)
    for start, end in spans:
        kept[start:end] = " " * (end - start)
    scope = frozenset(_words("".join(kept)))

    return [
        FuzzyMatch(word, scope)
        for start, end in spans
        for word in _words(lowered[start:end])
    ]


def _body_of(node: ast.AST) -> list[ast.stmt]:
    """A node's statement list, or empty.

    ``body`` is a list on a module, class and function, and a *single node* on
    an ``IfExp`` or a lambda — so it is narrowed here rather than assumed at the
    one call site that reads it.
    """
    body = getattr(node, "body", None)
    return body if isinstance(body, list) else []


def _string_literals(code: str) -> list[str]:
    """Every string constant in the source, parsed rather than pattern-matched.

    A query lives in a string, and only in a string — a ``~`` in a comment or
    in this very docstring is not a query, and a regex over the file cannot
    tell those apart.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    # A docstring is a string constant like any other to `ast.walk`, and this
    # function claimed to exclude one while including every one. It read its own
    # prose as data: a module explaining that a page redirects to
    # `/reserve/message` was reported as a CSS selector, on correct code, every
    # time — the failure a stage must never have, because the repair loop acts
    # on findings and unchanged code is how a model disagrees.
    # `body` is a list on a module, class and function, and a single node on
    # an `IfExp` or a lambda — so it is filtered to lists rather than assumed.
    prose = {
        id(statement.value)
        for node in ast.walk(tree)
        for statement in _body_of(node)
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
    ]


@dataclass
class ResolutionStage:
    """Did a word from the spec reach a query without being looked up?

    From an observed generation. Asked for "all the stories in **sas work**",
    the agent wrote ``text ~ "saas"`` — it corrected the spelling on its own and
    fuzzy-matched free text instead of resolving "sas work" to a project or an
    epic. It validated clean, ran clean, and returns whatever happens to
    contain the substring, which is the failure entity resolution exists to
    prevent: a query built from the spec's vocabulary rather than the system's.

    The signal is deliberately narrow — a **match operator** (``~``,
    ``contains``, ``LIKE``) with an operand that came from the spec. An exact
    comparison is left alone, because ``status = "In Progress"`` is a plausible
    resolved value while ``text ~ "saas"`` is a guess by construction. Near
    misses count too: an operand one or two characters from a spec word is a
    silent spelling correction, which is a guess wearing an even better
    disguise.

    **Narrow in two ways the first version was not**, both found by running it:

    It reads the *operand* of the match operator, not every word in the string.
    Scanning the whole literal flagged ``due < now() AND ... text ~ "saas"`` for
    the word ``due`` — which is in the spec ("passed **due** date") and is also
    a JQL **field name**. The schema's own vocabulary is not an entity anybody
    has to resolve, and a check that says otherwise is telling the model to look
    up the word ``due``.

    And it reads only literals that reach a *call*. Every literal in the file
    was scanned, so the report sentence ``'No overdue tickets mentioning "saas"
    were found (searched ... text ~ "saas")'`` was flagged — a human-readable
    message, quoting the query for the reader, which is precisely what rung 4
    instructs: "keep the text match, **say so in what the workflow returns**".
    The check was punishing the model for following it.

    **An error rather than a warning**, on ``OutcomeStage``'s reasoning: the
    repair loop runs on ``report.errors``, so a warning here was a finding the
    model was never shown, and the guess shipped every time. What makes that
    safe is that code returned *unchanged* ends the repair — a model that has
    established the word names nothing in the system says so by leaving the
    file alone, and the message tells it that in as many words.

    **What it says is the other half of the fix.** The first version said "look
    the entity up", and the generation it was written for had already looked —
    in two namespaces of the several a tracker has, missing an epic by that
    name. A name lives in *some* namespace and a service has many, so the
    message names the resolvers the registry actually declares and then says to
    search the remaining namespaces by type. Advice that stops at "look it up"
    is advice the model has already followed.
    """

    name: str = "resolution"
    cost: int = 16
    blocking: bool = False

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry

    #: Operators that match on substance rather than identity.
    FUZZY: ClassVar[tuple[str, ...]] = ("~", "contains", "like ", "in text")

    #: Words too common to be anyone's name for anything. Not a stopword list
    #: for its own sake — every entry here is a word a spec uses *about* the
    #: task ("show all stories") rather than to name something in the system.
    COMMON: ClassVar[frozenset[str]] = COMMON_SPEC_WORDS

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        terms = _spec_terms(context.spec or "", self.COMMON)
        if not terms:
            return CheckResult(self.name)

        namespaces = self._namespaces()
        issues: list[CodeIssue] = []
        for literal in _query_literals(code):
            for found in _fuzzy_operands(literal, self.FUZZY):
                word = found.word
                match = _closest(word, terms)
                if match is None:
                    continue
                how = (
                    f"the spec's {match!r}"
                    if word == match
                    else f"{match!r} from the spec, respelled"
                )
                scoped = sorted(found.scope & namespaces)
                if scoped:
                    issues.append(self._scoped(literal, how, scoped))
                    break
                issues.append(
                    CodeIssue(
                        "resolution",
                        f"{literal.strip()!r} matches on {how}. A fuzzy text "
                        "search is not a resolution — it returns whatever "
                        "happens to contain the substring, and nothing when "
                        "the wording differs. Find what the word names before "
                        f"you match on it: {self._where_to_look()} A name that "
                        "is not a person or a field is usually a container or "
                        "a grouping — a project, board, space, folder, epic, "
                        "label, component, tag — and each is a different "
                        "namespace, so one that came back empty says nothing "
                        "about the others. Filter on the id you find. If it "
                        "stays ambiguous, resolve it in a ctx.agent() step "
                        "with the candidates. If you have searched every "
                        "namespace and nothing bears that name, then it is "
                        "subject matter rather than a thing: keep the text "
                        "match, say so in what the workflow returns, and "
                        "return the code unchanged — that is accepted and "
                        "ends the repair.",
                        "error",
                    )
                )
                break
        return CheckResult(self.name, issues=issues[:3])

    def _namespaces(self) -> frozenset[str]:
        """The entity kinds this deployment declares a resolver for.

        The vocabulary is read from the manifests rather than listed here, for
        the reason ``opaque_ids`` exists: which namespaces a service has is the
        service's own statement, and a list of them in the agent layer is a
        list of vendors in the agent layer. A toolset that declares
        ``resolves="board"`` teaches this check about boards with no change
        here.

        Empty without a registry, which makes every match unscoped and so
        restores exactly the behaviour that shipped before scope was read at
        all. Failing that way round matters: the check errors, and an error the
        model cannot act on is the loop this method exists to end.
        """
        registry = self._registry
        if registry is None or not getattr(registry, "list_toolsets", None):
            return frozenset()
        kinds: set[str] = set()
        try:
            for toolset_id in registry.list_toolsets():
                kinds.update(registry.get(toolset_id).resolvers() or {})
        except Exception:
            return frozenset()
        return frozenset(kinds)

    def _scoped(self, literal: str, how: str, scoped: list[str]) -> CodeIssue:
        """A match that names the namespace it is searching.

        A **warning**, where an unscoped match is an error, and the gap between
        the two is the point. The repair loop runs on ``report.errors``, so an
        error has to be actionable; this code has already done what the ladder
        asks — it picked a namespace and searched it by name — and for an
        entity whose service exposes no separate lookup there is nothing
        further to call. Erroring here asks for a resolver that does not exist,
        which no repair can produce and no escape hatch covers.

        What is left worth saying is the ambiguity, which is real: a name
        search can return two rows, and taking the first is the guess this
        stage exists to catch, one step later.
        """
        return CodeIssue(
            "resolution",
            f"{literal.strip()!r} matches on {how}, scoped to "
            + ", ".join(repr(kind) for kind in scoped)
            + ". That is a namespace search rather than a blind text match, "
            "and for an entity whose service exposes no other lookup it is "
            "the resolution — nothing further to call. What it does not "
            "settle is ambiguity: a name search can match more than one row, "
            "and taking the first is the same guess one step later. Read the "
            "result's own count before you use it — if exactly one matches, "
            "filter on its id; if several do, hand the candidates to a "
            "ctx.agent() step and let it choose; if none do, return that as "
            "the answer rather than an empty list. No change is needed if the "
            "code already does this.",
            "warning",
        )

    def _where_to_look(self) -> str:
        """The resolvers this deployment actually has, named.

        Read from the registry rather than listed here, so a host's own toolset
        is as findable as a shipped one and nothing in this module has to know
        which toolsets exist.
        """
        registry = self._registry
        if registry is None or not getattr(registry, "list_toolsets", None):
            return "call_read_operation over the toolset's own resolvers."
        named: list[str] = []
        try:
            for toolset_id in registry.list_toolsets():
                manifest = registry.get(toolset_id)
                for kind, op in (manifest.resolvers() or {}).items():
                    # The *op path*, because the next clause says to call it
                    # with `call_read_operation`, and that takes
                    # `<toolset>.<op_id>`. Naming `op.function` here handed the
                    # model a symbol that tool cannot accept — the same
                    # mismatch the registry's own "Operations:" line had, and
                    # it cost a turn each time to be told so.
                    named.append(f"{toolset_id}.{op.id} ({kind})")
        except Exception:
            return "call_read_operation over the toolset's own resolvers."
        if not named:
            return "call_read_operation over the toolset's own read operations."
        return (
            "the toolsets here declare "
            + ", ".join(sorted(named))
            + " — call them with call_read_operation."
        )


def _spec_terms(spec: str, common: frozenset[str]) -> set[str]:
    """The words in a spec that could name something in a system."""
    return {word for word in _words(spec.lower()) if word not in common and len(word) > 2}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def _closest(word: str, terms: set[str]) -> str | None:
    """A spec term this word is, or was probably meant to be.

    ``difflib`` rather than a hand-rolled distance: the same ratio the Jira
    user resolver already uses, so "close enough" means one thing across the
    codebase.
    """
    if word in terms:
        return word
    if len(word) < 4:
        return None
    found = difflib.get_close_matches(word, terms, n=1, cutoff=0.8)
    return found[0] if found else None


class IdentifierStage:
    """Where did that identifier come from?

    ``ResolutionStage`` catches a spec's *word* reaching a query. This catches
    the failure one step later: a spec word that became an **identifier nobody
    looked up**. Asked for "issues over 8 story points", a model that cannot
    resolve the field writes ``customfield_10016`` — a number that is right on
    some Jira site and wrong on this one. It compiles, it passes the smoke run
    (fakes ignore arguments), and in production it either 400s or reads a field
    that means something else entirely.

    Baking an id in is *correct*, and that is what makes this hard: the ladder
    in ``DEFAULT_SYSTEM_PROMPT`` explicitly says to resolve once and write the
    id into the code with the name in a comment. A resolved id and an invented
    one are identical in the file. So the evidence is not the code — it is
    whether a resolver for that entity kind was actually *called* while
    authoring, which ``CheckContext.resolved_kinds`` reports from the agent's
    own tool calls.

    Three things it deliberately does not flag: an id the spec supplied (the
    caller knows it, and it is not a guess), an id for a kind that *was*
    resolved, and any id belonging to a toolset that declares no
    ``opaque_ids`` pattern — most of them. **Silence here is weak evidence**,
    which is why this is a warning rather than a blocking error.
    """

    name: str = "identifiers"
    cost: int = 18
    blocking: bool = False

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        registry = self._registry
        if registry is None or not getattr(registry, "list_toolsets", None):
            return CheckResult(
                self.name, skipped=True, reason="no toolset registry to check against"
            )

        try:
            patterns = self._patterns(registry)
        except Exception:
            return CheckResult(
                self.name, skipped=True, reason="registry could not be read"
            )
        if not patterns:
            return CheckResult(
                self.name, skipped=True, reason="no toolset declares an id pattern"
            )

        spec = context.spec or ""
        issues: list[CodeIssue] = []
        seen: set[str] = set()

        for literal in _string_literals(code):
            for regex, kind, toolset_id, resolver in patterns:
                for found in regex.findall(literal):
                    if found in seen or found in spec:
                        continue
                    if kind in context.resolved_kinds:
                        continue
                    seen.add(found)
                    issues.append(
                        CodeIssue(
                            "identifiers",
                            f"{found!r} is a {toolset_id} {kind} id that the spec "
                            "does not mention and nothing looked up. These differ "
                            "per account, so a remembered one is wrong somewhere "
                            "and fails silently — the wrong field, or zero rows. "
                            f"Resolve it with {resolver} and use what it returns.",
                            "warning",
                        )
                    )

        return CheckResult(self.name, issues=issues)

    def _patterns(self, registry: Any) -> list[tuple[Any, str, str, str]]:
        """``(compiled, entity kind, toolset id, the resolver to call)``.

        A list rather than a dict keyed by the pattern: ``re.compile`` returns
        a *cached* object for an identical string, so two toolsets declaring
        the same shape would collapse to one entry and the second would be
        dropped without a word.

        Built per run rather than cached: a registry is per-Runtime, and a
        host that registers its own toolset should have its patterns honoured
        without this module knowing it exists.
        """
        found: list[tuple[Any, str, str, str]] = []
        for toolset_id in registry.list_toolsets():
            manifest = registry.get(toolset_id)
            declared = getattr(manifest, "opaque_ids", None) or {}
            if not declared:
                continue
            resolvers = manifest.resolvers()
            for pattern, kind in declared.items():
                op = resolvers.get(kind)
                # A pattern naming a kind nothing resolves has no advice to
                # give, and an issue that cannot say what to do instead is
                # noise.
                if op is None:
                    continue
                try:
                    compiled = re.compile(pattern)
                except re.error:
                    continue
                found.append((compiled, kind, toolset_id, op.function or op.id))
        return found


class GrantStage:
    """Does a declared grant name a toolset that exists?

    A grant entry that matches nothing permits nothing — ``allows_operation``
    simply never returns True for it — so a typo produces a workflow that looks
    restricted to what it lists and is in fact restricted to nothing. The
    failure surfaces much later, as an agent reporting that it could not find a
    tool, with nothing pointing back at the declaration.

    Non-blocking: the code is otherwise correct, and the repair is one string.
    """

    name: str = "grants"
    cost: int = 12
    blocking: bool = False

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        registry = self._registry
        if registry is None or not getattr(registry, "list_toolsets", None):
            # `skipped` is a bool and `reason` is the string. Passing the
            # explanation as `skipped` worked by truthiness and left `reason`
            # empty, so anything rendering *why* a stage was skipped — which is
            # the whole point of distinguishing skipped from passed — showed
            # nothing.
            return CheckResult(
                self.name, skipped=True, reason="no toolset registry to check against"
            )

        issues: list[CodeIssue] = []
        for grant in _declared_grants(code):
            for issue in grant.validate_against(toolsets=registry):
                issues.append(
                    CodeIssue(
                        "grants",
                        f"{issue}. An entry that matches nothing permits "
                        "nothing, so this workflow would run with an empty "
                        "toolset rather than the one it appears to declare.",
                        "warning",
                    )
                )
        return CheckResult(self.name, issues=issues[:3])


def _declared_grants(code: str) -> list[Any]:
    """Every ``GrantSet(toolsets=[...])`` literal in a source file.

    Read from the AST rather than by importing: a stage must not execute the
    code it is checking, and the entries are literals by the time they matter.
    """
    from loom.security.grants import GrantSet

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    found: list[Any] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else (
            node.func.attr if isinstance(node.func, ast.Attribute) else ""
        )
        if name != "GrantSet":
            continue
        entries: list[str] = []
        for keyword in node.keywords:
            if keyword.arg != "toolsets" or not isinstance(keyword.value, ast.List):
                continue
            entries = [
                element.value
                for element in keyword.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
        if entries:
            found.append(GrantSet(toolsets=entries))
    return found


@dataclass
class PlacementStage:
    """Was the filter pushed to the service, or applied after fetching everything?

    Every integration LOOM ships can filter server-side — JQL, Gmail's ``q=``,
    Graph's ``$filter``, SOQL, HubSpot search — and nothing anywhere told the
    coding agent to use it. Fetching a whole project and keeping six rows in a
    comprehension compiles, lints, types, smoke-runs and replays clean, so it
    passed all ten stages while being wrong twice over: it pages an unbounded
    amount of somebody else's data, and the comprehension turns a ``Results``
    into a plain ``list``, discarding the coverage that would have said the
    fetch was truncated. The visible answer is a short, confident, incomplete
    list.

    A warning, not an error. Filtering locally is sometimes the only option —
    the predicate may not be expressible in the service's query language — and
    the point is to make that a decision rather than an accident.
    """

    name: str = "placement"
    cost: int = 17
    blocking: bool = False

    #: Names that mark a call as a *read* whose rows come from a service.
    READ_MARKERS: ClassVar[tuple[str, ...]] = (
        "search", "list_", "find_", "query", "fetch", "get_all",
    )

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return CheckResult(self.name)

        fetched = _names_bound_to_reads(tree, self.READ_MARKERS)
        if not fetched:
            return CheckResult(self.name)

        issues: list[CodeIssue] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
                continue
            for generator in node.generators:
                if not generator.ifs:
                    continue
                source = generator.iter
                if isinstance(source, ast.Name) and source.id in fetched:
                    issues.append(
                        CodeIssue(
                            "placement",
                            f"`{source.id}` comes from a service read and is then "
                            "filtered in Python. Push the predicate into the "
                            "query instead — JQL, `q=`, `$filter`, SOQL and "
                            "HubSpot search all take one — so the service returns "
                            "what you want rather than everything. Filtering "
                            "after the fact also drops the coverage: a "
                            "comprehension over a `Results` is a plain list, so "
                            "`.complete` is gone and a truncated fetch reads as a "
                            "complete answer. If the predicate genuinely cannot "
                            "be expressed server-side, use `.filtered(...)` to "
                            "keep it.",
                            "warning",
                        )
                    )
                    break
        return CheckResult(self.name, issues=issues[:3])


def _names_bound_to_reads(tree: ast.Module, markers: tuple[str, ...]) -> set[str]:
    """Variables assigned from something that reads rows out of a service.

    Both call shapes the prompt sanctions: ``await ctx.step(jira_search_issues,
    ...)`` and a direct ``await jira_search_issues(...)``.
    """

    def is_read(value: ast.expr) -> bool:
        call = value.value if isinstance(value, ast.Await) else value
        if not isinstance(call, ast.Call):
            return False
        named: list[str] = []
        func = call.func
        if isinstance(func, ast.Attribute):
            named.append(func.attr)
            # ctx.step(<operation>, ...) — the operation is the first argument,
            # so the name that matters is not the one being called.
            if func.attr in ("step", "node") and call.args:
                first = call.args[0]
                if isinstance(first, ast.Name):
                    named.append(first.id)
        elif isinstance(func, ast.Name):
            named.append(func.id)
        return any(marker in name for name in named for marker in markers)

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and is_read(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and is_read(node.value)
            and isinstance(node.target, ast.Name)
        ):
            found.add(node.target.id)
    return found


class JudgementStage:
    """Is the answer produced by a model that a rule could have produced?

    From an observed generation. Asked to list overdue tickets with their due
    dates, the agent fetched the rows in a ``@step`` — typed models carrying
    every field it needed — and then handed them to ``ctx.agent()`` to "fetch
    the details and produce a markdown table". The table came out right, so
    nothing downstream complained: it compiled, ran, replayed, and answered.

    What it cost is invisible in the file. A model call re-derives the answer
    on every run, adds a request and its latency to every run, and can drop,
    reorder or invent rows the code was already holding — the kind of wrong
    that reads as right. The prompt has always said formatting is a ``@step``;
    nothing checked it, and "nothing checked it" is why the rule was followed
    about as often as it was not.

    Two conditions coincide before it says anything, the discipline
    ``CoverageStage`` and ``OutcomeStage`` use:

    * The workflow's **answer** comes from a model call — not merely that one
      exists. An agent that classifies mid-flow, or resolves an ambiguous name,
      is judgement doing exactly its job and is left alone.
    * And the spec asked for **no judgement at all**. The vocabulary is
      deliberately generous, because a false positive here argues with a
      correct design while a false negative only fails to catch one: any hint
      that the request wants language, a decision or a classification and this
      stage stays quiet.

    An error rather than a warning, for ``OutcomeStage``'s reason — the repair
    loop reads ``report.errors`` — and safe for the same one: unchanged code
    ends the repair, so a model that judges the call necessary keeps it by
    leaving the file alone.
    """

    name: str = "judgement"
    cost: int = 19
    """Pure AST, beside the other static passes and well before running."""
    blocking: bool = False

    #: A spec asking for any of these is asking for judgement, and a model call
    #: is then the right implementation. Substrings, so "summarise", "summary"
    #: and "summarize" are one entry; stems over words for the same reason.
    WANTS_JUDGEMENT: ClassVar[tuple[str, ...]] = (
        "summar", "draft", "rewrite", "reword", "rephrase", "compose",
        "classif", "categor", "sentiment", "intent", "tone", "translat",
        "judge", "assess", "evaluat", "decide", "decision", "recommend",
        "suggest", "advis", "rank", "prioriti", "important", "urgen",
        "relevant", "attention", "risk", "insight", "analy", "explain",
        "interpret", "review", "triage", "critique", "opinion", "why",
        "generate", "write ", "reply", "respond", "answer",
        "joke", "poem", "haiku", "limerick", "story", "song", "lyric",
        "greeting", "caption", "slogan", "tagline", "brainstorm", "idea",
    )

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        spec = (context.spec or "").lower()
        if not spec:
            return CheckResult(
                self.name, skipped=True, reason="no spec to read the intent from"
            )
        if any(word in spec for word in self.WANTS_JUDGEMENT):
            return CheckResult(
                self.name, reason="the spec asks for judgement; a model call fits"
            )

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return CheckResult(self.name)

        lines = _model_answers(tree)
        if not lines:
            return CheckResult(self.name)

        if not _holds_data(tree):
            return CheckResult(
                self.name,
                reason="the workflow reads nothing; the model call is the answer",
            )

        where = ", ".join(f"line {line}" for line in lines[:3])
        return CheckResult(
            self.name,
            issues=[
                CodeIssue(
                    "judgement",
                    f"the workflow's answer is produced by ctx.agent() ({where}), "
                    "and the spec asks for data rather than judgement. Turning "
                    "rows the code already holds into a table, a list or a "
                    "count is a rule: write it in a @step. A model call there "
                    "re-derives the answer on every run, costs a request each "
                    "time, and can drop or invent rows that were already in "
                    "hand. Never ask a model to fetch a field either — the "
                    "read returns typed models, and a field missing from one "
                    "is asked for on the read, not from a model. Keep the "
                    "model call only where the spec asked for judgement; if it "
                    "did, return the code unchanged — that is accepted and "
                    "ends the repair.",
                    "error",
                )
            ],
            detail=lines,
        )


def _model_answers(tree: ast.Module) -> list[int]:
    """Lines where a workflow returns something a model produced.

    Follows assignment rather than matching a shape, because the same thing is
    written four ways — returned inline, bound then returned, ``.text()`` taken
    off it, or interpolated into an f-string that is returned. Chasing names to
    a fixed point covers all four and does not care which one a model wrote.
    """

    def is_agent_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "agent"
        )

    def is_laundered(node: ast.AST) -> bool:
        """A durable call whose result is the code's, whatever went into it.

        ``return await ctx.step(format_report, rows, verdict.text())`` is the
        shape the prompt asks for when a task is part judgement and part rule —
        a model decides, a step composes the answer. Walking through it would
        report that split as the failure it is the fix for.
        """
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("step", "node")
        )

    def carries(node: ast.AST, names: set[str], acted: frozenset[str]) -> bool:
        if isinstance(node, ast.Name):
            return node.id in names and node.id not in acted
        if is_agent_call(node):
            return True
        if is_laundered(node):
            return False
        return any(
            carries(child, names, acted) for child in ast.iter_child_nodes(node)
        )

    def spread(acted: frozenset[str]) -> set[str]:
        tainted: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                targets, value = _assignment(node)
                if not targets or not carries(value, tainted, acted):
                    continue
                for target in targets:
                    if target not in tainted and target not in acted:
                        tainted.add(target)
                        changed = True
        return tainted

    def acted_upon(tainted: set[str]) -> frozenset[str]:
        """Model-derived names a durable call *consumed*.

        The dual of ``is_laundered``, and the same boundary read the other way.
        That says a value coming **out** of a durable call is the code's,
        whatever went in; this says a value going **in** is a decision the code
        acted on rather than the answer it produced.

        Without it, the ladder's own rung 3 was reported as this failure. An
        ambiguous name resolved by ``ctx.agent()`` and then used to *fetch* —
        ``ctx.step(search, f"parentEpic = {key} ...")`` — is a model deciding
        and a step answering, which is the split this stage exists to protect.
        It fired anyway, because the resolved key was also echoed in the
        heading above the table: labelling the answer, not producing it. Two
        stages then disagreed about the same correct code, with the resolution
        check instructing exactly what this one refused.
        """
        consumed: set[str] = set()
        sources: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if is_laundered(node):
                assert isinstance(node, ast.Call)
                for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                    for inner in ast.walk(argument):
                        if isinstance(inner, ast.Name) and inner.id in tainted:
                            consumed.add(inner.id)
                continue
            targets, value = _assignment(node)
            for target in targets:
                for inner in ast.walk(value):
                    if isinstance(inner, ast.Name) and inner.id in tainted:
                        sources.setdefault(target, set()).add(inner.id)

        # Consumption has to follow the same bindings taint does, or the
        # verdict turns on whether the query was interpolated inside the call
        # or bound to a name one line above it. Those are the same code, they
        # are both what a model writes, and they disagreed — a check flaky on
        # formatting is one nobody can act on.
        acted: set[str] = set()
        frontier = list(consumed)
        while frontier:
            name = frontier.pop()
            if name in acted:
                continue
            acted.add(name)
            frontier.extend(sources.get(name, ()))
        return frozenset(acted)

    tainted = spread(frozenset())
    acted = acted_upon(tainted)
    if acted:
        tainted = spread(acted)

    found: list[int] = []
    for function in _workflow_bodies(tree):
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Return)
                and node.value is not None
                and carries(node.value, tainted, acted)
            ):
                found.append(node.lineno)
    return sorted(found)


def _assignment(node: ast.AST) -> tuple[list[str], ast.expr]:
    """The names *node* binds and the expression it binds them to.

    One reader for the shapes an assignment comes in, because the taint walk
    and the consumption walk have to agree about what a binding is. They read
    the same code for opposite purposes, and a shape one understands and the
    other does not is a disagreement that shows up as a verdict, not as an
    error.
    """
    if isinstance(node, ast.Assign):
        value = node.value
        targets = node.targets
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        value = node.value
        targets = [node.target]
    else:
        return [], ast.Constant(value=None)
    return [t.id for t in targets if isinstance(t, ast.Name)], value


def _holds_data(tree: ast.Module) -> bool:
    """Does the workflow have anything in hand for a model to re-derive?

    This stage's whole claim is that rows the code *already holds* are turned
    into an answer by a model that did not need to. A workflow that reads
    nothing holds nothing: ``sleep two minutes, then tell me a joke`` has no
    other possible source for its answer, so reporting the model call there
    asks for the one thing the file cannot contain, and the repair loop spends
    its rounds rewriting correct code into another spelling of itself — the
    no-passing-state failure ``FuzzyMatch`` was fixed for.

    Two ways data arrives, and nothing else counts:

    * a durable data call in the body — ``ctx.step`` or ``ctx.node``, the same
      pair ``is_laundered`` recognises, because they are what the prompt tells
      generated code to fetch through. ``ctx.sleep`` and ``ctx.agent`` are
      deliberately not among them: one produces no value and the other is the
      call under suspicion.
    * a workflow parameter the body actually **references**. A declared but
      unused ``input_data`` is what a model writes when a spec supplies no
      input, and counting it would put every such workflow back on this path.
    """
    for function in _workflow_bodies(tree):
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("step", "node")
            ):
                return True

        parameters = {
            argument.arg
            for argument in [*function.args.posonlyargs, *function.args.args,
                             *function.args.kwonlyargs]
        }
        parameters.discard("ctx")
        parameters.discard("self")
        if any(
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in parameters
            for node in ast.walk(function)
        ):
            return True
    return False


def _workflow_bodies(tree: ast.Module) -> list[ast.AsyncFunctionDef]:
    """The functions the ``@workflow`` decorator marks as workflow bodies.

    Only these, because a helper returning model output says nothing on its
    own — what matters is whether that output is the *run's* answer.
    """
    found: list[ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            call = decorator.func if isinstance(decorator, ast.Call) else decorator
            named = (
                call.attr
                if isinstance(call, ast.Attribute)
                else call.id
                if isinstance(call, ast.Name)
                else ""
            )
            if named == "workflow":
                found.append(node)
                break
    return found



@dataclass
class ObservedTargetsStage:
    """Do the controls this code addresses exist on the page that was observed?

    The cheapest question there is about a browser workflow, and nothing was
    asking it. The probe already renders the page, censuses every control, hands
    the model a summary, and throws the census away — so a workflow addressing
    a control that was demonstrably not there passed every stage.

    The run this was written for: ``/reserve`` redirected to an interstitial
    policy notice, the census reported a nav button and two identically-named
    "Confirm and continue" buttons, and the generated workflow addressed a party
    size control, a date picker and three contact fields. Not one of them
    appeared in what the agent had seen. The pipeline was green.

    **A warning, and worded as coverage rather than as a defect**, because the
    honest reading of an unconfirmed target is *"observation could not reach
    this"* and not *"this is wrong"*. Observation is single-shot: it navigates
    and reads, so any control that appears only after an interaction is
    legitimately absent from the census while the code addressing it is
    perfectly correct. Reporting that as an error would put the repair loop to
    work rewriting working code — the failure ``SelectorStage`` and
    ``BrowserEffectStage`` are both careful to avoid.

    What it *can* say without guessing is the count, and the count is where the
    signal lives: some targets unconfirmed is ordinary, and **none confirmed**
    means the code and the observation are describing different pages.
    """

    name: str = "observed-targets"
    cost: int = 19
    blocking: bool = False

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        if "browser." not in code:
            return CheckResult(self.name, skipped=True,
                               reason="not a browser workflow")
        pages = [p for p in (context.observed or []) if getattr(p, "names", ())]
        if not pages:
            # Nothing looked, or nothing readable came back. "I could not check"
            # is not "I checked and it was fine", and it is certainly not a
            # finding about the code.
            return CheckResult(self.name, skipped=True,
                               reason="no page census was captured while authoring")

        # Reported before, and independently of, target coverage. A workflow
        # that navigates somewhere the probe was bounced off is looking at a
        # census of the *destination*, and that stays true however many of its
        # targets happen to match — including when it dodges literal names
        # altogether by resolving every control from an intent, which is exactly
        # what an agent does when it cannot see the page.
        hops = _redirected_navigations(code, pages)
        detour = [
            CodeIssue(
                "browser-targets",
                f"This workflow navigates to {hop.target}, which redirected to "
                f"{hop.landed} when it was observed. The controls reported to "
                "you are that second page's. If the page you actually want is "
                "behind it, nothing you were shown describes it.",
                "warning",
            )
            for hop in hops
        ]

        targeted = _targeted_names(code)
        if not targeted:
            return CheckResult(
                self.name, issues=detour,
                reason="no role-and-name targets in this workflow",
                skipped=not detour,
            )

        seen: set[str] = set()
        for page in pages:
            seen.update(name.casefold() for name in page.names)

        confirmed = [n for n in targeted if _matches(n, seen)]
        missing = [n for n in targeted if not _matches(n, seen)]
        note = (
            f"{len(confirmed)}/{len(targeted)} target(s) confirmed on the "
            f"{len(pages)} page(s) observed"
        )
        if not missing:
            return CheckResult(self.name, issues=detour, reason=note)

        redirected = [p for p in pages if getattr(p, "redirected", False)]
        detail = (
            f"Observed: {', '.join(sorted(seen)[:12]) or 'nothing named'}."
        )
        if redirected:
            hop = redirected[0]
            detail += (
                f" Note that {hop.target} redirected to {hop.landed}, so the "
                "census above may describe a page in front of the one you want."
            )

        if confirmed:
            message = (
                f"{len(missing)} browser target(s) were not in the census of any "
                f"page observed while authoring: "
                f"{', '.join(repr(n) for n in missing[:6])}. Observation is "
                "single-shot — it navigates and reads, so a control that only "
                f"appears after an interaction is legitimately absent. {detail}"
            )
        else:
            message = (
                "None of this workflow's browser targets appear in the census of "
                "any page observed while authoring, which usually means the code "
                "and the observation describe different pages rather than that "
                f"every control is dynamic. Targets: "
                f"{', '.join(repr(n) for n in missing[:6])}. {detail}"
            )
        return CheckResult(
            self.name,
            issues=[*detour, CodeIssue("browser-targets", message, "warning")],
            reason=note,
        )


def _redirected_navigations(code: str, pages: list[Any]) -> list[Any]:
    """Observed pages this workflow navigates to that were *not* where the
    probe ended up.

    Matched on the URL the code navigates to against the URL that was
    *requested*, never the one that was reached: a workflow going straight to
    the destination has already accounted for the hop and has nothing to learn
    from it. One report per distinct hop, since a flow that opens the same page
    twice has one problem, not two.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    urls = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(("http://", "https://"))
    }
    hops: dict[str, Any] = {}
    for page in pages:
        if getattr(page, "redirected", False) and page.target in urls:
            hops.setdefault(page.target, page)
    return list(hops.values())


def _matches(name: str, seen: set[str]) -> bool:
    """Whether *name* names something the census named.

    Substring in both directions, because an accessible name is routinely a
    superset of what a caller addresses it by ("2 Guests" against "Guests") and
    routinely a subset of a visible label. Loose on purpose: this stage's error
    of choice is silence, so a near-match is treated as a match.
    """
    folded = name.casefold().strip()
    if not folded:
        return True
    return any(folded == s or folded in s or s in folded for s in seen)


def _targeted_names(code: str) -> list[str]:
    """Every ``name="..."`` passed to a Target/TargetIn constructor.

    Read from the constructor rather than from any string that looks like a
    label, so prose, comments and unrelated keyword arguments are never mistaken
    for an address. A dynamic name — an f-string, a variable — is skipped
    outright: it cannot be checked, and guessing at it is how a stage starts
    firing on correct code.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        label = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if label not in ("Target", "TargetIn"):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "name"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
                and kw.value.value.strip()
            ):
                found.append(kw.value.value)
    seen: dict[str, None] = {}
    for name in found:
        seen.setdefault(name, None)
    return list(seen)


class ConnectionStage:
    """Does this file import a toolset the machine has no credential for?

    Only after `AuthSpec` made the requirement declarable: the credential a
    client reads was a keyword default inside one file, so nothing outside it
    could learn the name, let alone check for one.

    **A warning, and never an error.** The repair loop reads `report.errors`
    and unchanged code ends the repair, so an error here would ask a model to
    fix a *machine* — and the cheapest fix available to it is to stop importing
    the toolset, which ships a workflow that passes every remaining stage
    having removed the integration the spec was about. This repository has hit
    that shape three times (`AutoRespondChannel`, `FakeBrowserProvider`, the
    gutted repair) and it is the deliberate opposite of `BrowserEffectStage`'s
    error case, where the run genuinely cannot reach the write.

    What it is for is the summary: `loom connect jira` printed beside a file
    that will otherwise fail on its first real call.
    """

    name: str = "connections"
    cost: int = 14
    blocking: bool = False

    def __init__(self, registry: Any = None, credentials: Any = None) -> None:
        self._registry = registry
        self._credentials = credentials

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        if self._registry is None:
            return CheckResult(self.name)
        try:
            from loom.connectors.inspect import ConnectionInspector

            inspector = ConnectionInspector(self._registry, self._credentials)
            wanted = self._imported_toolsets(code, context)
            statuses = [await inspector.status(t) for t in sorted(wanted)]
        except Exception:
            # A check that cannot run has found nothing, which is not passing —
            # and is also not this file's defect.
            return CheckResult(self.name, skipped=True)

        missing = [s for s in statuses if not s.usable]
        if not missing:
            return CheckResult(self.name)
        return CheckResult(
            self.name,
            issues=[
                CodeIssue(
                    "connections",
                    f"{status.toolset} is not configured here"
                    + (f" — {status.how}" if status.how else "")
                    + (
                        f" (needs {', '.join(status.missing_fields)})"
                        if status.missing_fields
                        else ""
                    )
                    + ". The code is fine; the machine is not set up. Do not "
                    "change the workflow because of this.",
                    "warning",
                )
                for status in missing
            ],
        )

    def _imported_toolsets(self, code: str, context: CheckContext) -> set[str]:
        """Toolset ids this file imports, by their real module paths.

        `toolset_modules` maps id to module, and a module is what an import
        names — `google_calendar` lives at `loom.toolsets.google.calendar`, so
        matching on the id would miss every nested toolset and invent matches
        for none.
        """
        modules = context.toolset_modules or {}
        imported = {
            node.module
            for node in ast.walk(ast.parse(code))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        return {
            toolset_id
            for toolset_id, module in modules.items()
            if any(
                seen == module or seen.startswith(module + ".") for seen in imported
            )
        }


def default_stages(
    *,
    supervisor: Any = None,
    smoke: bool = True,
    registry: Any = None,
    credentials: Any = None,
) -> list[Any]:
    """The standard pipeline, cheapest first.

    Callers compose their own list when they want something else; this is the
    default, not the only arrangement.
    """
    stages: list[Any] = [
        CompileStage(),
        StaticStage(),
        GrantStage(registry),
        ConnectionStage(registry, credentials),
        CoverageStage(registry),
        PlacementStage(),
        ResolutionStage(registry),
        ProjectionStage(),
        CataloguePreferenceStage(),
        IdentifierStage(registry),
        SelectorStage(),
        ObservedTargetsStage(),
        BrowserEffectStage(),
        JudgementStage(),
        LintStage(),
        TypeStage(),
    ]
    if smoke:
        stages.extend([SmokeStage(), OutcomeStage(), ReplayStage()])
    if supervisor is not None:
        stages.append(CritiqueStage(supervisor=supervisor))
    return stages


#: A CSS or XPath selector, recognised structurally rather than by vibe.
#:
#: Each pattern is something ordinary English does not contain. That matters in
#: one direction more than the other: a stage that fires on prose is one people
#: switch off, which is the reasoning the redaction denylist already follows —
#: "a denylist that redacts ordinary data is one people switch off".
#:
#: A CSS *token* is a tag, class, id or attribute box, and they chain:
#: `div.card`, `button#submit[disabled]`. An earlier version required the
#: combinator to follow a bare tag, so `div.card > button.primary` — the single
#: most ordinary selector there is — went unreported.
_CSS_TOKEN = r"(?:[a-zA-Z][\w-]*|[.#][\w-]+|\[[^\]]+\])(?:[.#][\w-]+|\[[^\]]+\])*"

_SELECTOR_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^\s*\(?//[a-zA-Z*@\[]", "an XPath expression"),
    (r"/@?[a-zA-Z-]+\[@[a-zA-Z-]+\s*=", "an XPath attribute predicate"),
    (r"\[@[a-zA-Z-]+\s*=", "an XPath attribute predicate"),
    (rf"{_CSS_TOKEN}\s*[>+~]\s*{_CSS_TOKEN}", "a CSS selector chain"),
    (r"\[(?:data|aria)-[a-zA-Z-]+\s*[~^$*|]?=", "a CSS attribute selector"),
    (r":nth-(?:child|of-type|last-child)\(", "a CSS positional selector"),
    (r"^[.#][a-zA-Z_][\w-]*(?:[.#][\w-]+)+$", "a CSS class or id chain"),
    # Descendant combinator — whitespace — which is the one CSS operator that
    # ordinary prose also uses. A `.class` sigil on one side is the tell: `#a #b`
    # is two hashtags and stays silent, `#main .row` is a selector.
    (r"(?:^|\s)[.#][\w-]+\s+\.[\w-]+", "a CSS descendant selector"),
    (r"(?:^|\s)\.[\w-]+\s+[.#][\w-]+", "a CSS descendant selector"),
)


class SelectorStage:
    """A selector written into a browser workflow, instead of a name.

    The direct analogue of :class:`IdentifierStage`, one subsystem over. That
    one catches an identifier nobody looked up; this catches an **address
    nobody can check**.

    A CSS path or XPath read off a page during authoring is right on the render
    it was read from and silently wrong afterwards — and it fails the way this
    whole area keeps failing: by matching *nothing*, so the workflow runs,
    completes, and reports that there was nothing to do. ``tests/corpus``
    measured the alternative at 76% with no model call at all, which is what
    makes "use the accessible name" advice rather than aspiration.

    ``Target.css`` exists and is legitimate — some controls genuinely carry no
    accessible name. So this is a **warning**, and it says what to reach for
    first rather than forbidding the escape hatch: a selector in `css=` beside
    a role and name is a fallback, and a selector *as* the address is a guess.

    Deliberately not flagged: a selector in a comment or a docstring (only
    string constants are read), and any file that never touches ``browser.*``
    — a CSS string in a workflow that renders an email is not this mistake.
    """

    name: str = "selectors"
    cost: int = 18
    blocking: bool = False

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        if "browser." not in code:
            return CheckResult(
                self.name, skipped=True,
                reason="no browser.* call in this workflow")

        compiled = [(re.compile(pattern), what)
                    for pattern, what in _SELECTOR_PATTERNS]
        spec = context.spec or ""
        issues: list[CodeIssue] = []
        seen: set[str] = set()

        for literal in _string_literals(code):
            if len(literal) < 4 or literal in seen or literal in spec:
                continue
            for regex, what in compiled:
                if regex.search(literal):
                    seen.add(literal)
                    issues.append(
                        CodeIssue(
                            "selectors",
                            f"{literal!r} looks like {what}. A selector read off "
                            "a page while authoring is right on that render and "
                            "silently wrong later — it matches nothing, so the "
                            "workflow completes and reports an empty result. "
                            "Address the control the way a person reads it: "
                            "Target(role=..., name=...), which resolves through "
                            "the accessible name, then placeholder, then label. "
                            "Keep a selector only as css= beside those.",
                            "warning",
                        )
                    )
                    break

        return CheckResult(self.name, issues=issues)


class BrowserEffectStage:
    """What a browser action says it does to the world.

    ``browser.act`` takes a declared ``effect``, and the declaration is what
    makes ``TaintBroker`` able to refuse a submit on a run that has read a
    page. It cannot be inferred: ``click "Next"`` and ``click "Confirm
    booking"`` are the same shape, and a keyword list over the target name is
    exactly the guess ``DEFAULT_SYSTEM_PROMPT`` names as the tell for a rule
    nobody should write.

    So this does not guess either. It reports two things a reader can check:

    **An act with no ``effect`` at all.** The default is WRITE, which is a
    fail-safe backstop rather than a classification — safe, but it means every
    unmarked action is refused on a tainted run, and a workflow full of them is
    one nobody can deploy under the rule. Reported as a *warning*: the code is
    correct, it is merely unclassified.

    **A declared write with no approval anywhere in the file.** Under the
    default policy that run cannot reach the write at all, so this is code that
    will not do what it says. An **error**, because the repair loop reads
    ``report.errors`` and a warning here is a finding nobody sees — and it is
    safe to escalate for the reason the loop already relies on: unchanged code
    ends the repair, so a model that judges the finding wrong says so by
    leaving the file alone.

    It reads the call's own keywords, so a payload built elsewhere and passed
    by name is invisible to it. That is the same limit ``ctx.arguments`` has,
    and the same answer: silence is weak evidence.
    """

    name: str = "browser-effect"
    cost: int = 19
    blocking: bool = False

    async def run(self, code: str, context: CheckContext) -> CheckResult:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return CheckResult(self.name, skipped=True, reason="does not parse")

        acts = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and _is_node_call(node, "browser.act")]
        if not acts:
            return CheckResult(
                self.name, skipped=True, reason="no browser.act call")

        issues: list[CodeIssue] = []
        undeclared = 0
        writes = 0
        for call in acts:
            declared = _payload_value(call, "effect")
            if declared is None:
                undeclared += 1
            elif declared in {"write", "destructive"}:
                writes += 1

        if undeclared:
            issues.append(
                CodeIssue(
                    "browser-effect",
                    f"{undeclared} browser.act call(s) declare no `effect`. It "
                    "defaults to WRITE, which is a fail-safe backstop and not a "
                    "classification — so every one of them is refused on a run "
                    "that has read a page. Say what each action does: filling a "
                    "field and moving between steps are 'read'; submitting, "
                    "sending or confirming is 'write'; cancelling or deleting "
                    "is 'destructive'.",
                    "warning",
                )
            )

        if writes and not _asks_a_person(tree):
            issues.append(
                CodeIssue(
                    "browser-effect",
                    f"this workflow performs {writes} declared browser write(s) "
                    "after reading a page, and never asks a person. Under the "
                    "default taint policy the write is refused and the workflow "
                    "cannot complete — reading a page is what taints the run, "
                    "and only an approval clears it. Add "
                    "ctx.node('human.approval', ...) before the write, and open "
                    "the flow with scope='durable' so the browser survives the "
                    "wait. If the write genuinely needs no review, leave this "
                    "alone and say so.",
                    "error",
                )
            )

        return CheckResult(self.name, issues=issues)


def _is_node_call(node: ast.Call, node_id: str) -> bool:
    """``ctx.node("<node_id>", ...)``, however ctx is named."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "node"):
        return False
    first = node.args[0] if node.args else None
    return isinstance(first, ast.Constant) and first.value == node_id


def _payload_value(call: ast.Call, key: str) -> str | None:
    """A string value for *key* in the call's payload dict, if it is a literal.

    ``None`` covers three different situations on purpose — no payload dict, no
    such key, and a value that is not a literal — because a stage that cannot
    read the value should report the same thing in all three: it does not know.
    """
    payload = call.args[1] if len(call.args) > 1 else None

    # Two shapes, because generated code uses both and only one was read. A
    # node's payload is written either as a dict literal or as its own input
    # model — `ActIn(..., effect="read")` — and reading only the dict reported
    # every model-built call as undeclared. That is the same defect
    # `_effect_arguments` already carried one layer down, where it made
    # `effect_by` dead for every node; here it made this stage fire on correct
    # code, which is worse, since the repair loop acts on what it reports.
    if isinstance(payload, ast.Call):
        for kw in payload.keywords:
            if (kw.arg == key
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)):
                return kw.value.value
        return None

    if not isinstance(payload, ast.Dict):
        return None
    for name, value in zip(payload.keys, payload.values, strict=False):
        if (isinstance(name, ast.Constant) and name.value == key
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)):
            return value.value
    return None


def _asks_a_person(tree: ast.AST) -> bool:
    """Whether the file asks anybody anything, by any of the routes there are."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "wait_for_approval":
                return True
            first = node.args[0] if node.args else None
            if (isinstance(func, ast.Attribute) and func.attr == "node"
                    and isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and first.value.startswith("human.")):
                return True
    return False
