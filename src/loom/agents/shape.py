"""Describe a value's shape without reproducing it.

When a tool result is too large to put in front of a model, the useful thing
to say is not the first two kilobytes of it. It is *what kind of thing this
is* — a JSON object with these keys, an array of 312 records shaped like this
— because that is what the model needs to decide how to page through it.

Bounded by construction: depth, key count, and total length are all capped, so
a pathological ten-megabyte nested structure cannot make its own summary
expensive to produce.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["describe_format", "describe_shape"]

#: How deep to descend before summarizing a branch as its bare type.
MAX_DEPTH = 3

#: Keys shown at the top level, and at every level below it.
TOP_KEYS = 20
NESTED_KEYS = 8

#: Hard cap on the rendered shape, so the description cannot itself be large.
MAX_CHARS = 500


def describe_format(text: str) -> str:
    """Name the wire format: ``JSON object``, ``JSONL (120 records)``, ``text``.

    JSONL is only considered after whole-document JSON fails, so a pretty-printed
    document is never mistaken for line-delimited records.
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        pass
    else:
        return f"JSON {_kind(parsed)}"

    lines = [line for line in text.split("\n") if line.strip()]
    if len(lines) > 1:
        try:
            for line in lines:
                json.loads(line)
        except ValueError:
            pass
        else:
            return f"JSONL ({len(lines)} records)"

    return "text"


def describe_shape(value: Any, depth: int = 0) -> str:
    """A one-line structural summary of *value*.

    Args:
        value: Any JSON-compatible value.
        depth: Current nesting level; callers pass nothing.
    """
    return _bounded(_shape(value, depth))


def _shape(value: Any, depth: int) -> str:
    if isinstance(value, list):
        if not value:
            return "array(0)"
        return f"array({len(value)}) of {_shape(value[0], depth + 1)}"

    if isinstance(value, dict):
        if depth > MAX_DEPTH:
            return "object"
        limit = TOP_KEYS if depth == 0 else NESTED_KEYS
        items = list(value.items())
        shown = [
            f"{_key(name)}: {_shape(child, depth + 1)}" for name, child in items[:limit]
        ]
        if len(items) > len(shown):
            shown.append(f"… +{len(items) - len(shown)} keys")
        return f"{{ {', '.join(shown)} }}"

    return _kind(value)


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    if isinstance(value, bool):
        return "boolean"
    return type(value).__name__


def _key(name: Any) -> str:
    """Render a key plainly when it is an identifier, quoted when it is not."""
    text = str(name)
    return text if text.isidentifier() else json.dumps(text)


def _bounded(shape: str) -> str:
    return shape if len(shape) <= MAX_CHARS else f"{shape[: MAX_CHARS - 1]}…"
