"""Drift detection — monitor upstream API changes.

Compares a toolset manifest's declared operations against an upstream
API spec to identify added, removed, or changed operations.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ChangedOp(BaseModel):
    """An operation whose definition differs between current and upstream."""

    op_id: str
    field: str
    """What changed (e.g. ``"summary"``)."""
    old_value: str = ""
    new_value: str = ""


class DriftReport(BaseModel):
    """Result of comparing current toolset operations against upstream."""

    toolset_id: str
    added_ops: list[str] = Field(default_factory=list)
    removed_ops: list[str] = Field(default_factory=list)
    changed_ops: list[ChangedOp] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def severity(self) -> str:
        """Classify the drift severity.

        Returns ``"breaking"`` if ops were removed or changed,
        ``"additive"`` if only new ops appeared, or ``"clean"``
        if nothing changed.
        """
        if self.removed_ops or self.changed_ops:
            return "breaking"
        if self.added_ops:
            return "additive"
        return "clean"

    @property
    def has_drift(self) -> bool:
        """``True`` if any operations were added, removed, or changed."""
        return bool(self.added_ops or self.removed_ops or self.changed_ops)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare_operations(
    current_ops: dict[str, str],
    upstream_ops: dict[str, str],
    *,
    toolset_id: str = "",
) -> DriftReport:
    """Compare *current_ops* against *upstream_ops* and return a :class:`DriftReport`.

    Both arguments are mappings of ``{op_id: summary}``.
    """
    current_ids = set(current_ops)
    upstream_ids = set(upstream_ops)

    added = sorted(upstream_ids - current_ids)
    removed = sorted(current_ids - upstream_ids)

    changed: list[ChangedOp] = []
    for op_id in sorted(current_ids & upstream_ids):
        if current_ops[op_id] != upstream_ops[op_id]:
            changed.append(
                ChangedOp(
                    op_id=op_id,
                    field="summary",
                    old_value=current_ops[op_id],
                    new_value=upstream_ops[op_id],
                )
            )

    return DriftReport(
        toolset_id=toolset_id,
        added_ops=added,
        removed_ops=removed,
        changed_ops=changed,
    )
