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

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from workflow_builder.agents.validator import CodeIssue

__all__ = [
    "Check",
    "CheckContext",
    "CheckPipeline",
    "CheckResult",
    "PipelineReport",
]


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
    fakes: list[tuple[str, str]] = field(default_factory=list)
    """``(tools_module, fakes_module)`` pairs to substitute during execution."""
    timeout: float = 30.0
    spec: str = ""
    """The original request, for stages that judge intent rather than mechanics."""


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

    async def run(self, code: str, context: CheckContext) -> PipelineReport:
        report = PipelineReport()
        for check in self._checks:
            result = await check.run(code, context)
            report.results.append(result)
            if check.blocking and result.errors:
                # Later stages would only report consequences of this one.
                break
        return report
