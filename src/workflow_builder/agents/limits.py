"""Budgets for agent runs.

An agent without limits is an unbounded spend authorised by a probability distribution.
Turn, request, tool-call, token, and dollar ceilings are all enforced *before* the work
that would exceed them, so a runaway loop costs one wasted check rather than one wasted
afternoon of inference.
"""

from __future__ import annotations

from dataclasses import dataclass

from workflow_builder.core.exceptions import UsageLimitExceeded
from workflow_builder.core.models import Usage


@dataclass(frozen=True, slots=True)
class UsageLimits:
    """Ceilings applied to a single agent run."""

    max_turns: int = 20
    """Model round trips. The primary infinite-loop guard."""
    max_requests: int | None = None
    max_tool_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    """Enforced from the running cost estimate; advisory, not a billing guarantee."""
    max_history_messages: int = 40
    """How many prior turns a session replays into a new run. Older turns are
    dropped from the window, never from the stored session."""

    def check_turn(self, turn: int) -> None:
        if turn > self.max_turns:
            raise UsageLimitExceeded(
                f"agent exceeded its budget of {self.max_turns} turns without producing a "
                f"final answer; raise max_turns or narrow the task",
                limit_name="max_turns",
                limit=self.max_turns,
                actual=turn,
            )

    def check_tool_calls(self, count: int) -> None:
        if self.max_tool_calls is not None and count > self.max_tool_calls:
            raise UsageLimitExceeded(
                f"agent exceeded {self.max_tool_calls} tool calls",
                limit_name="max_tool_calls",
                limit=self.max_tool_calls,
                actual=count,
            )

    def check_usage(self, usage: Usage) -> None:
        """Validate cumulative consumption. Call before each model request."""
        checks: list[tuple[str, int | float | None, int | float]] = [
            ("max_requests", self.max_requests, usage.requests),
            ("max_input_tokens", self.max_input_tokens, usage.input_tokens),
            ("max_output_tokens", self.max_output_tokens, usage.output_tokens),
            ("max_total_tokens", self.max_total_tokens, usage.total_tokens),
            ("max_cost_usd", self.max_cost_usd, usage.cost_usd),
        ]
        for name, limit, actual in checks:
            if limit is not None and actual >= limit:
                raise UsageLimitExceeded(
                    f"agent exceeded {name}: {actual} >= {limit}",
                    limit_name=name,
                    limit=limit,
                    actual=actual,
                )


DEFAULT_LIMITS = UsageLimits()
