"""The verification stages themselves.

Each wraps a capability that already exists — compiling, the AST validator, the
smoke runner, the supervisor — behind the one :class:`Check` interface, so the
generator composes them instead of knowing about each in turn.

Nothing here is specific to an integration or a model. A stage is added by
writing a class and putting it in the list.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workflow_builder.agents.checks import CheckContext, CheckResult
from workflow_builder.agents.validator import CodeIssue, CodeValidator

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
        from workflow_builder.agents.smoke import compile_check

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
                        # The file is generated, not authored: annotations are a
                        # nice-to-have, and demanding them would bury the real
                        # findings under noise the model cannot act on usefully.
                        "--disable-error-code", "no-untyped-def",
                        "--disable-error-code", "annotation-unchecked",
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
        from workflow_builder.agents.smoke import smoke_run

        outcome = await asyncio.to_thread(
            smoke_run,
            code,
            context.workflow_input,
            timeout=context.timeout,
            fakes=context.fakes,
        )
        if outcome.ok:
            return CheckResult(self.name, detail=outcome)

        if outcome.environmental:
            # The sandbox could not authenticate. Unverified is not broken, and
            # asking for a repair invites the model to delete the integration.
            return CheckResult(
                self.name,
                issues=[
                    CodeIssue(
                        "runtime",
                        "could not verify by running: the sandbox has no "
                        f"credentials for this integration ({outcome.error[:120]}). "
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
        from workflow_builder.agents.smoke import smoke_run

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


def default_stages(*, supervisor: Any = None, smoke: bool = True) -> list[Any]:
    """The standard pipeline, cheapest first.

    Callers compose their own list when they want something else; this is the
    default, not the only arrangement.
    """
    stages: list[Any] = [CompileStage(), StaticStage(), LintStage(), TypeStage()]
    if smoke:
        stages.extend([SmokeStage(), ReplayStage()])
    if supervisor is not None:
        stages.append(CritiqueStage(supervisor=supervisor))
    return stages
