"""Is anything still listening, and is it keeping up?

Everything else in this package assumes events keep arriving and subscribers
keep reading. This is the part that notices when neither is true — because both
failures are silent. A lapsed provider subscription looks like a quiet week; a
subscriber that stopped committing looks like a topic with nothing on it.

Three things live here, and each answers a question no other component can:

:class:`SubscriptionManager`
    What is subscribed, where it has got to, and how far behind it is. The
    registry is durable, so a subscription added operationally survives a
    restart and is not re-derived from whatever workflows this process
    happened to import.

**Quarantine, not retirement.** A subscriber whose checkpoint has not moved in
``subscriber_ttl`` stops holding retention back — otherwise one abandoned
subscriber pins a topic forever — but its position is *kept*, and resuming it
raises :class:`GapDetected` naming what it missed. The third option, pretending
nothing happened, is the only unacceptable one: it is silent data loss dressed
as a successful resume.

**Backfill is bounded and explicit.** :meth:`SubscriptionManager.replay` is what
``start_at=EARLIEST`` is refused in favour of. It takes a ceiling and a time
window, reports what it will do before doing it, and re-derives each event's
original dispatch key — so replaying a widened filter re-runs only what newly
matches, and everything already handled deduplicates away.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loom.events.subscription import StartAt, Subscription

if TYPE_CHECKING:
    from loom.events.log import Checkpoints, EventLog

logger = logging.getLogger("workflow.events")

__all__ = [
    "GapDetected",
    "ReplayPlan",
    "SubscriptionHealth",
    "SubscriptionManager",
]

_REGISTRY_KEY = "eventsub:registry"
_QUARANTINE_KEY = "eventsub:quarantined"
_NO_TTL = 0.0

#: How long a checkpoint may sit still before its subscriber is quarantined.
#: A week: long enough to survive a holiday deployment freeze, short enough that
#: an abandoned subscriber does not pin a topic's retention for a quarter.
DEFAULT_SUBSCRIBER_TTL = 7 * 24 * 3600.0

#: How far back a replay plan looks. A ceiling rather than "everything": the
#: plan is bounded work by design, and a topic with a million retained events
#: should not be fully materialised to answer "what would replaying the last
#: hundred cover?". A window this large is past any sane replay ceiling.
_SCAN_LIMIT = 100_000


class GapDetected(Exception):  # noqa: N818 - names the state, not an error class
    """A quarantined subscriber was resumed and cannot account for the interval.

    Raised rather than returned, and this is the one place in the package where
    that is right: every other "we lost visibility" surfaces as a ``*.gap``
    event because a workflow can act on it. This one is a **caller** error — an
    operator resuming something that has been asleep past retention — and the
    caller must choose between accepting the gap and replaying what remains.
    """

    def __init__(self, message: str, *, subscriber: str = "", topic: str = "") -> None:
        super().__init__(message)
        self.subscriber = subscriber
        self.topic = topic


@dataclass
class SubscriptionHealth:
    """One subscriber's standing on one topic."""

    subscriber: str
    topic: str
    position: str | None = None
    head: str | None = None
    lag: int | None = None
    """Events between the checkpoint and the head. ``None`` when the log cannot
    say cheaply — a broker that multiplexes topics may not be able to, and
    guessing would put a wrong number in front of somebody deciding whether to
    page."""
    idle_seconds: float | None = None
    quarantined: bool = False
    reason: str = ""

    @property
    def healthy(self) -> bool:
        return not self.quarantined and not self.reason

    @property
    def started(self) -> bool:
        """Whether this subscriber has ever committed. A subscription that has
        never moved is not *behind* — it may simply be new."""
        return self.position is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "subscriber": self.subscriber,
            "topic": self.topic,
            "position": self.position,
            "head": self.head,
            "lag": self.lag,
            "idle_seconds": self.idle_seconds,
            "quarantined": self.quarantined,
            "reason": self.reason,
            "healthy": self.healthy,
        }


@dataclass(frozen=True, slots=True)
class ReplayPlan:
    """What a backfill would do, before it does it."""

    subscriber: str
    topic: str
    from_position: str | None
    events: int
    truncated: bool = False
    """``True`` when the ceiling cut the plan short. The replay still runs, and
    the count is what it will actually cover — a plan that silently reported the
    full backlog while covering a tenth of it is the failure ``Results`` exists
    to prevent, one layer up."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "subscriber": self.subscriber,
            "topic": self.topic,
            "from_position": self.from_position,
            "events": self.events,
            "truncated": self.truncated,
        }


class SubscriptionManager:
    """The durable registry of who is reading what, and how they are doing.

    Separate from :class:`~loom.events.dispatcher.EventDispatcher` because they
    answer different questions and fail in different ways: the dispatcher's job
    is to get events to workflows, and it is *supposed* to be a hot loop with no
    storage of its own. Fusing them would put a store round trip in the dispatch
    path to answer a question nobody asks per event.

        manager = SubscriptionManager(store, log=rt.events,
                                      checkpoints=StoreBackedCheckpoints(store))
        await manager.add(Subscription("triage", "app.slack.message", "triage"))
        for row in await manager.health():
            print(row.subscriber, row.lag)
    """

    def __init__(
        self,
        store: Any,
        *,
        log: EventLog | None = None,
        checkpoints: Checkpoints | None = None,
        subscriber_ttl: float = DEFAULT_SUBSCRIBER_TTL,
        clock: Any = None,
    ) -> None:
        self._store = store
        self._log = log
        self._marks = checkpoints
        self._ttl = subscriber_ttl
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock.now() if self._clock is not None else datetime.now(UTC)

    # -- the registry --------------------------------------------------------

    async def add(self, subscription: Subscription) -> Subscription:
        """Record *subscription* durably. Replacing one keeps its checkpoint.

        Keeping the checkpoint is the whole reason identity is a stable name
        (see :class:`~loom.events.subscription.Subscription`): editing a filter
        must be an edit, not a new subscriber that re-reads all of history.
        """
        registry = await self._load()
        registry[self._id(subscription.subscriber, subscription.topic)] = {
            "subscriber": subscription.subscriber,
            "topic": subscription.topic,
            "workflow": subscription.workflow,
            "filter": _dump_filter(subscription.filter),
            "start_at": str(subscription.start_at),
            "max_attempts": subscription.max_attempts,
        }
        await self._save(registry)
        return subscription

    async def remove(self, subscriber: str, topic: str) -> bool:
        """Forget a subscription **and** its checkpoint.

        Both, because leaving the checkpoint behind means re-adding the same
        name silently resumes from wherever it was — which is right for an edit
        and wrong for a deliberate removal, and the two are indistinguishable
        afterwards.
        """
        registry = await self._load()
        removed = registry.pop(self._id(subscriber, topic), None) is not None
        if removed:
            await self._save(registry)
            if self._marks is not None:
                await self._marks.forget(subscriber, topic)
        return removed

    async def subscriptions(self) -> list[Subscription]:
        """Every registered subscription.

        Not called ``list``: a method of that name shadows the builtin inside
        the class body, so every ``-> list[...]`` annotation in the class
        silently resolves to the method instead of the type. mypy catches it;
        nothing at runtime does.
        """
        registry = await self._load()
        return [_load_subscription(row) for row in registry.values()]

    async def get(self, subscriber: str, topic: str) -> Subscription | None:
        row = (await self._load()).get(self._id(subscriber, topic))
        return _load_subscription(row) if row else None

    # -- health --------------------------------------------------------------

    async def health(self, topic: str | None = None) -> list[SubscriptionHealth]:
        """Where every subscriber has got to, and how far behind that is.

        Reads the registry *and* the checkpoints, because they disagree in both
        directions and each disagreement means something: a registered
        subscription with no checkpoint has never run, and a checkpoint with no
        registration is a subscriber somebody removed from the code without
        removing from the deployment.
        """
        quarantined = await self._quarantined()
        rows: dict[tuple[str, str], SubscriptionHealth] = {}

        for subscription in await self.subscriptions():
            if topic is not None and subscription.topic != topic:
                continue
            rows[(subscription.subscriber, subscription.topic)] = SubscriptionHealth(
                subscription.subscriber, subscription.topic
            )

        if self._marks is not None:
            for name in await self._topics(topic):
                for subscriber, mark in (await self._marks.active(name)).items():
                    row = rows.setdefault(
                        (subscriber, name), SubscriptionHealth(subscriber, name)
                    )
                    row.position = mark.position
                    if mark.updated_at is not None:
                        row.idle_seconds = max(
                            0.0, (self._now() - _aware(mark.updated_at)).total_seconds()
                        )

        for (subscriber, name), row in rows.items():
            row.quarantined = f"{subscriber}@{name}" in quarantined
            if row.quarantined:
                row.reason = quarantined[f"{subscriber}@{name}"]
            elif (
                row.idle_seconds is not None
                and self._ttl > 0
                and row.idle_seconds > self._ttl
            ):
                # Reported, not acted on. Quarantining is an operator's decision
                # and a retention consequence; a health read must not have side
                # effects, or `loom events status` would change what it reports.
                row.reason = (
                    f"has not committed in {int(row.idle_seconds)}s, past the "
                    f"{int(self._ttl)}s subscriber TTL"
                )
            await self._fill_lag(row)

        return sorted(rows.values(), key=lambda r: (r.topic, r.subscriber))

    async def _fill_lag(self, row: SubscriptionHealth) -> None:
        if self._log is None:
            return
        row.head = await self._log.head(row.topic)
        if row.head is None:
            row.lag = 0
            return
        # Counted by reading, because `Position` is opaque — subtracting two
        # would work on this implementation and be wrong on every partitioned
        # one, which is exactly the assumption the opacity rule exists to stop.
        try:
            behind = await self._log.read(row.topic, after=row.position, limit=10_000)
        except Exception:
            row.lag = None
            return
        row.lag = len(behind)

    # -- quarantine ----------------------------------------------------------

    async def quarantine(self, subscriber: str, topic: str, reason: str) -> None:
        """Stop *subscriber* holding retention back, without forgetting it.

        Its position is kept, so :meth:`resume` can say precisely what it
        missed. Deleting it instead would let the subscriber restart clean and
        report success — silent loss dressed as a healthy resume.
        """
        current = await self._quarantined()
        current[f"{subscriber}@{topic}"] = reason
        await self._store.set(_QUARANTINE_KEY, json.dumps(current), _NO_TTL)
        logger.warning(
            "quarantined subscriber '%s' on '%s': %s. Its checkpoint is kept; "
            "retention will now proceed past it.",
            subscriber,
            topic,
            reason,
        )

    async def resume(self, subscriber: str, topic: str, *, accept_gap: bool = False) -> None:
        """Take *subscriber* out of quarantine.

        :raises GapDetected: unless *accept_gap*, when the position it holds is
            no longer in the log — which is the normal case, since quarantine
            exists precisely so retention can pass it.
        """
        current = await self._quarantined()
        key = f"{subscriber}@{topic}"
        if key not in current:
            return

        if not accept_gap and self._marks is not None and self._log is not None:
            position = await self._marks.load(subscriber, topic)
            if position is not None and not await self._still_readable(topic, position):
                raise GapDetected(
                    f"'{subscriber}' was quarantined at position {position} on "
                    f"'{topic}', and the log no longer holds it — so the "
                    "events between there and now cannot be enumerated. Resume "
                    "with accept_gap=True to continue from what is retained, "
                    "having recorded that the interval is unaccounted for.",
                    subscriber=subscriber,
                    topic=topic,
                )

        del current[key]
        await self._store.set(_QUARANTINE_KEY, json.dumps(current), _NO_TTL)
        logger.info("released subscriber '%s' on '%s' from quarantine", subscriber, topic)

    async def _still_readable(self, topic: str, position: str) -> bool:
        """Whether *position* is still a place the log can read from.

        Probing with a one-record read rather than comparing positions, because
        they are opaque. A log that has discarded past the position returns
        records that are *newer* than it, which the probe cannot distinguish —
        so this is deliberately conservative: it only reports readable when the
        position is the head or something follows it, and the caller's escape
        hatch is `accept_gap`.
        """
        try:
            if await self._log.head(topic) == position:  # type: ignore[union-attr]
                return True
            oldest = await self._log.read(topic, after=None, limit=1)  # type: ignore[union-attr]
            if not oldest:
                return True
            return oldest[0].position <= position
        except Exception:
            return True

    async def _quarantined(self) -> dict[str, str]:
        raw = await self._store.get(_QUARANTINE_KEY)
        if not raw:
            return {}
        try:
            return dict(json.loads(raw) if isinstance(raw, str) else raw)
        except (TypeError, ValueError):
            return {}

    # -- bounded backfill ----------------------------------------------------

    async def plan_replay(
        self,
        subscriber: str,
        topic: str,
        *,
        since: timedelta | None = None,
        max_events: int = 1000,
    ) -> ReplayPlan:
        """What replaying would cover. Costs a read and changes nothing.

        Separate from :meth:`replay` on purpose. ``EARLIEST`` is refused in a
        declaration because its blast radius depends on data the author cannot
        see; a backfill run by an operator has the same problem unless they can
        look at the number first.
        """
        if self._log is None:
            return ReplayPlan(subscriber, topic, None, 0)

        # The whole retained window, not `limit=max_events`: the ceiling has to
        # be applied to the *newest* events, and reading only the first
        # `max_events` would make the plan describe the oldest ones instead.
        candidates = list(await self._log.read(topic, after=None, limit=_SCAN_LIMIT))
        if since is not None:
            cutoff = self._now() - since
            candidates = [e for e in candidates if _aware(e.appended_at) >= cutoff]

        truncated = len(candidates) > max_events
        # The newest, and the oldest are dropped — the same rule `max_catch_up`
        # applies to a missed cron. A replay that covered the oldest N would
        # rewind past everything since, so the count in the plan and the number
        # the dispatcher then re-reads would disagree, in the dangerous
        # direction.
        selected = candidates[-max_events:] if max_events > 0 else []
        start = None if not selected else _before(selected[0].position)
        return ReplayPlan(subscriber, topic, start, len(selected), truncated)

    async def replay(
        self,
        subscriber: str,
        topic: str,
        *,
        since: timedelta | None = None,
        max_events: int = 1000,
    ) -> ReplayPlan:
        """Rewind *subscriber* so the next pass re-reads the planned window.

        Safe by construction rather than by care: each re-read event derives its
        original dispatch key (``{event_id}#{subscriber}``), so everything this
        subscriber already handled deduplicates away and only newly-matching
        events start runs. That is what makes widening a filter retroactively an
        ordinary operation instead of a migration.

        It does **not** dispatch. Rewinding the checkpoint and letting the
        normal loop do the work means a backfill goes through the same
        admission, grants, and dead-lettering as everything else, rather than a
        second path that has to re-implement all of it.
        """
        plan = await self.plan_replay(
            subscriber, topic, since=since, max_events=max_events
        )
        if self._marks is None:
            return plan
        if plan.from_position is None:
            await self._marks.forget(subscriber, topic)
        else:
            await self._marks.commit(subscriber, topic, plan.from_position)
        logger.info(
            "rewound '%s' on '%s' to replay %d event(s)%s",
            subscriber,
            topic,
            plan.events,
            " (truncated by the ceiling)" if plan.truncated else "",
        )
        return plan

    # -- topics --------------------------------------------------------------

    async def topics(self) -> list[str]:
        """Every topic this deployment knows about.

        The log's own list where it can produce one, plus every topic a
        subscription names — a broker that multiplexes may not be able to
        enumerate, and a subscription is proof a topic exists either way.
        """
        return await self._topics(None)

    async def _topics(self, only: str | None) -> list[str]:
        if only is not None:
            return [only]
        names: set[str] = {s.topic for s in await self.subscriptions()}
        lister = getattr(self._log, "topics", None)
        if lister is not None:
            try:
                names.update(await lister())
            except Exception:
                logger.debug("event log could not enumerate topics", exc_info=True)
        return sorted(names)

    # -- storage -------------------------------------------------------------

    def _id(self, subscriber: str, topic: str) -> str:
        return f"{subscriber}@{topic}"

    async def _load(self) -> dict[str, dict[str, Any]]:
        raw = await self._store.get(_REGISTRY_KEY)
        if not raw:
            return {}
        try:
            return dict(json.loads(raw) if isinstance(raw, str) else raw)
        except (TypeError, ValueError):
            return {}

    async def _save(self, registry: dict[str, dict[str, Any]]) -> None:
        await self._store.set(_REGISTRY_KEY, json.dumps(registry), _NO_TTL)

    def __repr__(self) -> str:
        return "<SubscriptionManager>"


def _dump_filter(spec: Any) -> dict[str, Any] | None:
    if spec is None:
        return None
    for attribute in ("model_dump", "as_dict", "to_dict"):
        dumper = getattr(spec, attribute, None)
        if dumper is not None:
            try:
                return dict(dumper())
            except Exception:
                continue
    conditions = getattr(spec, "conditions", None)
    return {"conditions": dict(conditions)} if conditions else None


def _load_subscription(row: dict[str, Any]) -> Subscription:
    from loom.triggers.filter import FilterSpec

    raw_filter = row.get("filter")
    spec = None
    if raw_filter:
        try:
            spec = FilterSpec(**raw_filter)
        except Exception:
            spec = FilterSpec(conditions=dict(raw_filter.get("conditions") or {}))
    return Subscription(
        subscriber=str(row.get("subscriber", "")),
        topic=str(row.get("topic", "")),
        workflow=str(row.get("workflow", "")),
        filter=spec,
        start_at=StartAt(row.get("start_at") or StartAt.LATEST),
        max_attempts=int(row.get("max_attempts", 3) or 3),
    )


def _aware(value: datetime) -> datetime:
    """A naive timestamp read back from a store is treated as UTC.

    Not a formality: SQLite hands back naive datetimes, and subtracting one from
    an aware `now()` raises — which would turn a health read into an error on
    exactly the backend a laptop uses.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _before(position: str) -> str | None:
    """A position that reads *up to but not including* the given one.

    The reference log numbers sequentially, so this is one less. It returns
    ``None`` — meaning "from the beginning of what is retained" — for anything
    it cannot decrement, which is correct for an opaque position and is why
    :meth:`SubscriptionManager.replay` never claims a start it cannot honour.
    """
    try:
        value = int(position)
    except (TypeError, ValueError):
        return None
    return str(value - 1) if value > 0 else None
