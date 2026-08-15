"""Eval framework — datasets, cases, and scoring for the coding agent.

Provides the data models for evaluating the workflow coding agent's
output quality.  The runner and judge protocols enable CI-gated
regression testing.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class EvalCase(BaseModel):
    """A single evaluation case for the coding agent."""

    id: str
    input: str
    """Natural language spec."""
    expected_steps: list[str] = Field(default_factory=list)
    expected_toolsets: list[str] = Field(default_factory=list)
    golden_code: str | None = None
    difficulty: str = "medium"
    """easy / medium / hard."""
    tags: list[str] = Field(default_factory=list)


class EvalDataset(BaseModel):
    """A named collection of :class:`EvalCase` instances."""

    id: str
    name: str
    description: str = ""
    cases: list[EvalCase] = Field(default_factory=list)
    version: str = "1.0.0"

    @property
    def size(self) -> int:
        """Number of cases in this dataset."""
        return len(self.cases)


class EvalScore(BaseModel):
    """Per-case scoring result."""

    case_id: str
    compile_pass: bool = False
    type_check_pass: bool = False
    behavioral_pass: bool = False
    structural_score: float = 0.0
    """0-1 structural similarity."""
    code_quality_score: float = 0.0
    """0-1 code quality rating."""

    @property
    def overall(self) -> float:
        """Weighted average across all dimensions.

        Weights: compile 0.3, type_check 0.2, behavioral 0.3,
        structural 0.1, code_quality 0.1.
        """
        return (
            0.3 * (1.0 if self.compile_pass else 0.0)
            + 0.2 * (1.0 if self.type_check_pass else 0.0)
            + 0.3 * (1.0 if self.behavioral_pass else 0.0)
            + 0.1 * self.structural_score
            + 0.1 * self.code_quality_score
        )


class AggregateScore(BaseModel):
    """Aggregate statistics across all cases in a dataset."""

    compile_rate: float = 0.0
    type_check_rate: float = 0.0
    behavioral_pass_rate: float = 0.0
    mean_structural: float = 0.0
    mean_overall: float = 0.0


class EvalReport(BaseModel):
    """Full evaluation report for a dataset run."""

    dataset_id: str
    scores: list[EvalScore] = Field(default_factory=list)
    aggregate: AggregateScore = Field(default_factory=AggregateScore)

    def meets_gate(self, gate: str) -> bool:
        """Check whether the aggregate passes a gate expression.

        *gate* must be in the form ``"metric>=0.80"`` where the operator
        is one of ``>=``, ``>``, or ``==``.

        Raises:
            ValueError: If *gate* cannot be parsed.
        """
        m = re.fullmatch(r"(\w+)\s*(>=|>|==)\s*([0-9]*\.?[0-9]+)", gate.strip())
        if not m:
            msg = f"Cannot parse gate expression: {gate!r}"
            raise ValueError(msg)

        metric, op, threshold_s = m.group(1), m.group(2), m.group(3)
        threshold = float(threshold_s)

        value = getattr(self.aggregate, metric, None)
        if value is None:
            msg = f"Unknown aggregate metric: {metric!r}"
            raise ValueError(msg)

        if op == ">=":
            return float(value) >= threshold
        if op == ">":
            return float(value) > threshold
        # op == "=="
        return float(value) == threshold


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def aggregate_scores(scores: list[EvalScore]) -> AggregateScore:
    """Compute :class:`AggregateScore` from individual case scores."""
    if not scores:
        return AggregateScore()

    n = len(scores)
    return AggregateScore(
        compile_rate=sum(1.0 for s in scores if s.compile_pass) / n,
        type_check_rate=sum(1.0 for s in scores if s.type_check_pass) / n,
        behavioral_pass_rate=sum(1.0 for s in scores if s.behavioral_pass) / n,
        mean_structural=sum(s.structural_score for s in scores) / n,
        mean_overall=sum(s.overall for s in scores) / n,
    )
