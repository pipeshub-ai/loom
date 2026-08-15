"""Testing utilities for workflow-builder.

Mock model providers so an agent-powered workflow can be tested without a real
LLM call, and a controllable clock so a workflow that waits can be tested
without waiting.

    from workflow_builder.runtime.clock import ManualClock
    from workflow_builder.testing import advance

    runtime = Runtime(store=MemoryStore(), clock=ManualClock())
    run = await runtime.run(reminder_flow)      # parks on a four-minute timer
    await advance(runtime, minutes=5)           # ...which now already happened

And the journal as a test seam — say what already happened, and the workflow
runs against it instead of against the expensive step:

    from workflow_builder.testing import given, run_with

    result = await run_with(onboard, payload, given(research, returns=canned))
"""

from __future__ import annotations

from workflow_builder.runtime.clock import ManualClock, SystemClock
from workflow_builder.testing.clock import advance, advance_to, settled
from workflow_builder.testing.journal import (
    Given,
    assert_replays,
    given,
    run_with,
    seed,
)
from workflow_builder.testing.mock import MockModelProvider, mock_response

__all__ = [
    "Given",
    "ManualClock",
    "MockModelProvider",
    "SystemClock",
    "advance",
    "advance_to",
    "assert_replays",
    "given",
    "mock_response",
    "run_with",
    "seed",
    "settled",
]
