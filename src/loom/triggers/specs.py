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

    Safe to run on every replica: each occurrence is submitted under a
    deterministic idempotency key (``trigger_id@scheduled_for``), so two
    dispatchers produce one run rather than two. To avoid the *duplicated work*
    — and to keep one process advancing the schedule — pass a dispatcher to
    :meth:`Runtime.start_scheduler` with a
    :class:`~loom.runtime.leader.LeaderElector`.
    """

    cron: str
    timezone: str = "UTC"
    catch_up: bool = False
    """Replay fire times missed while the scheduler was down, instead of skipping them."""
    max_catch_up: int = 10
    """Ceiling on a backfill, counted in occurrences.

    Only consulted when ``catch_up`` is set. The newest occurrences are the ones
    kept — after a two-week outage the last ten days of a daily report are worth
    more than the first ten — and the remainder are dropped with a warning
    naming the count. Without a ceiling, a per-minute schedule and a week of
    downtime submits ten thousand runs at once, which is a second outage caused
    by recovering from the first."""
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

    def describe(self, *, now: datetime | None = None) -> JSONDict:
        """What this trigger is. **Stable across calls**, unless *now* is given.

        ``next_fire`` used to be in here unconditionally, computed from the
        wall clock at the moment of the call, and that made the description a
        moving value. Two consequences, both silent. The dispatcher decides
        whether a registered trigger has changed by comparing
        ``existing.spec != spec.describe()``, so *every* registration wrote to
        the trigger store — a store round trip per boot per trigger, to persist
        a field nothing reads. And a run under a ``ManualClock`` persisted a
        trigger record stamped with the real date, so the one artefact a
        time-travel test could inspect was the one still on wall time.

        Pass *now* — the Runtime's ``clock.now()`` — when the answer is for a
        human or an API: then ``next_fire`` comes back, derived from a moment
        the caller chose rather than from whenever this happened to be called.
        """
        described: JSONDict = {
            "kind": self.kind.value,
            "name": self.name,
            "cron": self.cron,
            "timezone": self.timezone,
            "catch_up": self.catch_up,
            "max_catch_up": self.max_catch_up,
            # Carried like the rest of the policy: a field the dispatcher
            # cannot read is a field that does nothing, however clearly it is
            # declared on the spec.
            "jitter": to_seconds(self.jitter),
        }
        if now is not None:
            described["next_fire"] = self.next_fire(now).isoformat()
        return described


@dataclass(frozen=True, slots=True)
class Interval(TriggerSpec):
    """Fire every N seconds. Simpler than cron when the phase does not matter."""

    every: Duration
    kind: TriggerKind = field(default=TriggerKind.SCHEDULE, init=False)

    @property
    def name(self) -> str:
        return f"interval:{to_seconds(self.every)}s"

    def next_fire(self, after: datetime | None = None) -> datetime:
        """*after* plus the interval.

        Pass one. The wall-clock fallback exists for a caller holding nothing
        else, and it is the reason every scheduler path in LOOM supplies
        ``clock.now()`` explicitly: an interval computed from the wall clock
        inside a run on virtual time lands a year away from the schedule the
        test is driving, and looks like a trigger that simply never fires.
        """
        from datetime import timedelta

        base = after or datetime.now(UTC)
        return base + timedelta(seconds=to_seconds(self.every))

    def describe(self) -> JSONDict:
        return {"kind": self.kind.value, "name": self.name, "seconds": to_seconds(self.every)}


@dataclass(frozen=True, slots=True)
class After(TriggerSpec):
    """Fire **once**, a fixed delay after the trigger is first registered.

    The gap ``Schedule`` and ``Interval`` leave between them: one is a grid the
    workflow sits on forever, the other a cycle that never stops, and *"tell me
    a joke in two minutes"* is neither. Expressed with ``ctx.sleep`` instead, a
    one-off delay parks the run and waits for something to wake it — which is
    right inside a flow that is already under way, and wrong for a delay the
    request states up front, because there is nothing for the body to do until
    the clock says so.

    **Relative to registration, not to each boot.** ``TriggerDispatcher``
    keeps a known trigger's ``next_fire_at`` rather than recomputing it, so the
    two minutes are counted from when the dispatcher first learned of this
    trigger. Restarting the process does not push the joke into the future
    forever — the same property that stops a pod restarting more often than its
    cron from never firing at all.

    **One shot is a property of the record, not a counter.**
    ``_next_fire_from_record`` reads ``cron`` and ``seconds`` out of a stored
    spec and answers ``None`` for anything else, and the dispatcher already
    retires a trigger whose next fire is ``None``. So the delay is published as
    ``after_seconds`` deliberately: naming it ``seconds`` would make every
    stored one-shot indistinguishable from an ``Interval`` and it would repeat
    for ever, which is the one behaviour this spec exists to rule out.
    """

    seconds: Duration = 0
    minutes: Duration = 0
    hours: Duration = 0
    days: Duration = 0
    kind: TriggerKind = field(default=TriggerKind.SCHEDULE, init=False)

    def __post_init__(self) -> None:
        if self.delay <= 0:
            raise ValueError(
                "After(...) needs a delay greater than zero; a workflow meant "
                "to start straight away declares Manual() (or no trigger at "
                "all) instead."
            )

    @property
    def delay(self) -> float:
        """The whole delay in seconds."""
        return (
            to_seconds(self.seconds)
            + to_seconds(self.minutes) * 60
            + to_seconds(self.hours) * 3600
            + to_seconds(self.days) * 86400
        )

    @property
    def name(self) -> str:
        return f"after:{self.delay:g}s"

    def next_fire(self, after: datetime | None = None) -> datetime:
        """*after* plus the delay.

        Consulted once, at registration. Every later call goes through the
        stored record, which cannot reproduce this and answers ``None`` — that
        is what makes it fire once.
        """
        from datetime import timedelta

        base = after or datetime.now(UTC)
        return base + timedelta(seconds=self.delay)

    def describe(self) -> JSONDict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "after_seconds": self.delay,
            "once": True,
        }


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
class OnAppEvent(TriggerSpec):
    """Start this workflow when an event lands on a topic.

    The pub/sub entry point: many workflows may subscribe to one topic, each
    with its own filter and its own position, and none can hold up another.

        @workflow(triggers=[
            OnAppEvent("app.slack.message",
                       where=FilterSpec(conditions={"channel": "C024BE91L"})),
        ])

    Distinct from :class:`OnEvent`, which consumes a *broker* topic through a
    ``QueueBackend``. This one reads LOOM's own event log, so it resumes from a
    checkpoint and a second subscriber can be added without disturbing the
    first.

    **Filter on ids, not names.** Slack's payload carries
    ``"channel": "C024BE91L"``; a filter written as ``channel="tech"`` matches
    nothing, forever, with no error — the same failure ``resolves=`` exists to
    prevent in the toolsets, one layer up.
    """

    topic: str
    where: Any | None = None
    """A :class:`~loom.triggers.filter.FilterSpec`, evaluated against the event
    payload before a run is created."""
    subscription: str = ""
    """Identity for the checkpoint. Defaults to the workflow name.

    A workflow declaring **more than one** ``OnAppEvent`` must name each, or
    they share a checkpoint and silently consume each other's backlog.
    Deliberately *not* derived from the filter: an identity that changes when a
    filter is edited re-fires every historical event."""
    start_at: str = "latest"
    """``"latest"`` only, in a declaration. Backfilling a retained log into a
    workflow with side effects performs all of them at once, and the dispatch
    key cannot help — a new subscriber has legitimately seen none of them. That
    makes it an operational act with bounds, not a line in a workflow file."""
    max_attempts: int = 3
    kind: TriggerKind = field(default=TriggerKind.EVENT, init=False)

    @property
    def name(self) -> str:
        return f"app_event:{self.topic}"

    def subscription_for(self, workflow: str) -> Any:
        """The :class:`~loom.events.subscription.Subscription` this declares."""
        from loom.events.subscription import StartAt, Subscription

        built = Subscription(
            subscriber=self.subscription or workflow,
            topic=self.topic,
            workflow=workflow,
            filter=self.where,
            start_at=StartAt(self.start_at),
            max_attempts=self.max_attempts,
        )
        built.validate_declarable()
        return built

    def describe(self) -> JSONDict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "topic": self.topic,
            "subscription": self.subscription,
            "start_at": self.start_at,
            "filter": (
                self.where.conditions if self.where is not None else None
            ),
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


def build_manual_event(payload: Any = None, *, now: datetime | None = None) -> TriggerEvent:
    """A manual trigger event, stamped *now* — the Runtime's ``clock.now()``.

    The fallback is the wall clock, which is what makes the argument worth
    passing: a manual run inside a virtual-clock test otherwise carries a
    ``received_at`` from a different timeline than every other timestamp on
    the same run, and comparing the two reads as a run that arrived months
    before it started.
    """
    return TriggerEvent(
        kind=TriggerKind.MANUAL,
        payload=payload,
        trigger_name="manual",
        received_at=now or datetime.now(UTC),
    )


def describe_all(specs: Sequence[TriggerSpec]) -> list[JSONDict]:
    return [spec.describe() for spec in specs]
