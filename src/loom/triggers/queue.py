"""Message-queue ingress.

A queue consumer turns messages into workflow runs. The interesting part is not
the polling loop but the acknowledgement contract: a message must not be acked
until the run it started is durable, or a crash between the two loses work that
the broker believes was delivered.

The order is therefore always:

1. poll a message,
2. submit a run whose ``idempotency_key`` derives from the message id,
3. only then ack.

A crash after (2) and before (3) redelivers the message, and the idempotency key
makes the second submit resolve to the run already recorded rather than starting
a duplicate. That is at-least-once delivery with exactly-once execution, which is
the strongest thing you can build on a queue that offers at-least-once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from loom.core.exceptions import AdmissionRejected, ConfigurationError
from loom.core.models import TriggerKind
from loom.core.types import Duration, to_seconds

if TYPE_CHECKING:
    from loom.runtime.engine import Runtime
    from loom.runtime.workflow import WorkflowDefinition

logger = logging.getLogger("workflow.queue")

#: Messages per poll when neither the caller nor the trigger says otherwise.
_DEFAULT_BATCH_SIZE = 10


def _event_spec(workflow: Any) -> Any:
    """The workflow's first ``OnEvent`` trigger, if it declares one."""
    from loom.triggers.specs import OnEvent

    for spec in getattr(workflow, "triggers", ()):
        if isinstance(spec, OnEvent):
            return spec
    return None


class QueueMessage(BaseModel):
    """One message taken from a broker."""

    id: str
    """Broker-assigned identifier. Also seeds the run's idempotency key, so it
    must be stable across redeliveries of the same message."""
    payload: Any = None
    attempts: int = 1
    """How many times this message has been delivered, 1 on first delivery."""
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class QueueBackend(Protocol):
    """A broker LOOM can consume from.

    Implement this over Redis Streams, SQS, RabbitMQ, Kafka, or anything else.
    The consumer only needs to take messages, confirm them, and return them.
    """

    async def poll(self, max_messages: int) -> list[QueueMessage]:
        """Take up to *max_messages*, making them invisible to other consumers."""
        ...

    async def ack(self, message_id: str) -> None:
        """Confirm permanent handling. The message must not be redelivered."""
        ...

    async def nack(self, message_id: str, *, requeue: bool) -> None:
        """Return a message. ``requeue=False`` means give up on it."""
        ...


class InMemoryQueue:
    """Reference :class:`QueueBackend` for tests and single-process use.

    Models the parts of broker behaviour that matter for correctness: in-flight
    messages are invisible until acked or nacked, a nacked message comes back
    with an incremented attempt count, and a dead-lettered one is set aside
    rather than dropped.
    """

    def __init__(self) -> None:
        self._pending: list[QueueMessage] = []
        self._in_flight: dict[str, QueueMessage] = {}
        self.dead_letters: list[QueueMessage] = []
        self._counter = 0

    def publish(self, payload: Any, *, message_id: str | None = None) -> str:
        """Add a message. Returns its id."""
        if message_id is None:
            self._counter += 1
            message_id = f"msg-{self._counter}"
        self._pending.append(QueueMessage(id=message_id, payload=payload))
        return message_id

    @property
    def depth(self) -> int:
        """Messages waiting to be delivered, excluding in-flight ones."""
        return len(self._pending)

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)

    async def poll(self, max_messages: int) -> list[QueueMessage]:
        taken = self._pending[:max_messages]
        self._pending = self._pending[max_messages:]
        for message in taken:
            self._in_flight[message.id] = message
        return taken

    async def ack(self, message_id: str) -> None:
        self._in_flight.pop(message_id, None)

    async def nack(self, message_id: str, *, requeue: bool) -> None:
        message = self._in_flight.pop(message_id, None)
        if message is None:
            return
        if requeue:
            self._pending.append(message.model_copy(update={"attempts": message.attempts + 1}))
        else:
            self.dead_letters.append(message)


@dataclass
class ConsumeReport:
    """What one polling pass did."""

    submitted: list[str] = field(default_factory=list)
    """Run ids started, in message order."""
    requeued: list[str] = field(default_factory=list)
    dead_lettered: list[str] = field(default_factory=list)

    @property
    def handled(self) -> int:
        return len(self.submitted) + len(self.requeued) + len(self.dead_lettered)


class QueueConsumer:
    """Drives runs from a :class:`QueueBackend`.

    Parameters
    ----------
    runtime, workflow:
        Where each message goes.
    backend:
        The broker to consume from.
    max_attempts:
        Deliveries a message gets before it is dead-lettered instead of requeued.
    idempotency_prefix:
        Namespace for the derived idempotency key. Change it to deliberately
        reprocess a queue that has already been consumed.
    """

    def __init__(
        self,
        runtime: Runtime,
        backend: QueueBackend,
        workflow: WorkflowDefinition[Any, Any, Any] | str,
        *,
        max_attempts: int = 3,
        idempotency_prefix: str | None = None,
        idempotency_field: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._runtime = runtime
        self._backend = backend
        self._workflow = workflow
        self._max_attempts = max_attempts
        self._task: asyncio.Task[None] | None = None

        # Anything not passed explicitly falls back to what the workflow's own
        # OnEvent trigger declared. Ignoring the declaration here would make
        # dedupe semantics depend on which constructor you happened to use.
        declared = _event_spec(workflow)
        if idempotency_prefix is not None:
            self._prefix = idempotency_prefix
        else:
            self._prefix = declared.name if declared else "queue"

        if idempotency_field is not None:
            self._idempotency_field = idempotency_field
        else:
            self._idempotency_field = declared.idempotency_field if declared else None

        if batch_size is not None:
            self._batch_size = batch_size
        elif declared is not None:
            self._batch_size = declared.batch_size
        else:
            self._batch_size = _DEFAULT_BATCH_SIZE

    @classmethod
    def for_workflow(
        cls,
        runtime: Runtime,
        backend: QueueBackend,
        workflow: WorkflowDefinition[Any, Any, Any],
        *,
        topic: str | None = None,
        max_attempts: int = 3,
    ) -> QueueConsumer:
        """Build a consumer from the workflow's own :class:`OnEvent` trigger.

        The trigger already declares ``batch_size`` and ``idempotency_field``;
        reading them here is what keeps the declaration and the running consumer
        from drifting apart.

        The plain constructor already picks these up; the difference is that this
        one *insists* on a declaration instead of falling back to defaults.

        Raises :class:`ConfigurationError` when the workflow declares no matching
        ``OnEvent`` trigger — consuming into a workflow that never said it
        accepted events is almost always a wiring mistake.
        """
        from loom.triggers.specs import OnEvent

        events = [spec for spec in workflow.triggers if isinstance(spec, OnEvent)]
        if topic is not None:
            events = [spec for spec in events if spec.topic == topic]
        if not events:
            wanted = f" for topic '{topic}'" if topic else ""
            raise ConfigurationError(
                f"workflow '{workflow.name}' declares no OnEvent trigger{wanted}; "
                "add triggers=[OnEvent(topic=...)] to consume a queue into it."
            )
        spec = events[0]
        return cls(
            runtime,
            backend,
            workflow,
            max_attempts=max_attempts,
            idempotency_prefix=spec.name,
            idempotency_field=spec.idempotency_field,
            batch_size=spec.batch_size,
        )

    def idempotency_key(self, message: QueueMessage) -> str:
        """Key that makes a redelivered message resolve to its existing run.

        Defaults to the broker's message id. When the trigger names an
        ``idempotency_field``, that field of the payload wins instead — brokers
        assign a fresh id to a republished message, so business keys are what
        deduplicate a producer that sends the same order twice.
        """
        if self._idempotency_field and isinstance(message.payload, dict):
            value = message.payload.get(self._idempotency_field)
            if value is not None:
                return f"{self._prefix}:{value}"
        return f"{self._prefix}:{message.id}"

    async def poll_once(self, *, max_messages: int | None = None) -> ConsumeReport:
        """Take a batch, start a run per message, ack only what became durable."""
        report = ConsumeReport()
        for message in await self._backend.poll(max_messages or self._batch_size):
            try:
                run_id = await self._runtime.submit(
                    self._workflow,
                    message.payload,
                    trigger=TriggerKind.EVENT,
                    idempotency_key=self.idempotency_key(message),
                )
            except AdmissionRejected as rejected:
                # Flow control said not now. Returning the message to the broker
                # is better than dropping it — unless the policy said "skip",
                # which means this message is never going to be wanted.
                requeue = rejected.retryable
                await self._backend.nack(message.id, requeue=requeue)
                (report.requeued if requeue else report.dead_lettered).append(message.id)
                logger.info("message %s not admitted: %s", message.id, rejected)
            except Exception:
                # The run was never recorded, so the message is still owed. Give
                # it back until it has had its attempts.
                requeue = message.attempts < self._max_attempts
                await self._backend.nack(message.id, requeue=requeue)
                (report.requeued if requeue else report.dead_lettered).append(message.id)
                logger.exception(
                    "failed to submit message %s (attempt %d/%d)",
                    message.id,
                    message.attempts,
                    self._max_attempts,
                )
            else:
                # The record is durable; the broker can forget the message. A
                # workflow that later fails is a recorded failure, not lost work,
                # so it must not come back through the queue.
                await self._backend.ack(message.id)
                report.submitted.append(run_id)
        return report

    async def start(
        self, *, interval: Duration = 1.0, max_messages: int | None = None
    ) -> None:
        """Poll in the background until :meth:`stop`."""
        if self._task is not None:
            return

        delay = to_seconds(interval)

        async def loop() -> None:
            while True:
                try:
                    await self.poll_once(max_messages=max_messages)
                except Exception:
                    logger.exception("queue poll failed")
                await asyncio.sleep(delay)

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        """Stop polling. In-flight submissions are allowed to finish."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
