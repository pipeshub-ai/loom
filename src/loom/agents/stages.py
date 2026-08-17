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
    "CompileStage",
    "CritiqueStage",
    "LintStage",
    "ReplayStage",
    "SmokeStage",
    "StaticStage",
    "TypeStage",
    "default_stages",
]


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

        issues = [
            CodeIssue("types", line.split(":", 1)[-1].strip(), "warning")
            for line in completed.stdout.splitlines()
            if ": error:" in line
        ]
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
                timeout=context.timeout, fakes=context.fakes,
            ),
            asyncio.to_thread(
                smoke_run, code, context.workflow_input,
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

    A warning, not an error. Deciding that 100 is enough is a legitimate call;
    making it *without noticing* is the defect, and the fix is usually one line.
    """

    name: str = "coverage"
    cost: int = 15
    blocking: bool = False

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
        if not capped:
            return CheckResult(self.name)
        if _reads_coverage(code, self.COVERAGE_CHECKS):
            return CheckResult(self.name)

        where = ", ".join(sorted(capped))
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


#: Every keyword a shipped toolset uses to cap a read. Recognising only
#: `max_results` and `limit` left `num_results` (Exa) invisible, and a cap this
#: does not know is a cap this stage cannot see.
CAP_KEYWORDS: frozenset[str] = frozenset(
    {"max_results", "limit", "page_size", "per_page", "num_results", "top", "count"}
)


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
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
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
    """

    name: str = "resolution"
    cost: int = 16
    blocking: bool = False

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

        issues: list[CodeIssue] = []
        for literal in _string_literals(code):
            lowered = literal.lower()
            if not any(op in lowered for op in self.FUZZY):
                continue
            for word in _words(lowered):
                match = _closest(word, terms)
                if match is None:
                    continue
                how = (
                    f"the spec's {match!r}"
                    if word == match
                    else f"{match!r} from the spec, respelled"
                )
                issues.append(
                    CodeIssue(
                        "resolution",
                        f"{literal.strip()!r} matches on {how}. A fuzzy text "
                        "search is not a resolution — it returns whatever "
                        "happens to contain the substring, and nothing when "
                        "the wording differs. Look the entity up with "
                        "call_read_operation and filter on the id it returns; "
                        "if it stays ambiguous, resolve it in a ctx.agent() "
                        "step with the candidates.",
                        "warning",
                    )
                )
                break
        return CheckResult(self.name, issues=issues[:3])


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


def default_stages(
    *, supervisor: Any = None, smoke: bool = True, registry: Any = None
) -> list[Any]:
    """The standard pipeline, cheapest first.

    Callers compose their own list when they want something else; this is the
    default, not the only arrangement.
    """
    stages: list[Any] = [
        CompileStage(),
        StaticStage(),
        GrantStage(registry),
        CoverageStage(),
        PlacementStage(),
        ResolutionStage(),
        LintStage(),
        TypeStage(),
    ]
    if smoke:
        stages.extend([SmokeStage(), ReplayStage()])
    if supervisor is not None:
        stages.append(CritiqueStage(supervisor=supervisor))
    return stages
