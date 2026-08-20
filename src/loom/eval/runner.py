"""Runs a dataset of specs through the coding agent and reports what happened.

The instrument that was missing. ``eval/`` shipped 140 lines of Pydantic models
— ``EvalCase``, ``EvalDataset``, a scoring helper — and no runner, no judge, no
dataset and no CI gate, while ``phases/phase-7`` specified a model-stratified
suite. Every other defect in the codebase is a bug that can be fixed once; this
was the absence of the thing that says whether any of the fixes helped.

The verification pipeline already produces a rich signal per generation —
stages passed, repair rounds spent, tokens burned, whether the run came back
empty. None of it was aggregated across a corpus. This aggregates it.

**The gate is no-regression, not an absolute bar.** A threshold picked today is
a threshold nobody can adopt tomorrow; a comparison against a committed
baseline is adoptable immediately and gets stricter on its own as the numbers
improve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loom.eval.dataset import EvalCase, EvalDataset
from loom.eval.judge import Judge, Score, StructuralJudge

logger = logging.getLogger("workflow.eval")

__all__ = [
    "CaseOutcome",
    "Coder",
    "EvalReport",
    "EvalRunner",
    "compare",
    "load_reference_dataset",
]


@runtime_checkable
class Coder(Protocol):
    """Whatever turns a spec into a ``CodingResult``.

    An abstraction rather than ``WorkflowCodingAgent`` directly, so the harness
    is testable without a model, a key, or a network — which is the difference
    between an eval suite that runs in CI and one that runs when somebody
    remembers to.
    """

    async def generate(self, spec: str) -> Any: ...


@dataclass
class CaseOutcome:
    """One case, run once.

    Everything here is a fact about the generation rather than a judgement
    about it, so a later change to the scoring can be applied to a stored
    report without re-running anything.
    """

    case_id: str
    model: str = ""
    score: float = 0.0
    passed: bool = False
    reasons: list[str] = field(default_factory=list)

    produced_code: bool = False
    blocking_errors: int = 0
    repair_attempts: int = 0
    smoke_ok: bool | None = None
    empty_output_paths: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    wall_seconds: float = 0.0
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "score": self.score,
            "passed": self.passed,
            "reasons": self.reasons,
            "produced_code": self.produced_code,
            "blocking_errors": self.blocking_errors,
            "repair_attempts": self.repair_attempts,
            "smoke_ok": self.smoke_ok,
            "empty_output_paths": self.empty_output_paths,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_calls": self.tool_calls,
            "wall_seconds": round(self.wall_seconds, 3),
            "error": self.error,
        }


@dataclass
class EvalReport:
    """What a whole dataset did, at one model, on one day."""

    dataset_id: str
    model: str
    outcomes: list[CaseOutcome] = field(default_factory=list)

    @property
    def cases(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.cases if self.cases else 0.0

    @property
    def clean_first_pass(self) -> float:
        """Share of cases that passed with no repair round at all.

        The metric the repair work is aimed at, and the one a threaded
        conversation should move: a model that can still see the schemas it
        fetched should need fewer second attempts.
        """
        if not self.cases:
            return 0.0
        return sum(
            1 for o in self.outcomes if o.passed and o.repair_attempts == 0
        ) / self.cases

    @property
    def mean_score(self) -> float:
        return (
            statistics.fmean(o.score for o in self.outcomes) if self.outcomes else 0.0
        )

    @property
    def mean_repair_rounds(self) -> float:
        return (
            statistics.fmean(o.repair_attempts for o in self.outcomes)
            if self.outcomes
            else 0.0
        )

    @property
    def total_tokens(self) -> int:
        return sum(o.total_tokens for o in self.outcomes)

    def summary(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset_id,
            "model": self.model,
            "cases": self.cases,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "clean_first_pass": round(self.clean_first_pass, 4),
            "mean_score": round(self.mean_score, 4),
            "mean_repair_rounds": round(self.mean_repair_rounds, 3),
            "total_tokens": self.total_tokens,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "cases": [o.as_dict() for o in self.outcomes],
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")

    def render(self) -> str:
        """A table a person reads in a CI log."""
        head = self.summary()
        lines = [
            f"{head['dataset']} @ {head['model'] or 'unknown model'}",
            f"  pass {head['passed']}/{head['cases']} "
            f"({head['pass_rate']:.0%})  clean-first-pass "
            f"{head['clean_first_pass']:.0%}  mean score {head['mean_score']:.2f}",
            f"  repair rounds {head['mean_repair_rounds']:.2f} avg  "
            f"tokens {head['total_tokens']:,}",
            "",
        ]
        for outcome in self.outcomes:
            mark = "ok  " if outcome.passed else "FAIL"
            note = outcome.error or "; ".join(outcome.reasons)
            lines.append(
                f"  {mark} {outcome.case_id:<28} {outcome.score:.2f} "
                f"r={outcome.repair_attempts} {note[:60]}"
            )
        return "\n".join(lines)


@dataclass
class EvalRunner:
    """Runs each case, scores it, and reports.

    Cases run with bounded concurrency rather than all at once: a provider rate
    limit turns an unbounded fan-out into a suite that fails for reasons that
    have nothing to do with the code being measured.
    """

    coder: Coder
    judge: Judge = field(default_factory=StructuralJudge)
    max_concurrency: int = 2
    model: str = ""

    async def run(self, dataset: EvalDataset) -> EvalReport:
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def one(case: EvalCase) -> CaseOutcome:
            async with semaphore:
                return await self._run_case(case)

        outcomes = await asyncio.gather(*(one(c) for c in dataset.cases))
        report = EvalReport(
            dataset_id=dataset.id, model=self.model, outcomes=list(outcomes)
        )
        logger.info("eval | %s", json.dumps(report.summary()))
        return report

    async def _run_case(self, case: EvalCase) -> CaseOutcome:
        started = time.monotonic()
        try:
            result = await self.coder.generate(case.input)
        except Exception as exc:
            # A harness that dies on one case reports nothing about the other
            # nine. The failure is data.
            return CaseOutcome(
                case_id=case.id,
                model=self.model,
                wall_seconds=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed = time.monotonic() - started
        score: Score = self.judge.score(case, result)
        issues = list(getattr(result, "issues", []) or [])
        smoke = getattr(result, "smoke", None)

        return CaseOutcome(
            case_id=case.id,
            model=self.model or getattr(result, "model_used", ""),
            score=score.value,
            passed=score.passed,
            reasons=score.reasons,
            produced_code=bool((getattr(result, "code", "") or "").strip()),
            blocking_errors=sum(
                1 for i in issues if getattr(i, "severity", "") == "error"
            ),
            repair_attempts=getattr(result, "repair_attempts", 0),
            smoke_ok=None if smoke is None else bool(getattr(smoke, "ok", False)),
            empty_output_paths=list(getattr(smoke, "empty_paths", []) or []),
            input_tokens=getattr(result, "input_tokens", 0),
            output_tokens=getattr(result, "output_tokens", 0),
            tool_calls=len(getattr(result, "tool_calls", []) or []),
            wall_seconds=elapsed,
        )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Regression:
    """One metric that got worse than the committed baseline allows."""

    metric: str
    baseline: float
    measured: float
    tolerance: float

    def __str__(self) -> str:
        return (
            f"{self.metric}: {self.measured:.4f} vs baseline "
            f"{self.baseline:.4f} (tolerance {self.tolerance:.4f})"
        )


#: How much each metric may move in the wrong direction before it is a
#: regression. Not zero: a suite driven by a sampled model has run-to-run
#: variance, and a gate that fires on noise is a gate people switch off.
DEFAULT_TOLERANCE: dict[str, float] = {
    "pass_rate": 0.05,
    "clean_first_pass": 0.10,
    "mean_score": 0.05,
}


def compare(
    baseline: dict[str, Any],
    report: EvalReport,
    *,
    tolerance: dict[str, float] | None = None,
) -> list[Regression]:
    """Which metrics moved backwards past their tolerance.

    Empty means the gate passes. Comparing against a committed baseline rather
    than an absolute bar is what makes this adoptable on the day it lands: the
    bar is wherever the code already is, and it ratchets as the numbers improve.
    """
    limits = {**DEFAULT_TOLERANCE, **(tolerance or {})}
    measured = report.summary()
    previous = baseline.get("summary", baseline)
    found: list[Regression] = []
    for metric, allowed in limits.items():
        if metric not in previous:
            continue
        before = float(previous[metric])
        after = float(measured[metric])
        if after < before - allowed:
            found.append(
                Regression(
                    metric=metric,
                    baseline=before,
                    measured=after,
                    tolerance=allowed,
                )
            )
    return found


# ---------------------------------------------------------------------------
# The dataset that already existed on disk
# ---------------------------------------------------------------------------

#: Toolsets each reference spec should plausibly reach for. Deliberately sparse:
#: an expectation nobody is sure of is worse than none, because it turns a
#: correct generation into a failing case.
_REFERENCE_TOOLSETS: dict[str, list[str]] = {}


def load_reference_dataset(directory: Path | str | None = None) -> EvalDataset:
    """Build a dataset from ``examples/reference_specs/``.

    Seeded from what is already committed rather than from newly invented
    prompts: those ten specs are the corpus the reference workflows were built
    against, so a regression against them is a regression against the product's
    own examples.
    """
    root = Path(directory) if directory else _default_spec_dir()
    cases: list[EvalCase] = []
    for path in sorted(root.glob("*_spec.txt")):
        cases.append(
            EvalCase(
                id=path.stem.replace("_spec", ""),
                input=path.read_text(encoding="utf-8").strip(),
                expected_toolsets=_REFERENCE_TOOLSETS.get(path.stem, []),
                difficulty="hard",
                tags=["reference"],
            )
        )
    return EvalDataset(
        id="reference-workflows",
        name="Reference workflow specs",
        description=(
            "The ten specs in examples/reference_specs, which the shipped "
            "reference workflows were written from."
        ),
        cases=cases,
    )


def _default_spec_dir() -> Path:
    """``examples/reference_specs`` relative to the installed package.

    Walks up rather than hard-coding a depth, so this works from a source
    checkout and from a site-packages install alike.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "examples" / "reference_specs"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "examples/reference_specs is not next to this installation; pass a "
        "directory to load_reference_dataset()"
    )


def dataset_from(specs: Sequence[tuple[str, str]], *, dataset_id: str) -> EvalDataset:
    """A dataset from ``(id, spec)`` pairs, for a caller with their own corpus."""
    return EvalDataset(
        id=dataset_id,
        name=dataset_id,
        cases=[EvalCase(id=case_id, input=spec) for case_id, spec in specs],
    )
