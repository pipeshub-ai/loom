"""The two calls that make a spilled result reachable.

A locator with no retrieval tool is a more informative truncation. These are
mounted whenever bounding is active — not when a spill first happens, because
the model sees the tool list before the tool call that overflows, and a tool
that appears mid-conversation is not a tool the model can plan around.
"""

from __future__ import annotations

from typing import Any

from workflow_builder.agents.bounds import SpillStore
from workflow_builder.agents.tools import Tool

__all__ = ["spill_tools"]


def spill_tools(store: SpillStore) -> list[Tool]:
    """Bind ``read_spill`` and ``grep_spill`` to one store."""

    async def read_spill(ref: str, offset: int = 0, limit: int = 4000) -> str:
        """Read part of a tool result that was too large to show in full.

        Args:
            ref: The locator from the omission notice, e.g. ``blob:9f3a…``.
            offset: Character position to start from. Defaults to the start.
            limit: How many characters to return. Defaults to 4000.
        """
        try:
            text = await store.read(ref, offset=offset, limit=limit)
        except Exception as exc:
            return f"Could not read {ref}: {exc}"
        if not text:
            return f"No content at offset {offset} in {ref} — past the end."
        return text

    async def grep_spill(ref: str, pattern: str, max_matches: int = 50) -> str:
        """Search a tool result that was too large to show in full.

        Args:
            ref: The locator from the omission notice, e.g. ``blob:9f3a…``.
            pattern: A regular expression. Treated literally if it will not compile.
            max_matches: Most matching lines to return. Defaults to 50.
        """
        try:
            found = await store.grep(ref, pattern, max_matches=max_matches)
        except Exception as exc:
            return f"Could not search {ref}: {exc}"
        if not found:
            return f"No line in {ref} matched {pattern!r}."
        head = f"{len(found)} matching line(s) in {ref}:"
        return "\n".join([head, *found])

    return [_tool(read_spill), _tool(grep_spill)]


def _tool(fn: Any) -> Tool:
    from workflow_builder.agents.tools import coerce_tool

    return coerce_tool(fn)
