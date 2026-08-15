"""Conformance suite for AgentExecutor adapters.

A suite that only offers helpers checks nothing. This one had two — neither
asserting anything — and under it every adapter accepted an ``output_type`` and
silently ignored it for as long as the adapters have existed. The signature
said typed, the body returned prose, and nothing in the repository disagreed.

So the tests are the suite. Subclass it, set ``executor``, and an adapter that
drops a capability fails rather than degrades:

    class TestMyAdapter(ExecutorConformanceSuite):
        @pytest.fixture(autouse=True)
        def _setup(self):
            self.executor = MyExecutor(my_framework_agent)

What is checked is deliberately the *contract*, not the framework: that a
result comes back, that a declared output type is honoured or refused loudly,
that tools and settings are accepted, and that a failure is an exception rather
than a plausible-looking wrong answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from loom.core.exceptions import ValidationError

__all__ = ["ConformanceAnswer", "ExecutorConformanceSuite"]


class ConformanceAnswer(BaseModel):
    """The shape every adapter is asked to produce."""

    value: int
    label: str = ""


class ExecutorConformanceSuite:
    """Contract every :class:`AgentExecutor` adapter must satisfy.

    Set ``executor`` on a subclass. Set ``supports_tools = False`` where a
    framework genuinely cannot take LOOM tools — that is a declaration, not an
    escape: the test then asserts the adapter *says so* rather than accepting
    tools and discarding them.
    """

    executor: Any = None
    supports_tools: bool = True

    # -- helpers ------------------------------------------------------------

    async def _sample_execute(self, input_text: str = "hello") -> Any:
        return await self.executor.execute(
            input=input_text, tools=[], output_type=None, settings=None
        )

    async def _execute_with_settings(
        self, input_text: str = "hello", settings: dict[str, Any] | None = None
    ) -> Any:
        return await self.executor.execute(
            input=input_text,
            tools=[],
            output_type=None,
            settings=settings or {"temperature": 0.5},
        )

    # -- the contract -------------------------------------------------------

    async def test_it_returns_something(self) -> None:
        """The floor: a result, not ``None``."""
        assert await self._sample_execute() is not None

    async def test_it_accepts_settings(self) -> None:
        assert await self._execute_with_settings() is not None

    async def test_it_accepts_tools(self) -> None:
        """Declaring no tool support is fine; accepting and dropping is not."""
        if not self.supports_tools:
            pytest.skip("adapter declares supports_tools = False")
        assert (
            await self.executor.execute(
                input="hello", tools=[], output_type=None, settings=None
            )
            is not None
        )

    async def test_a_declared_output_type_is_honoured(self) -> None:
        """The one the old suite could not have caught.

        An adapter that ignores ``output_type`` returns the framework's own
        answer — usually a string — and the caller finds out several attribute
        accesses later, somewhere unrelated.
        """
        result = await self.executor.execute(
            input='Reply with {"value": 7}',
            tools=[],
            output_type=ConformanceAnswer,
            settings=None,
        )
        assert isinstance(result, ConformanceAnswer), (
            f"declared ConformanceAnswer, got {type(result).__name__} — the "
            "adapter accepted output_type and did not apply it"
        )
        assert result.value == 7

    async def test_an_unmeetable_output_type_raises(self) -> None:
        """Loudly wrong beats quietly wrong.

        Returning the raw value when it will not fit is how the original defect
        behaved: the caller believes it holds a typed object.
        """
        with pytest.raises((ValidationError, ValueError, TypeError)):
            await self.executor.execute(
                input="reply with prose and no json at all",
                tools=[],
                output_type=ConformanceAnswer,
                settings=None,
            )

    async def test_no_output_type_passes_the_value_through(self) -> None:
        """The common case must stay free of coercion."""
        assert await self._sample_execute() is not None
