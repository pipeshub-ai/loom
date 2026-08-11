"""Mock model provider for deterministic agent testing.

A ``MockModelProvider`` returns scripted responses, so agent workflows
can be tested end-to-end without real LLM calls, without network access,
and with deterministic output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from workflow_builder.agents.messages import ToolCall, assistant
from workflow_builder.agents.models import (
    FinishReason,
    ModelRequest,
    ModelResponse,
)
from workflow_builder.core.models import Usage


@dataclass
class MockModelProvider:
    """A model provider that returns scripted responses in order.

    Usage::

        provider = MockModelProvider(responses=[
            mock_response("Hello!"),
            mock_response(tool_calls=[ToolCall(name="search", arguments={"q": "test"})]),
            mock_response("Done."),
        ])
        agent = Agent(name="test", model=provider)
        result = await agent("hi")
    """

    model_name: str = "mock"
    responses: list[ModelResponse] = field(default_factory=list)
    _call_count: int = field(default=0, init=False)
    _requests: list[ModelRequest] = field(default_factory=list, init=False)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return the next scripted response."""
        self._requests.append(request)
        if self._call_count >= len(self.responses):
            return ModelResponse(
                message=assistant("I have no more responses."),
                finish_reason=FinishReason.STOP,
                model=self.model_name,
            )
        response = self.responses[self._call_count]
        self._call_count += 1
        return response

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def requests(self) -> list[ModelRequest]:
        return list(self._requests)

    def last_request(self) -> ModelRequest | None:
        return self._requests[-1] if self._requests else None

    def reset(self) -> None:
        self._call_count = 0
        self._requests.clear()


def mock_response(
    text: str | None = None,
    *,
    tool_calls: list[ToolCall] | None = None,
    finish_reason: FinishReason | None = None,
    usage: Usage | None = None,
    model: str = "mock",
) -> ModelResponse:
    """Create a scripted model response for testing."""
    if finish_reason is None:
        finish_reason = (
            FinishReason.TOOL_CALLS if tool_calls else FinishReason.STOP
        )
    return ModelResponse(
        message=assistant(text, tool_calls=tool_calls),
        finish_reason=finish_reason,
        usage=usage or Usage(requests=1, input_tokens=100, output_tokens=50),
        model=model,
    )
