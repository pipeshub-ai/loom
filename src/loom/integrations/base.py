"""Base protocol for agent framework adapters."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentExecutor(Protocol):
    """Protocol that all framework adapters implement.

    Each adapter wraps a framework-specific agent/graph/crew and
    exposes a unified ``execute`` interface so the LOOM runtime can
    delegate work to any supported agent framework.
    """

    async def execute(
        self,
        *,
        input: str,
        tools: list[Any] | None = None,
        output_type: type | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        """Run the underlying agent and return its result."""
        ...
