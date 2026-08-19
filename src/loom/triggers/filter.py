"""Declarative event filtering with MongoDB-style operators.

A ``FilterSpec`` matches event payloads against a set of conditions.
Supports exact match, nested dotted paths, and operators:
``$eq``, ``$in``, ``$nin``, ``$contains``, ``$gt``, ``$gte``, ``$lt``,
``$lte``, ``$ne``, ``$regex``, ``$exists``.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from pydantic import BaseModel, Field

#: Every operator ``_eval_single`` knows. Named here rather than derived from
#: the ``match`` below so :meth:`FilterSpec.check` can report what is available
#: without evaluating anything.
OPERATORS = frozenset(
    {
        "$eq",
        "$ne",
        "$in",
        "$nin",
        "$contains",
        "$gt",
        "$gte",
        "$lt",
        "$lte",
        "$regex",
        "$exists",
    }
)


class FilterError(ValueError):
    """A filter that cannot be evaluated as written.

    Distinct from "did not match" on purpose. A filter naming an operator that
    does not exist, or comparing a string to a number, has not decided anything
    about the event — and the two outcomes call for opposite handling: a
    non-match is stepped over silently and in bulk, an unevaluable filter is a
    declaration bug somebody has to fix.
    """


class FilterSpec(BaseModel):
    """Declarative filter over event payload fields.

    Conditions are key-value pairs where the key is a dotted path into
    the payload and the value is either a literal (exact match) or a dict
    of operators::

        FilterSpec(conditions={
            "priority": "P1",
            "fields.status.name": {"$in": ["Open", "Reopened"]},
            "fields.labels": {"$contains": "urgent"},
            "fields.assignee": {"$exists": True},
        })

    ``$in`` asks whether the payload's value is one of several; ``$contains``
    asks whether the payload's value is a list holding a given item. They read
    alike and are not interchangeable — ``{"labels": {"$in": ["bug"]}}`` against
    a payload whose ``labels`` is ``["bug"]`` evaluates ``["bug"] in ["bug"]``,
    which is ``False``. That is the shape of bug this class is most likely to
    produce, so both operators exist and the difference is stated here.
    """

    conditions: dict[str, Any] = Field(default_factory=dict)

    def check(self) -> None:
        """Raise :class:`FilterError` if any operator is unknown.

        Deliberately separate from construction. A filter arriving from the
        subscription registry is data somebody already stored, and refusing to
        *load* it would take down the whole registry rather than the one
        subscription at fault — so loading stays permissive and the callers that
        can act on a problem ask for it: :meth:`Subscription.validate_declarable`
        at declaration, and the dispatcher when evaluation fails.
        """
        for path, expected in self.conditions.items():
            if not isinstance(expected, dict):
                continue
            for op in expected:
                if op in OPERATORS:
                    continue
                near = difflib.get_close_matches(op, sorted(OPERATORS), n=1, cutoff=0.6)
                hint = f" Did you mean {near[0]!r}?" if near else ""
                raise FilterError(
                    f"unknown filter operator {op!r} on {path!r}.{hint} "
                    f"Known operators: {', '.join(sorted(OPERATORS))}. "
                    "A condition whose operator does not exist cannot match "
                    "anything, so the subscription would never run."
                )

    def matches(self, payload: dict[str, Any]) -> bool:
        """Return ``True`` if *payload* satisfies all conditions.

        Raises :class:`FilterError` when a condition cannot be evaluated at all
        — an unknown operator, or an ordering comparison between types that do
        not order. Returning ``False`` there would be indistinguishable from an
        honest non-match, which is how a filter comes to silently accept nothing
        for months.
        """
        for path, expected in self.conditions.items():
            actual = _get_nested(payload, path)
            if isinstance(expected, dict):
                if not _eval_operators(actual, expected, path):
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


def _eval_operators(actual: Any, ops: dict[str, Any], path: str) -> bool:
    """Evaluate MongoDB-style operators against *actual*."""
    return all(_eval_single(actual, op, val, path) for op, val in ops.items())


def _eval_single(actual: Any, op: str, val: Any, path: str = "") -> bool:
    """Evaluate one operator."""
    match op:
        case "$eq":
            return bool(actual == val)
        case "$in":
            return _membership(actual, val, op, path)
        case "$nin":
            return not _membership(actual, val, op, path)
        case "$contains":
            # The payload side is the container here, which is the mirror of
            # `$in` and the reason both exist.
            if actual is None:
                return False
            if isinstance(actual, str | bytes):
                return val in actual
            try:
                return val in actual
            except TypeError:
                return False
        case "$gt" | "$gte" | "$lt" | "$lte":
            return _ordered(actual, op, val, path)
        case "$ne":
            return bool(actual != val)
        case "$regex":
            return bool(re.search(val, str(actual))) if actual is not None else False
        case "$exists":
            return bool((actual is not None) == val)
        case _:
            near = difflib.get_close_matches(op, sorted(OPERATORS), n=1, cutoff=0.6)
            hint = f" Did you mean {near[0]!r}?" if near else ""
            raise FilterError(
                f"unknown filter operator {op!r}"
                + (f" on {path!r}" if path else "")
                + f".{hint} Known operators: {', '.join(sorted(OPERATORS))}."
            )


def _membership(actual: Any, val: Any, op: str, path: str) -> bool:
    if not isinstance(val, list | tuple | set | frozenset):
        raise FilterError(
            f"{op} on {path!r} needs a list of values, got {type(val).__name__}. "
            f"To test whether the payload's own list holds an item, use $contains."
        )
    return actual in val


def _ordered(actual: Any, op: str, val: Any, path: str) -> bool:
    """An ordering comparison, with a mismatched pair reported rather than raised raw.

    ``"high" > 50`` is a ``TypeError`` from deep inside evaluation. Left alone it
    escapes into the dispatch loop, where it is neither a match nor a failure
    the retry machinery understands.
    """
    if actual is None:
        return False
    try:
        match op:
            case "$gt":
                return bool(actual > val)
            case "$gte":
                return bool(actual >= val)
            case "$lt":
                return bool(actual < val)
            case _:
                return bool(actual <= val)
    except TypeError as exc:
        raise FilterError(
            f"{op} on {path!r} compared {type(actual).__name__} to "
            f"{type(val).__name__}, which do not order ({actual!r} vs {val!r}). "
            "Filter on a field whose values are comparable, or use $in/$eq."
        ) from exc
