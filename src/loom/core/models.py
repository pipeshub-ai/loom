"""Persistent records describing executions, steps, and their results.

These are the rows an operator queries: "which runs failed last night, at which step, with
what input". They are deliberately plain Pydantic models so any store backend can persist
them without a bespoke mapping layer.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from loom.core.compat import UNKNOWN as _UNKNOWN
from loom.core.compat import tolerant_enum
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

    UNKNOWN = _UNKNOWN
    """A status this version does not know, read from a record a newer one wrote.

    Never written by this version. It exists so ``load_journal`` and
    ``get_execution`` can return a record whose status they cannot interpret,
    instead of raising and making the run unreadable — see
    :mod:`loom.core.compat`. Deliberately **not** terminal: a run whose status
    cannot be read must not be compacted or reported as finished on a guess.
    """

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

    UNKNOWN = _UNKNOWN
    """A trigger kind written by a newer version. See :mod:`loom.core.compat`."""


#: Persisted enum fields, annotated to survive a value this version predates.
#: The tolerance lives here rather than on the enum so a typo in application
#: code still raises.
Status = Annotated[
    ExecutionStatus, BeforeValidator(tolerant_enum(ExecutionStatus, ExecutionStatus.UNKNOWN))
]
Trigger = Annotated[
    TriggerKind, BeforeValidator(tolerant_enum(TriggerKind, TriggerKind.UNKNOWN))
]


class ErrorInfo(BaseModel):
    """A serializable snapshot of a failure, including the traceback."""

    type: str
    message: str
    traceback: str | None = None
    retryable: bool = True
    step_name: str | None = None

    @classmethod
    def from_exception(cls, exc: BaseException, *, step_name: str | None = None) -> Self:
        # `retryable` was never derived here, so it kept its `True` default for
        # everything — including the exceptions whose whole purpose is to say
        # "do not try this again". A run failing on a deleted payload or a
        # missing scope was recorded as worth retrying, forever.
        from loom.core.exceptions import NonRetryableError

        return cls(
            type=type(exc).__name__,
            message=str(exc) or type(exc).__name__,
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            retryable=not isinstance(exc, NonRetryableError),
            step_name=step_name,
        )


class Usage(BaseModel):
    """Token and cost accounting, aggregated up from individual model calls.

    One contract, stated once, because two providers report the same numbers
    differently and the cost model was silently mixing the conventions.
    ``input_tokens`` is the **total** prompt cost of the request — fresh tokens
    plus cache reads plus cache writes — which is what OpenAI's
    ``prompt_tokens`` already is and what Anthropic's ``input_tokens``
    deliberately is not. Every provider normalises to this shape at its own
    boundary; nothing downstream should have to know which vendor answered.
    """

    requests: int = 0
    input_tokens: int = 0
    """Total prompt tokens, cache reads and writes included."""
    output_tokens: int = 0
    cached_input_tokens: int = 0
    """Prompt tokens served from a cache, billed at a fraction of the input
    rate. A subset of :attr:`input_tokens`."""
    cache_write_tokens: int = 0
    """Prompt tokens written *into* a cache, billed at a premium.

    Never counted at all before, so a long agent loop — which writes a cache
    entry on almost every turn — was under-billed by the whole of the write
    surcharge. Also a subset of :attr:`input_tokens`."""
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
        self.cache_write_tokens += other.cache_write_tokens
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

    # extra="allow", not the "ignore" default. The stores rewrite the whole
    # record on every write, so ignoring an unknown field does not merely skip
    # it — it *deletes* it. During a rolling deploy an old pod would read a
    # record a new pod wrote, drop the new field, and write it back without it,
    # leaving no way to tell a destroyed value from one never set. Allowing
    # extras makes the round trip lossless. "forbid" would be strictly worse
    # here: it converts silent loss into a hard failure on every old pod.
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    schema_version: int = 1
    """Shape of this record, for a reader that has to ask "how old is this?".

    Not branched on today. It exists because that question was unanswerable —
    the only way to date a record was to notice which fields it happened to be
    missing, which is exactly the guess a version tag removes.
    """

    run_id: str = Field(default_factory=lambda: new_id("run"))
    workflow: str
    workflow_version: str = "1"
    status: Status = ExecutionStatus.PENDING
    trigger: Trigger = TriggerKind.MANUAL

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
    pause_requested: bool = False
    """An operator has asked this run to hold at the next durable boundary.

    There was no way to do this at all. A run parked only when its own code
    said so — a timer or an event — so the only things an operator could do to
    a misbehaving run were cancel it (terminal, unwinds compensations) or watch
    it. Every comparable platform lets you freeze a live automation, and it is
    what you reach for when a downstream API starts corrupting data at 3am.

    A pause is not a new mechanism: the run suspends on ``resume:<run_id>``,
    which is an ordinary event, so it costs nothing while held and resumes
    through the path every other suspension uses.
    """

    cancel_requested: bool = False
    """Somebody has asked for this run to stop, and it has not stopped yet.

    Persisted, because the request has to survive reaching a process other than
    the one driving the run. ``cancel()`` used to record the request in an
    in-memory set and write ``CANCELLED`` straight onto the record: a worker in
    another process never saw it, kept executing steps, and overwrote the
    status on its next update — and because its body never raised
    ``WorkflowCancelled``, the compensation stack never unwound. The promise
    that cancellation rolls back held only for a single process.

    Read by the lease heartbeat, which is already the one periodic store
    contact a running drive makes, so honouring a remote cancel costs no extra
    round trip and takes effect within a third of the lease TTL.
    """

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

    # See ExecutionRecord: this record is rewritten whole on every fire.
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    schema_version: int = 1
    """Shape of this record. See :attr:`ExecutionRecord.schema_version`."""

    trigger_id: str = Field(default_factory=lambda: new_id("trg"))
    workflow: str
    kind: TriggerKind = TriggerKind.SCHEDULE
    spec: dict[str, Any] = Field(default_factory=dict)
    next_fire_at: datetime | None = None
    last_fire_at: datetime | None = None
    enabled: bool = True
    run_count: int = 0
    timezone: str = "UTC"
    claimed_by: str = ""
    """Which dispatcher currently holds this trigger, if any."""
    claimed_until: datetime | None = None
    """When that claim lapses.

    A lease rather than a lock: a dispatcher that dies mid-tick must not park a
    trigger permanently. That is the difference between a crash costing one late
    run and costing every run after it.

    Lives here, inside the record, rather than in a column of its own — every
    store already persists this model whole, so claiming needed no migration on
    a deployed table."""


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


class EventDelivery(BaseModel):
    """What ``Runtime.send_event`` did with an event.

    A return value rather than ``None`` because an at-least-once consumer needs
    to tell "delivered" from "already delivered" in order to ack correctly: a
    duplicate that raised would be retried forever, and one that looked like a
    fresh delivery would hide the redelivery entirely.
    """

    delivered: bool = True
    reason: str = ""
    """Why not, when ``delivered`` is False. ``"duplicate"`` today."""
    run_ids: list[str] = Field(default_factory=list)
    """Runs this delivery resumed. Empty is normal — an event can arrive before
    anything waits for it, and it stays buffered."""
    dedupe_key: str = ""


class Event(BaseModel):
    """An external signal delivered to a suspended (or future) execution."""

    name: str
    payload: Any = None
    run_id: str | None = None
    id: str = Field(default_factory=lambda: new_id("evt"))
    received_at: datetime | None = None
