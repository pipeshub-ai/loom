"""Measuring the coding agent.

``EvalRunner`` drives a dataset of specs through anything satisfying
:class:`~loom.eval.runner.Coder`, scores each generation with a
:class:`~loom.eval.judge.Judge`, and produces a report that can be compared
against a committed baseline. See :mod:`loom.eval.runner` for why the gate is a
comparison rather than an absolute bar.
"""

from __future__ import annotations

from loom.eval.dataset import EvalCase, EvalDataset
from loom.eval.judge import Judge, ModelJudge, Score, StructuralJudge
from loom.eval.runner import (
    CaseOutcome,
    Coder,
    EvalReport,
    EvalRunner,
    Regression,
    compare,
    dataset_from,
    load_reference_dataset,
)

__all__ = [
    "CaseOutcome",
    "Coder",
    "EvalCase",
    "EvalDataset",
    "EvalReport",
    "EvalRunner",
    "Judge",
    "ModelJudge",
    "Regression",
    "Score",
    "StructuralJudge",
    "compare",
    "dataset_from",
    "load_reference_dataset",
]
