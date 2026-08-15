"""``control.*`` — flow shaping.

Every node here is deterministic and does no I/O, so it is journaled but cheap
and replays identically. They exist because the same six shapes get hand-written
in every workflow, each time slightly differently, and a catalogued version is
one the coding agent can be pointed at by name.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from loom.core.types import to_seconds
from loom.nodes.base import Node, NodeContext
from loom.nodes.registry import register_node
from loom.nodes.spec import NodeCategory, NodeDuration, NodeExample, NodeSpec

__all__ = [
    "BatchNode",
    "DedupeNode",
    "FilterNode",
    "SwitchNode",
    "ThrottleNode",
]

def control_spec(**declared: Any) -> NodeSpec:
    """A ``control.*`` spec, deterministic unless the node says otherwise."""
    return NodeSpec(
        import_module="loom.nodes.control",
        category=NodeCategory.CONTROL,
        **{"deterministic": True, **declared},
    )


def _field(row: Any, path: str) -> Any:
    """Read ``a.b.c`` from a dict or object, returning ``None`` for a miss."""
    current = row
    for part in path.split("."):
        current = (
            current.get(part) if isinstance(current, dict) else getattr(current, part, None)
        )
        if current is None:
            return None
    return current


# ---------------------------------------------------------------------------


class SwitchIn(BaseModel):
    value: Any = Field(default=None, description="What is being matched.")
    cases: dict[str, Any] = Field(
        default_factory=dict, description="Exact value to the branch it selects."
    )
    default: Any = Field(default=None, description="Used when nothing matches.")


class SwitchOut(BaseModel):
    branch: Any = None
    matched: bool = False
    """False means the default was used — which a caller usually wants to log,
    and cannot see from the branch alone when the default is a real value."""


@register_node
class SwitchNode(Node[SwitchIn, SwitchOut]):
    """Pick a branch by exact match, with a default."""

    spec = control_spec(
        id="control.switch",
        summary="Pick a branch by exact match on a value, with a default.",
        tags=["switch", "branch", "route", "case"],
        examples=[
            NodeExample(
                payload={
                    "value": "urgent",
                    "cases": {"urgent": "page", "normal": "queue"},
                    "default": "queue",
                }
            )
        ],
    )
    Input, Output = SwitchIn, SwitchOut

    async def run(self, ctx: NodeContext, payload: SwitchIn) -> SwitchOut:
        key = str(payload.value)
        if key in payload.cases:
            return SwitchOut(branch=payload.cases[key], matched=True)
        return SwitchOut(branch=payload.default, matched=False)


# ---------------------------------------------------------------------------


class FilterIn(BaseModel):
    items: list[Any] = Field(default_factory=list, description="Rows to filter.")
    field: str = Field(default="", description="Dotted path to compare, e.g. 'status'.")
    equals: Any = Field(default=None, description="Keep rows where the field equals this.")
    one_of: list[Any] = Field(
        default_factory=list, description="Keep rows whose field is any of these."
    )
    exists: bool = Field(default=False, description="Keep rows where the field is not None.")


class FilterOut(BaseModel):
    items: list[Any] = Field(default_factory=list)
    kept: int = 0
    dropped: int = 0
    """Reported rather than left to be derived, so a workflow can say what it
    discarded. A filter that silently removes everything looks identical to one
    that was given nothing."""


@register_node
class FilterNode(Node[FilterIn, FilterOut]):
    """Keep the rows matching a declared condition, and report what was dropped."""

    spec = control_spec(
        id="control.filter",
        summary="Keep rows matching a declared field condition; report the drop count.",
        tags=["filter", "where", "select"],
        examples=[
            NodeExample(
                payload={
                    "items": [{"status": "open"}, {"status": "done"}],
                    "field": "status",
                    "equals": "open",
                }
            )
        ],
    )
    Input, Output = FilterIn, FilterOut

    async def run(self, ctx: NodeContext, payload: FilterIn) -> FilterOut:
        if not payload.field:
            return FilterOut(items=list(payload.items), kept=len(payload.items))

        kept: list[Any] = []
        for row in payload.items:
            found = _field(row, payload.field)
            if payload.exists:
                keep = found is not None
            elif payload.one_of:
                keep = found in payload.one_of
            else:
                keep = found == payload.equals
            if keep:
                kept.append(row)
        return FilterOut(
            items=kept, kept=len(kept), dropped=len(payload.items) - len(kept)
        )


# ---------------------------------------------------------------------------


class DedupeIn(BaseModel):
    items: list[Any] = Field(default_factory=list, description="Rows to deduplicate.")
    key: str = Field(
        default="", description="Dotted path to dedupe on. Empty uses the whole row."
    )
    keep: str = Field(default="first", description="first | last")


class DedupeOut(BaseModel):
    items: list[Any] = Field(default_factory=list)
    removed: int = 0


@register_node
class DedupeNode(Node[DedupeIn, DedupeOut]):
    """Remove duplicate rows, keeping the first or last of each key."""

    spec = control_spec(
        id="control.dedupe",
        summary="Remove duplicate rows by key, keeping the first or last.",
        tags=["dedupe", "unique", "distinct"],
        examples=[
            NodeExample(payload={"items": [{"id": 1}, {"id": 1}], "key": "id"})
        ],
    )
    Input, Output = DedupeIn, DedupeOut

    async def run(self, ctx: NodeContext, payload: DedupeIn) -> DedupeOut:
        seen: dict[str, int] = {}
        out: list[Any] = []
        for row in payload.items:
            raw = _field(row, payload.key) if payload.key else row
            token = json.dumps(raw, sort_keys=True, default=str)
            if token in seen:
                if payload.keep == "last":
                    out[seen[token]] = row
                continue
            seen[token] = len(out)
            out.append(row)
        return DedupeOut(items=out, removed=len(payload.items) - len(out))


# ---------------------------------------------------------------------------


class BatchIn(BaseModel):
    items: list[Any] = Field(default_factory=list, description="Rows to group.")
    size: int = Field(default=100, ge=1, description="Rows per batch.")


class BatchOut(BaseModel):
    batches: list[list[Any]] = Field(default_factory=list)
    count: int = 0


@register_node
class BatchNode(Node[BatchIn, BatchOut]):
    """Split rows into fixed-size batches."""

    spec = control_spec(
        id="control.batch",
        summary="Split a list into fixed-size batches.",
        tags=["batch", "chunk", "group"],
        examples=[NodeExample(payload={"items": [1, 2, 3, 4, 5], "size": 2})],
    )
    Input, Output = BatchIn, BatchOut

    async def run(self, ctx: NodeContext, payload: BatchIn) -> BatchOut:
        rows = list(payload.items)
        batches = [rows[i : i + payload.size] for i in range(0, len(rows), payload.size)]
        return BatchOut(batches=batches, count=len(batches))


# ---------------------------------------------------------------------------


class ThrottleIn(BaseModel):
    value: Any = Field(default=None, description="Passed through unchanged.")
    every: NodeDuration = Field(
        default=1.0, description="Seconds between calls."
    )


class ThrottleOut(BaseModel):
    value: Any = None
    waited: float = 0.0


@register_node
class ThrottleNode(Node[ThrottleIn, ThrottleOut]):
    """Pace a loop by sleeping between iterations."""

    spec = control_spec(
        id="control.throttle",
        summary="Sleep a fixed interval, to pace calls against a rate limit.",
        description=(
            "Uses ctx.sleep, so the wait is journaled and a long one parks the "
            "run rather than holding a worker."
        ),
        tags=["throttle", "rate limit", "pace", "sleep"],
        deterministic=False,
        examples=[NodeExample(payload={"every": 2})],
    )
    Input, Output = ThrottleIn, ThrottleOut

    async def run(self, ctx: NodeContext, payload: ThrottleIn) -> ThrottleOut:
        seconds = to_seconds(payload.every)
        if seconds > 0:
            await ctx.sleep(seconds, name="throttle")
        return ThrottleOut(value=payload.value, waited=seconds)


def content_token(value: Any) -> str:
    """A stable hash of *value*, used by dedupe-style nodes and their tests."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
