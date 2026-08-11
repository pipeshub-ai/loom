"""Toolset version pinning and drift detection.

``generate_lock()`` produces a ``ToolsetLock`` from the current set of
registered manifests.  ``verify_lock()`` compares a lock against live
manifests and reports any drift (version changes, schema changes,
missing/added toolsets).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from workflow_builder.toolsets.manifest import ToolsetManifest


class ToolsetPin(BaseModel):
    """A pinned toolset version and manifest hash."""

    version: str
    manifest_hash: str
    pinned_at: datetime


class ToolsetLock(BaseModel):
    """Pinned toolset versions for the project."""

    toolsets: dict[str, ToolsetPin] = Field(default_factory=dict)
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )

    def to_json(self) -> str:
        """Serialize the lock to JSON."""
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, data: str) -> ToolsetLock:
        """Deserialize a lock from JSON."""
        return cls.model_validate_json(data)


class Drift(BaseModel):
    """A drift entry — difference between lock and live manifest."""

    toolset_id: str
    kind: str  # "version_changed", "schema_changed", "added", "removed"
    detail: str = ""


def _manifest_hash(manifest: ToolsetManifest) -> str:
    """Compute a stable hash of a manifest's schema-relevant fields."""
    payload: dict[str, Any] = {
        "id": manifest.id,
        "version": manifest.version,
        "groups": {
            group: [
                {
                    "id": op.id,
                    "input_schema": op.input_schema,
                    "output_schema": op.output_schema,
                    "effect": op.effect.value,
                    "scopes": op.scopes,
                    "pagination": op.pagination,
                    "idempotent": op.idempotent,
                }
                for op in ops
            ]
            for group, ops in sorted(manifest.groups.items())
        },
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def generate_lock(manifests: list[ToolsetManifest]) -> ToolsetLock:
    """Create a lock from the current set of manifests."""
    now = datetime.now(UTC)
    pins: dict[str, ToolsetPin] = {}
    for m in manifests:
        pins[m.id] = ToolsetPin(
            version=m.version,
            manifest_hash=_manifest_hash(m),
            pinned_at=now,
        )
    return ToolsetLock(toolsets=pins, generated_at=now)


def verify_lock(
    lock: ToolsetLock, manifests: list[ToolsetManifest]
) -> list[Drift]:
    """Compare a lock against live manifests and return drifts."""
    drifts: list[Drift] = []
    live = {m.id: m for m in manifests}

    for tid, pin in lock.toolsets.items():
        if tid not in live:
            drifts.append(Drift(
                toolset_id=tid,
                kind="removed",
                detail=f"Toolset '{tid}' was in lock but is no longer registered",
            ))
            continue
        m = live[tid]
        if m.version != pin.version:
            drifts.append(Drift(
                toolset_id=tid,
                kind="version_changed",
                detail=f"{pin.version} → {m.version}",
            ))
        current_hash = _manifest_hash(m)
        if current_hash != pin.manifest_hash:
            drifts.append(Drift(
                toolset_id=tid,
                kind="schema_changed",
                detail=(
                    f"Manifest hash changed: {pin.manifest_hash} → "
                    f"{current_hash}"
                ),
            ))

    for tid in live:
        if tid not in lock.toolsets:
            drifts.append(Drift(
                toolset_id=tid,
                kind="added",
                detail=f"Toolset '{tid}' is registered but not in lock",
            ))

    return drifts
