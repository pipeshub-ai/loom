"""Conformance suite for AgentExecutor adapters."""

from __future__ import annotations

from typing import Any


class ExecutorConformanceSuite:
    """Mixin for testing executor adapters.

    Subclass and set ``executor`` to test your adapter.
    Tests check: execute() returns a result, handles empty tools,
    handles settings dict.
    """

    executor: Any = None

    async def _sample_execute(
        self, input_text: str = "hello"
    ) -> Any:
        """Run the executor with minimal arguments."""
        return await self.executor.execute(
            input=input_text,
            tools=[],
            output_type=None,
            settings=None,
        )

    async def _execute_with_settings(
        self,
        input_text: str = "hello",
        settings: dict[str, Any] | None = None,
    ) -> Any:
        """Run the executor with a settings dict."""
        return await self.executor.execute(
            input=input_text,
            tools=[],
            output_type=None,
            settings=settings or {"temperature": 0.5},
        )
