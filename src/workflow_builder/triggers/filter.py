"""Declarative event filtering with MongoDB-style operators.

A ``FilterSpec`` matches event payloads against a set of conditions.
Supports exact match, nested dotted paths, and operators:
``$in``, ``$nin``, ``$gt``, ``$gte``, ``$lt``, ``$lte``, ``$ne``,
``$regex``, ``$exists``.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


class FilterSpec(BaseModel):
    """Declarative filter over event payload fields.

    Conditions are key-value pairs where the key is a dotted path into
    the payload and the value is either a literal (exact match) or a dict
    of operators::

        FilterSpec(conditions={
            "priority": "P1",
            "fields.status.name": {"$in": ["Open", "Reopened"]},
            "fields.labels": {"$exists": True},
        })
    """

    conditions: dict[str, Any] = Field(default_factory=dict)

    def matches(self, payload: dict[str, Any]) -> bool:
        """Return ``True`` if *payload* satisfies all conditions."""
        for path, expected in self.conditions.items():
            actual = _get_nested(payload, path)
            if isinstance(expected, dict):
                if not _eval_operators(actual, expected):
                    return False
            elif actual != expected:
                return False
        return True


def _get_nested(obj: Any, path: str) -> Any:
    """Get a value by dotted path: ``'fields.priority.name'``."""
    for key in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and key.isdigit():
            idx = int(key)
            obj = obj[idx] if idx < len(obj) else None
        else:
            return None
    return obj


def _eval_operators(actual: Any, ops: dict[str, Any]) -> bool:
    """Evaluate MongoDB-style operators against *actual*."""
    return all(_eval_single(actual, op, val) for op, val in ops.items())


def _eval_single(actual: Any, op: str, val: Any) -> bool:
    """Evaluate one operator."""
    match op:
        case "$in":
            return actual in val
        case "$nin":
            return actual not in val
        case "$gt":
            return actual is not None and actual > val
        case "$gte":
            return actual is not None and actual >= val
        case "$lt":
            return actual is not None and actual < val
        case "$lte":
            return actual is not None and actual <= val
        case "$ne":
            return actual != val
        case "$regex":
            return bool(re.search(val, str(actual))) if actual is not None else False
        case "$exists":
            return (actual is not None) == val
        case _:
            msg = f"Unknown filter operator: {op}"
            raise ValueError(msg)
