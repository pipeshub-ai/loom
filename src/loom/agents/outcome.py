"""Where a result came back empty.

Split out of the stage that reports it because the fact has to be computed in
two places at once: inside the smoke subprocess, where the workflow's output is
whole, and in this process, where the verdict is assembled. The subprocess
sends back ``output_preview``, which is ``str(output)[:400]`` — enough for a
person reading a repair prompt, and not enough to re-derive this from, because
a large result truncates and an empty collection nested inside one disappears
with the rest.

Deriving it twice from different data is how the two answers drift. Deriving it
once, where the data is complete, is why this module is importable from both.
"""

from __future__ import annotations

from typing import Any

__all__ = ["empty_paths"]


def empty_paths(value: Any, prefix: str = "") -> list[str]:
    """Every path in *value* holding an empty collection, outermost first.

    An empty string is not one. ``""`` is a plausible answer to "what is the
    page title"; ``[]`` is never a plausible answer to "which fields did you
    find", and conflating them makes the check fire on results that are fine.

    A path reads as it would be written — ``stage2.fields``, ``rows[0].tags`` —
    so the report names somewhere the reader can go and look.
    """
    if isinstance(value, list | tuple | set | dict) and not value:
        return [prefix or "the result"]

    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.extend(empty_paths(item, path))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found.extend(empty_paths(item, f"{prefix}[{index}]"))
    return found
