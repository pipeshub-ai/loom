"""Verification stages for generated workflow code.

One interface, run cheapest-first, each returning the same kind of issue. The
alternative — and what this replaces — is a handful of differently-shaped checks
wired individually into the generator, where adding a stage means editing the
orchestrator and the repair loop can only consume one kind of failure.

Ordering is by cost, and the pipeline stops at the first stage that produces a
blocking error: there is no point type-checking code that does not compile, or
running code whose imports are wrong.

Stages that need a tool the environment may not have — a linter, a type checker
— report themselves *skipped* rather than failing. A check that cannot run has
found nothing, and saying so is not the same as passing.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from loom.agents.validator import CodeIssue

__all__ = [
    "Check",
    "CheckContext",
    "CheckPipeline",
    "CheckResult",
    "PipelineReport",
]


logger = logging.getLogger("workflow.coding_agent")

@dataclass(frozen=True)
class CheckContext:
    """Everything a stage may need beyond the code itself.

    One object rather than a widening parameter list, so a new stage that needs
    new information does not change every other stage's signature.
    """

    workflow_input: Any = None
    """Input the smoke run passes to the generated workflow."""
    allowed_packages: set[str] | None = None
    available_toolsets: set[str] | None = None
    toolset_modules: dict[str, str] = field(default_factory=dict)
    """Toolset id to its real module, so an invented import path is caught
    statically rather than at import time inside the smoke run."""
    fakes: list[tuple[str, str]] = field(default_factory=list)
    """``(tools_module, fakes_module)`` pairs to substitute during execution."""
    timeout: float = 30.0
    spec: str = ""
    """The original request, for stages that judge intent rather than mechanics."""
    prior: PipelineReport | None = None
    """What the stages before this one found.

    For a stage whose job is to *interpret* an earlier stage rather than to
    inspect the code again — reading the smoke run's output instead of paying
    to produce it a second time, and repeating whatever side effects it had.
    Carried here rather than as a second parameter to ``run`` because that is
    what this object is for: a stage that needs new information should not
    change every other stage's signature.

    ``None`` when a stage is run outside a pipeline, which is how the tests
    exercise one in isolation."""
    resolved_kinds: set[str] = field(default_factory=set)
    """Entity kinds a resolver was actually *executed* for while authoring.

    Derived from the agent's own tool calls, not from anything it reports —
    a self-report of "I resolved that" is exactly as easy to produce when it
    did not. :class:`~loom.agents.stages.IdentifierStage` weighs an opaque id
    in the finished code against this."""


@dataclass
class CheckResult:
    """What one stage found."""

    name: str
    issues: list[CodeIssue] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""
    """Why it was skipped, or a note about what it did."""
    detail: Any = None
    """Stage-specific payload — a SmokeResult, a supervisor verdict."""

    @property
    def errors(self) -> list[CodeIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        """A skipped stage is not a failed one."""
        return not self.errors


@runtime_checkable
class Check(Protocol):
    """One verification stage.

    ``cost`` orders the pipeline: compiling is free, running is not, asking a
    model is the most expensive thing here. ``blocking`` says whether an error
    should stop the stages after it.
    """

    name: str
    cost: int
    blocking: bool

    async def run(self, code: str, context: CheckContext) -> CheckResult: ...


@dataclass
class PipelineReport:
    """The outcome of a whole run."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def issues(self) -> list[CodeIssue]:
        return [issue for result in self.results for issue in result.issues]

    @property
    def errors(self) -> list[CodeIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def detail(self, name: str) -> Any:
        """The payload a named stage produced, if it ran."""
        for result in self.results:
            if result.name == name:
                return result.detail
        return None

    def result(self, name: str) -> CheckResult | None:
        for result in self.results:
            if result.name == name:
                return result
        return None

    @property
    def summary(self) -> str:
        """One line per stage, for a log or a CLI."""
        parts = []
        for r in self.results:
            state = "skip" if r.skipped else ("fail" if r.errors else "ok")
            parts.append(f"{r.name}={state}")
        return " ".join(parts)


class CheckPipeline:
    """Runs checks in cost order, stopping at the first blocking failure."""

    def __init__(self, checks: list[Check]) -> None:
        self._checks = sorted(checks, key=lambda check: check.cost)

    @property
    def names(self) -> list[str]:
        return [check.name for check in self._checks]

    async def run(
        self,
        code: str,
        context: CheckContext,
        *,
        on_stage: Callable[[Check, CheckResult | None], Any] | None = None,
    ) -> PipelineReport:
        """Run every check, cheapest first, stopping at the first blocking error.

        *on_stage* is called twice per check — once with ``None`` as the stage
        opens, once with its result as it closes. Sixteen stages including a
        subprocess smoke run and a full replay is most of the wall clock of an
        authoring job, and without this the whole of it was one silent pause.

        It is an argument rather than a hook registry because a stage is not an
        agent call: nothing here reaches a model, so the agent family has
        nothing to say about it, and inventing an event for a plain loop would
        be a mechanism where a callback is the thing.
        """
        report = PipelineReport()
        for check in self._checks:
            if on_stage is not None:
                await _announce(on_stage, check, None)
            result = await check.run(code, replace(context, prior=report))
            report.results.append(result)
            if on_stage is not None:
                await _announce(on_stage, check, result)
            if check.blocking and result.errors:
                # Later stages would only report consequences of this one.
                break
        return report


async def _announce(
    on_stage: Callable[[Check, CheckResult | None], Any],
    check: Check,
    result: CheckResult | None,
) -> None:
    """Call the progress callback, sync or async, never fatally.

    Fails open on purpose, the rule the non-deciding hook families already
    follow: a callback cannot change what the pipeline finds, so a broken
    renderer must not be able to turn a clean generation into a failed one.
    """
    try:
        outcome = on_stage(check, result)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        logger.debug("progress callback failed for stage %s", check.name, exc_info=True)
