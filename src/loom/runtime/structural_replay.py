"""Structural Replay — green/amber/red plan from steps.lock diff.

When a workflow's code changes between deploys, Structural Replay
compares the old and new ``steps.lock`` hashes to classify each
journaled step as safe to replay (green), needs review (amber),
or invalidated (red).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ReplayStatus(StrEnum):
    """Classification of a step for replay safety."""

    REUSE = "reuse"  # green — hashes match, safe to replay from journal
    RECOMPUTE = "recompute"  # amber — pure step body changed, re-run is safe
    ASK = "ask"  # amber — effect closure changed but contract same
    INVALIDATE = "invalidate"  # red — contract changed, cannot replay
    NEW = "new"  # step added in new version
    ORPHAN = "orphan"  # step removed in new version


class StepIdentity(BaseModel):
    """Snapshot of a step's identity taken from ``steps.lock``."""

    step_id: str
    step_class: str  # "pure" | "effect" | "node"
    contract_hash: str
    closure_hash: str


class StepPlan(BaseModel):
    """Replay decision for a single step."""

    step_id: str
    status: ReplayStatus
    reason: str
    external_effects: list[str] = Field(default_factory=list)


class ReplayPlan(BaseModel):
    """Aggregate replay plan for all steps in a workflow."""

    steps: list[StepPlan] = Field(default_factory=list)

    @property
    def green(self) -> list[StepPlan]:
        """Steps safe to replay from the journal."""
        return [s for s in self.steps if s.status == ReplayStatus.REUSE]

    @property
    def amber(self) -> list[StepPlan]:
        """Steps that need recomputation or human review."""
        return [
            s for s in self.steps if s.status in {ReplayStatus.RECOMPUTE, ReplayStatus.ASK}
        ]

    @property
    def red(self) -> list[StepPlan]:
        """Steps whose contracts changed — replay is unsafe."""
        return [s for s in self.steps if s.status == ReplayStatus.INVALIDATE]

    @property
    def safe_to_auto_replay(self) -> bool:
        """True only when there are no red steps and no amber ASK steps."""
        return all(
            s.status not in {ReplayStatus.INVALIDATE, ReplayStatus.ASK} for s in self.steps
        )

    @property
    def summary(self) -> str:
        """Human-readable summary in ``"N green, M amber, K red"`` format."""
        return f"{len(self.green)} green, {len(self.amber)} amber, {len(self.red)} red"


def plan_structural_replay(
    old_identities: dict[str, StepIdentity],
    new_identities: dict[str, StepIdentity],
) -> ReplayPlan:
    """Compare old and new ``steps.lock`` snapshots and produce a :class:`ReplayPlan`.

    Args:
        old_identities: Step identities from the previous deploy, keyed by step_id.
        new_identities: Step identities from the incoming deploy, keyed by step_id.

    Returns:
        A :class:`ReplayPlan` with one :class:`StepPlan` entry per step.
    """
    plans: list[StepPlan] = []

    all_step_ids = list(dict.fromkeys([*old_identities, *new_identities]))

    for step_id in all_step_ids:
        old = old_identities.get(step_id)
        new = new_identities.get(step_id)

        if old is not None and new is None:
            plans.append(
                StepPlan(step_id=step_id, status=ReplayStatus.ORPHAN, reason="Step removed")
            )
            continue

        if old is None and new is not None:
            plans.append(StepPlan(step_id=step_id, status=ReplayStatus.NEW, reason="New step"))
            continue

        # Both old and new exist — narrow types for the type checker.
        assert old is not None
        assert new is not None

        if old.contract_hash == new.contract_hash and old.closure_hash == new.closure_hash:
            plans.append(
                StepPlan(step_id=step_id, status=ReplayStatus.REUSE, reason="Unchanged")
            )
        elif old.contract_hash != new.contract_hash:
            plans.append(
                StepPlan(
                    step_id=step_id,
                    status=ReplayStatus.INVALIDATE,
                    reason="Contract changed — input/output types differ",
                )
            )
        elif new.step_class == "pure":
            plans.append(
                StepPlan(
                    step_id=step_id,
                    status=ReplayStatus.RECOMPUTE,
                    reason="Pure step body changed, safe to recompute",
                )
            )
        else:
            plans.append(
                StepPlan(
                    step_id=step_id,
                    status=ReplayStatus.ASK,
                    reason="Effect closure changed, contract same — review needed",
                )
            )

    return ReplayPlan(steps=plans)
