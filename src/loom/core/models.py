"""Persistent records describing executions, steps, and their results.

These are the rows an operator queries: "which runs failed last night, at which step, with
what input". They are deliberately plain Pydantic models so any store backend can persist
them without a bespoke mapping layer.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from loom.core.exceptions import WorkflowError
from loom.core.ids import new_id


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    """Parked on a durable timer or an awaited event; costs nothing while waiting."""
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        )


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CACHED = "cached"


class TriggerKind(StrEnum):
    MANUAL = "manual"
    WEBHOOK = "webhook"
    SCHEDULE = "schedule"
    POLL = "poll"
    EVENT = "event"
    FORM = "form"
    CHAT = "chat"
    EMAIL = "email"
    SUB_WORKFLOW = "sub_workflow"
    ERROR_HANDLER = "error_handler"
    REPLAY = "replay"


class ErrorInfo(BaseModel):
    """A serializable snapshot of a failure, including the traceback."""

    type: str
    message: str
    traceback: str | None = None
    retryable: bool = True
    step_name: str | None = None

    @classmethod
    def from_exception(cls, exc: BaseException, *, step_name: str | None = None) -> Self:
        return cls(
            type=type(exc).__name__,
            message=str(exc) or type(exc).__name__,
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            step_name=step_name,
        )


class Usage(BaseModel):
    """Token and cost accounting, aggregated up from individual model calls."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: Usage) -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost_usd += other.cost_usd

    def __add__(self, other: Usage) -> Usage:
        merged = self.model_copy(deep=True)
        merged.add(other)
        return merged


class StepRecord(BaseModel):
    """One durable operation within an execution."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    seq: int
    name: str
    kind: str
    status: StepStatus
    fingerprint: str = ""
    input: Any = None
    output: Any = None
    error: ErrorInfo | None = None
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    usage: Usage | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class ExecutionRecord(BaseModel):
    """The queryable header for a single workflow run."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str = Field(default_factory=lambda: new_id("run"))
    workflow: str
    workflow_version: str = "1"
    status: ExecutionStatus = ExecutionStatus.PENDING
    trigger: TriggerKind = TriggerKind.MANUAL

    input: Any = None
    output: Any = None
    error: ErrorInfo | None = None

    parent_run_id: str | None = None
    """Set when this run was started as a child of another workflow."""
    root_run_id: str | None = None
    replay_of: str | None = None

    attempt: int = 1
    """How many times the orchestration body has been (re)entered, including replays."""

    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    wake_at: datetime | None = None
    """When a suspended run should be resumed by the timer scanner."""
    awaiting_event: str | None = None

    idempotency_key: str | None = None
    code_hash: str = ""
    """Fingerprint of the workflow body that started this run.

    Recorded so a run can be traced back to the code that produced it — without
    it, a run replayed after a refactor gives no way to tell which version of
    the body its journal was written against."""
    lease_owner: str | None = None
    """Node currently executing this run. Empty once the run goes terminal."""
    lease_expires_at: datetime | None = None
    """When the lease goes stale. A RUNNING record past this is an orphan whose
    worker died, and is eligible to be picked up by another node."""

    usage: Usage = Field(default_factory=Usage)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Queryable custom data, the equivalent of n8n's saved execution data."""

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class TriggerRecord(BaseModel):
    """Persisted trigger state for the TriggerDispatcher.

    Tracks when a trigger last fired and when it should fire next.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    trigger_id: str = Field(default_factory=lambda: new_id("trg"))
    workflow: str
    kind: TriggerKind = TriggerKind.SCHEDULE
    spec: dict[str, Any] = Field(default_factory=dict)
    next_fire_at: datetime | None = None
    last_fire_at: datetime | None = None
    enabled: bool = True
    run_count: int = 0
    timezone: str = "UTC"


def _clip(value: Any, limit: int = 80) -> str:
    """One line, bounded — a summary should not scroll."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ExecutionResult(BaseModel):
    """What a caller gets back from ``runtime.run(...)``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    workflow: str
    status: ExecutionStatus
    output: Any = None
    error: ErrorInfo | None = None
    steps: list[StepRecord] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __repr__(self) -> str:
        """A line a person can read.

        ``print(result)`` is the first thing anyone does with a run, and the
        generated repr answers it with every step record, every timestamp, and
        the full usage breakdown — hundreds of characters in which the two
        facts that matter, did it work and what came back, are buried. Every
        field is still there to inspect; this is only how it presents itself.
        """
        parts = [f"run={self.run_id}", f"workflow={self.workflow!r}", self.status.value]
        if self.error is not None:
            parts.append(f"error={_clip(self.error.message)!r}")
        elif self.output is not None:
            parts.append(f"output={_clip(self.output)!r}")
        if self.steps:
            parts.append(f"{len(self.steps)} step{'s' if len(self.steps) > 1 else ''}")
        if self.usage.total_tokens:
            parts.append(f"{self.usage.total_tokens} tokens")
        return f"<ExecutionResult {' '.join(parts)}>"

    __str__ = __repr__

    @property
    def ok(self) -> bool:
        return self.status is ExecutionStatus.COMPLETED

    @property
    def suspended(self) -> bool:
        return self.status is ExecutionStatus.SUSPENDED

    def unwrap(self) -> Any:
        """Return the output, raising the recorded failure if the run did not succeed."""

        if self.ok:
            return self.output
        detail = self.error.message if self.error else self.status.value
        msg = f"workflow '{self.workflow}' did not complete ({self.status}): {detail}"
        raise WorkflowError(msg)

    def step(self, name: str) -> StepRecord | None:
        """Look up the most recent record for a named step, for assertions in tests."""
        for record in reversed(self.steps):
            if record.name == name:
                return record
        return None


class Event(BaseModel):
    """An external signal delivered to a suspended (or future) execution."""

    name: str
    payload: Any = None
    run_id: str | None = None
    id: str = Field(default_factory=lambda: new_id("evt"))
    received_at: datetime | None = None
