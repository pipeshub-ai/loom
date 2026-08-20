"""How a generated workflow is scored.

Everything else in this package measures; this decides what "better" means, and
it is kept separate for the reason the split usually exists: the criteria are
the part most likely to be argued about, and they should be replaceable without
touching the runner.

The default judge is **deterministic**. It reads the signals the sixteen-stage
verification pipeline already produces — did it compile, did it run, did it
answer, how many repair rounds — rather than asking a model whether the code is
good. A model-based judge on the default path would make the number that gates
CI depend on a sampled distribution, which is the opposite of a regression
gate. :class:`ModelJudge` exists for the cases a rule genuinely cannot cover,
and is opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["Judge", "ModelJudge", "Score", "StructuralJudge"]


@dataclass(frozen=True)
class Score:
    """What one generation was worth, and why.

    ``value`` is what gates CI; ``reasons`` is what makes a regression
    diagnosable rather than merely visible.
    """

    value: float
    """0.0 to 1.0."""
    passed: bool
    """Whether this generation meets the bar at all."""
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        state = "pass" if self.passed else "FAIL"
        return f"{state} {self.value:.2f} ({'; '.join(self.reasons) or 'no notes'})"


@runtime_checkable
class Judge(Protocol):
    """Scores one generated workflow against the case that asked for it."""

    name: str

    def score(self, case: Any, result: Any) -> Score:
        """Judge *result* — a ``CodingResult`` — against *case*."""
        ...


#: What each signal contributes to the score. Stated as data so the weighting
#: is reviewable in a diff rather than buried in an expression.
DEFAULT_WEIGHTS: dict[str, float] = {
    "produced_code": 0.20,
    "no_blocking_errors": 0.25,
    "ran_clean": 0.25,
    "answered": 0.10,
    "expected_toolsets": 0.10,
    "first_pass": 0.10,
}


@dataclass
class StructuralJudge:
    """Scores from the pipeline's own verdicts. Deterministic and free.

    Every component is something the verification pipeline already established,
    so running this adds no model calls and gives the same answer twice — which
    is what lets it gate CI.
    """

    name: str = "structural"
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    pass_mark: float = 0.7

    def score(self, case: Any, result: Any) -> Score:
        signals = self._signals(case, result)
        value = sum(self.weights.get(k, 0.0) for k, ok in signals.items() if ok)
        total = sum(self.weights.get(k, 0.0) for k in signals)
        normalised = value / total if total else 0.0
        reasons = [f"missing: {k}" for k, ok in signals.items() if not ok]
        return Score(
            value=round(normalised, 4),
            passed=normalised >= self.pass_mark and signals["no_blocking_errors"],
            reasons=reasons,
            detail={"signals": signals},
        )

    @staticmethod
    def _signals(case: Any, result: Any) -> dict[str, bool]:
        """Each thing that has to be true, named."""
        code = getattr(result, "code", "") or ""
        issues = list(getattr(result, "issues", []) or [])
        errors = [i for i in issues if getattr(i, "severity", "") == "error"]
        smoke = getattr(result, "smoke", None)
        expected = set(getattr(case, "expected_toolsets", []) or [])

        return {
            "produced_code": bool(code.strip()),
            "no_blocking_errors": not errors,
            # `smoke is None` means smoke testing was switched off, not that it
            # failed — scoring it as a miss would punish a caller for the
            # harness's own configuration.
            "ran_clean": smoke is None or bool(getattr(smoke, "ok", False)),
            "answered": smoke is None or not getattr(smoke, "empty_paths", None),
            "expected_toolsets": not expected or all(t in code for t in expected),
            "first_pass": getattr(result, "repair_attempts", 0) == 0,
        }


@dataclass
class ModelJudge:
    """A second model reads the spec and the code and says whether it answers it.

    Opt-in, and never the CI gate on its own: the value it returns is sampled,
    so a threshold over it moves on its own. Useful for the axis a rule cannot
    reach — whether the workflow does what was *asked*, as opposed to whether it
    is well formed.
    """

    supervisor: Any
    name: str = "model"
    pass_mark: float = 0.7

    def score(self, case: Any, result: Any) -> Score:
        verdict = getattr(result, "review", None)
        if verdict is None:
            return Score(
                value=0.0,
                passed=False,
                reasons=["no supervisor verdict on this result"],
            )
        blocking = [
            f for f in getattr(verdict, "findings", []) if f.severity == "error"
        ]
        value = 0.0 if blocking else 1.0
        return Score(
            value=value,
            passed=not blocking,
            reasons=[f"{f.category}: {f.message}" for f in blocking],
        )
