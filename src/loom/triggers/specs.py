"""Concrete trigger declarations.

Between them these cover the entry points that make a workflow product usable rather than
merely a library: a public URL, a clock, a poller with cursor state, a queue consumer, a
hosted form, a chat endpoint, sub-workflow invocation, and a failure hook.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from loom.core.models import TriggerKind
from loom.core.types import Duration, JSONDict, to_seconds
from loom.triggers.base import TriggerEvent, TriggerSpec
from loom.triggers.cron import CronSchedule


class ResponseMode(StrEnum):
    """When and what an HTTP-style trigger returns to its caller."""

    ACK = "ack"
    """Return 202 immediately; the run continues in the background."""
    RESULT = "result"
    """Hold the connection and return the workflow's output."""
    STREAM = "stream"
    """Server-sent events as the workflow emits them."""


class AuthMode(StrEnum):
    NONE = "none"
    BASIC = "basic"
    HEADER = "header"
    HMAC = "hmac"
    """Verify a signature header, the standard for provider webhooks."""
    BEARER = "bearer"


@dataclass(frozen=True, slots=True)
class Webhook(TriggerSpec):
    """An HTTP endpoint that starts the workflow.

    The host provisions separate test and production paths so that pointing a provider at
    a development machine never risks firing production runs.
    """

    path: str
    methods: tuple[str, ...] = ("POST",)
    auth: AuthMode = AuthMode.NONE
    auth_config: JSONDict = field(default_factory=dict)
    response: ResponseMode = ResponseMode.ACK
    idempotency_header: str | None = None
    """Header carrying the provider's delivery id, used to dedupe redeliveries."""
    raw_body: bool = False
    kind: TriggerKind = field(default=TriggerKind.WEBHOOK, init=False)

    @property
    def name(self) -> str:
        return f"webhook:{self.path}"

    def describe(self) -> JSONDict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "path": self.path,
            "methods": list(self.methods),
            "auth": self.auth.value,
            "response": self.response.value,
            "production_url": f"/webhook{self.path}",
            "test_url": f"/webhook-test{self.path}",
        }

    def idempotency_key_for(self, event: TriggerEvent) -> str | None:
        if event.idempotency_key:
            return event.idempotency_key
        if self.idempotency_header:
            return event.headers.get(self.idempotency_header.lower())
        return None


@dataclass(frozen=True, slots=True)
class Schedule(TriggerSpec):
    """Cron-driven execution.

    In a multi-replica deployment the host must run schedules only on the elected leader;
    see :mod:`loom.worker.leader`. Double-firing crons is the classic way a
    horizontally scaled scheduler corrupts data.
    """

    cron: str
    timezone: str = "UTC"
    catch_up: bool = False
    """Replay fire times missed while the scheduler was down, instead of skipping them."""
    jitter: Duration = 0.0
    kind: TriggerKind = field(default=TriggerKind.SCHEDULE, init=False)

    def __post_init__(self) -> None:
        CronSchedule.parse(self.cron, timezone=self.timezone)

    @property
    def name(self) -> str:
        return f"schedule:{self.cron}"

    @property
    def schedule(self) -> CronSchedule:
        return CronSchedule.parse(self.cron, timezone=self.timezone)

    def next_fire(self, after: datetime | None = None) -> datetime:
        return self.schedule.next_after(after)

    def describe(self) -> JSONDict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "cron": self.cron,
            "timezone": self.timezone,
            "catch_up": self.catch_up,
            "next_fire": self.next_fire().isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Interval(TriggerSpec):
    """Fire every N seconds. Simpler than cron when the phase does not matter."""

    every: Duration
    kind: TriggerKind = field(default=TriggerKind.SCHEDULE, init=False)

    @property
    def name(self) -> str:
        return f"interval:{to_seconds(self.every)}s"

    def next_fire(self, after: datetime | None = None) -> datetime:
        from datetime import timedelta

        base = after or datetime.now(UTC)
        return base + timedelta(seconds=to_seconds(self.every))

    def describe(self) -> JSONDict:
        return {"kind": self.kind.value, "name": self.name, "seconds": to_seconds(self.every)}


@dataclass(frozen=True, slots=True)
class Poll(TriggerSpec):
    """Poll a source that has no webhook, carrying a cursor between invocations.

    The host persists whatever the poll function returns as ``cursor`` and hands it back
    next time. A poll that yields nothing is not an execution, so idle polling stays free.
    """

    every: Duration
    cursor_key: str = "cursor"
    dedupe_key: str | None = None
    """Field on each item used to suppress items already seen."""
    kind: TriggerKind = field(default=TriggerKind.POLL, init=False)

    @property
    def name(self) -> str:
        return f"poll:{to_seconds(self.every)}s"

    def describe(self) -> JSONDict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "seconds": to_seconds(self.every),
            "cursor_key": self.cursor_key,
        }


@dataclass(frozen=True, slots=True)
class OnEvent(TriggerSpec):
    """Consume from a queue or event bus (Kafka, SQS, AMQP, Redis Streams, NATS)."""

    topic: str
    source: str = "default"
    group: str | None = None
    batch_size: int = 1
    idempotency_field: str | None = None
    kind: TriggerKind = field(default=TriggerKind.EVENT, init=False)

    @property
    def name(self) -> str:
        return f"event:{self.source}:{self.topic}"

    def describe(self) -> JSONDict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "topic": self.topic,
            "source": self.source,
            "group": self.group,
            "batch_size": self.batch_size,
        }


@dataclass(frozen=True, slots=True)
class Manual(TriggerSpec):
    """Invocable from the CLI, a test, or the dev UI. Implied when no trigger is declared."""

    label: str = "manual"
    kind: TriggerKind = field(default=TriggerKind.MANUAL, init=False)

    @property
    def name(self) -> str:
        return f"manual:{self.label}"


@dataclass(frozen=True, slots=True)
class FormField:
    """One input on a hosted form."""

    name: str
    label: str = ""
    type: str = "text"
    """text, textarea, number, email, date, select, checkbox, file."""
    required: bool = False
    options: tuple[str, ...] = ()
    placeholder: str = ""

    def to_json_schema(self) -> JSONDict:
        mapping = {
            "number": {"type": "number"},
            "checkbox": {"type": "boolean"},
            "date": {"type": "string", "format": "date"},
            "email": {"type": "string", "format": "email"},
            "file": {"type": "string", "contentEncoding": "base64"},
        }
        schema: JSONDict = dict(mapping.get(self.type, {"type": "string"}))
        if self.options:
            schema["enum"] = list(self.options)
        if self.label:
            schema["title"] = self.label
        return schema


@dataclass(frozen=True, slots=True)
class Form(TriggerSpec):
    """A hosted, generated HTML form that starts the workflow on submit.

    Cheap to provide and disproportionately useful: it turns a workflow into an internal
    tool without anyone building a front end.
    """

    path: str
    title: str = "Submit"
    description: str = ""
    fields: tuple[FormField, ...] = ()
    auth: AuthMode = AuthMode.NONE
    response: ResponseMode = ResponseMode.ACK
    kind: TriggerKind = field(default=TriggerKind.FORM, init=False)

    @property
    def name(self) -> str:
        return f"form:{self.path}"

    def json_schema(self) -> JSONDict:
        return {
            "type": "object",
            "title": self.title,
            "description": self.description,
            "properties": {f.name: f.to_json_schema() for f in self.fields},
            "required": [f.name for f in self.fields if f.required],
        }

    def describe(self) -> JSONDict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "path": self.path,
            "url": f"/form{self.path}",
            "schema": self.json_schema(),
        }


@dataclass(frozen=True, slots=True)
class Chat(TriggerSpec):
    """A conversational endpoint with session continuity and streaming responses.

    ``session_scoped=True`` keeps one long-lived execution per conversation rather than
    starting a new run per message, so history and cost accounting stay in one place.
    """

    path: str = "/chat"
    auth: AuthMode = AuthMode.NONE
    streaming: bool = True
    session_scoped: bool = True
    load_history: bool = True
    kind: TriggerKind = field(default=TriggerKind.CHAT, init=False)

    @property
    def name(self) -> str:
        return f"chat:{self.path}"

    def describe(self) -> JSONDict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "path": self.path,
            "url": f"/chat{self.path}",
            "streaming": self.streaming,
            "session_scoped": self.session_scoped,
        }


@dataclass(frozen=True, slots=True)
class CalledByWorkflow(TriggerSpec):
    """Marks a workflow as callable by others, with a typed contract."""

    label: str = "sub_workflow"
    kind: TriggerKind = field(default=TriggerKind.SUB_WORKFLOW, init=False)

    @property
    def name(self) -> str:
        return f"sub_workflow:{self.label}"


@dataclass(frozen=True, slots=True)
class OnFailure(TriggerSpec):
    """Run this workflow whenever a watched workflow fails.

    ``watches=()`` means every workflow in the app, which is the usual configuration for a
    single alerting handler.
    """

    watches: tuple[str, ...] = ()
    include_suspended_timeouts: bool = False
    kind: TriggerKind = field(default=TriggerKind.ERROR_HANDLER, init=False)

    @property
    def name(self) -> str:
        return "on_failure:" + (",".join(self.watches) if self.watches else "*")

    def handles(self, workflow_name: str) -> bool:
        return not self.watches or workflow_name in self.watches


@dataclass(frozen=True, slots=True)
class EmailInbox(TriggerSpec):
    """IMAP mailbox polling."""

    mailbox: str = "INBOX"
    credential: str = "imap"
    every: Duration = 60.0
    mark_seen: bool = True
    kind: TriggerKind = field(default=TriggerKind.EMAIL, init=False)

    @property
    def name(self) -> str:
        return f"email:{self.mailbox}"


PollFunction = Callable[[Any], Any]


def build_manual_event(payload: Any = None) -> TriggerEvent:
    return TriggerEvent(
        kind=TriggerKind.MANUAL,
        payload=payload,
        trigger_name="manual",
        received_at=datetime.now(UTC),
    )


def describe_all(specs: Sequence[TriggerSpec]) -> list[JSONDict]:
    return [spec.describe() for spec in specs]
