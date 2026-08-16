"""Is anything still listening, and is it keeping up?

Two silent failures are under test. A subscriber that stopped committing looks
like a topic with nothing on it, and an abandoned one pins a topic's retention
forever while looking perfectly healthy. Both need something that *reads* the
gap between the registry and the checkpoints — they disagree in both directions
and each disagreement means something different.

The replay tests are the other half. ``start_at=EARLIEST`` is refused in a
declaration because its blast radius depends on data the author cannot see; the
whole justification for that refusal is that this exists, is bounded, and shows
you the number first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from loom.events import (
    EventRecord,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
    Subscription,
)
from loom.events.manager import GapDetected, SubscriptionManager
from loom.stores.memory import MemoryStore
from loom.triggers.filter import FilterSpec

TOPIC = "app.slack.message"


class Frozen:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, **kw: Any) -> None:
        self._at += timedelta(**kw)


@pytest.fixture
def wired():
    store = MemoryStore()
    log = StoreBackedEventLog(store)
    marks = StoreBackedCheckpoints(store)
    manager = SubscriptionManager(store, log=log, checkpoints=marks)
    return store, log, marks, manager


async def fill(log: Any, n: int, *, topic: str = TOPIC) -> None:
    await log.append(topic, [
        EventRecord(
            event_id=f"{topic}/slack:Ev{i}",
            type="slack.message",
            payload={"channel": "C_TECH", "n": i},
            key="C_TECH",
            source="slack",
        )
        for i in range(n)
    ])


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class TestRegistry:
    async def test_a_subscription_survives_a_restart(self, wired) -> None:
        """Durable, so a subscription added operationally is not re-derived from
        whatever workflows this process happened to import."""
        store, log, marks, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))

        fresh = SubscriptionManager(store, log=log, checkpoints=marks)

        assert [s.subscriber for s in await fresh.subscriptions()] == ["triage"]

    async def test_a_filter_round_trips(self, wired) -> None:
        _, _, _, manager = wired
        await manager.add(
            Subscription(
                "triage", TOPIC, "triage",
                filter=FilterSpec(conditions={"channel": "C_TECH"}),
            )
        )

        loaded = await manager.get("triage", TOPIC)
        assert loaded is not None
        assert loaded.accepts({"channel": "C_TECH"})
        assert not loaded.accepts({"channel": "C_RANDOM"})

    async def test_replacing_a_subscription_keeps_its_checkpoint(
        self, wired
    ) -> None:
        """Editing a filter must be an edit, not a new subscriber that re-reads
        all of history — which is the reason identity is a stable name."""
        _, _, marks, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await marks.commit("triage", TOPIC, "5")

        await manager.add(
            Subscription(
                "triage", TOPIC, "triage",
                filter=FilterSpec(conditions={"channel": {"$in": ["A", "B"]}}),
            )
        )

        assert await marks.load("triage", TOPIC) == "5"

    async def test_removing_one_forgets_its_checkpoint(self, wired) -> None:
        """Leaving it behind means re-adding the same name silently resumes —
        right for an edit, wrong for a deliberate removal, and indistinguishable
        afterwards."""
        _, _, marks, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await marks.commit("triage", TOPIC, "5")

        assert await manager.remove("triage", TOPIC)

        assert await marks.load("triage", TOPIC) is None
        assert await manager.subscriptions() == []

    async def test_removing_one_that_is_not_there_says_so(self, wired) -> None:
        _, _, _, manager = wired
        assert await manager.remove("nobody", TOPIC) is False


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_it_reports_how_far_behind_a_subscriber_is(self, wired) -> None:
        _, log, marks, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await fill(log, 10)
        await marks.commit("triage", TOPIC, "4")

        (row,) = await manager.health()

        assert row.position == "4"
        assert row.head == "10"
        assert row.lag == 6

    async def test_lag_is_counted_not_subtracted(self, wired) -> None:
        """`Position` is opaque: subtracting two works on this implementation
        and is wrong on every partitioned one."""
        _, log, _, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await fill(log, 3)

        (row,) = await manager.health()

        assert row.lag == 3, "a subscriber that has never committed is 3 behind"
        assert row.position is None
        assert not row.started

    async def test_a_subscriber_with_no_registration_still_appears(
        self, wired
    ) -> None:
        """A checkpoint with no registration is somebody removed from the code
        but not from the deployment — which is worth seeing."""
        _, log, marks, manager = wired
        await fill(log, 2)
        await marks.commit("ghost", TOPIC, "1")

        rows = {row.subscriber for row in await manager.health()}

        assert rows == {"ghost"}

    async def test_a_registration_with_no_checkpoint_still_appears(
        self, wired
    ) -> None:
        """It has never run, which is a different thing from being current."""
        _, _, _, manager = wired
        await manager.add(Subscription("new", TOPIC, "triage"))

        (row,) = await manager.health()

        assert row.position is None and row.lag == 0

    async def test_a_stale_subscriber_is_reported(self, wired) -> None:
        store, log, marks, _ = wired
        # Anchored to real time, because the checkpoint's own `updated_at` is
        # stamped by the store's clock rather than this one — a frozen clock in
        # the past makes every checkpoint look like it moved in the future.
        clock = Frozen(datetime.now(UTC))
        manager = SubscriptionManager(
            store, log=log, checkpoints=marks, subscriber_ttl=3600, clock=clock
        )
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await marks.commit("triage", TOPIC, "1")

        clock.advance(days=1)
        (row,) = await manager.health()

        assert not row.healthy
        assert "subscriber TTL" in row.reason

    async def test_reading_health_has_no_side_effects(self, wired) -> None:
        """Quarantining is an operator's decision; a health read that did it
        would change what `loom events status` reports by reporting it."""
        store, log, marks, _ = wired
        clock = Frozen(datetime.now(UTC))
        manager = SubscriptionManager(
            store, log=log, checkpoints=marks, subscriber_ttl=1, clock=clock
        )
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await marks.commit("triage", TOPIC, "1")
        clock.advance(days=1)

        await manager.health()
        (row,) = await manager.health()

        assert not row.quarantined, "reading must not quarantine"

    async def test_a_naive_timestamp_does_not_break_a_health_read(
        self, wired
    ) -> None:
        """SQLite hands back naive datetimes, and subtracting one from an aware
        now() raises — turning a health read into an error on the backend a
        laptop uses."""
        from loom.events.manager import _aware

        assert _aware(datetime(2026, 1, 1)).tzinfo is UTC

    async def test_it_scopes_to_one_topic(self, wired) -> None:
        _, _, _, manager = wired
        await manager.add(Subscription("a", TOPIC, "w"))
        await manager.add(Subscription("b", "app.jira.issue_created", "w"))

        rows = await manager.health(TOPIC)

        assert [row.subscriber for row in rows] == ["a"]


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


class TestQuarantine:
    async def test_a_quarantined_subscriber_keeps_its_position(
        self, wired
    ) -> None:
        """Deleting it instead would let the subscriber restart clean and report
        success — silent loss dressed as a healthy resume."""
        _, log, marks, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await fill(log, 5)
        await marks.commit("triage", TOPIC, "2")

        await manager.quarantine("triage", TOPIC, "abandoned")

        (row,) = await manager.health()
        assert row.quarantined and row.position == "2"
        assert row.reason == "abandoned"

    async def test_quarantine_survives_a_restart(self, wired) -> None:
        store, log, marks, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await manager.quarantine("triage", TOPIC, "abandoned")

        fresh = SubscriptionManager(store, log=log, checkpoints=marks)

        assert (await fresh.health())[0].quarantined

    async def test_resuming_past_retention_raises_rather_than_pretending(
        self, wired
    ) -> None:
        """The only unacceptable option is to pretend nothing happened."""
        _, log, marks, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await fill(log, 5)
        await marks.commit("triage", TOPIC, "1")
        await manager.quarantine("triage", TOPIC, "abandoned")

        # Retention has since discarded everything up to 3.
        from loom.events.models import RetentionPolicy

        await log.retain(TOPIC, RetentionPolicy(max_records=2))

        with pytest.raises(GapDetected) as exc:
            await manager.resume("triage", TOPIC)

        assert exc.value.subscriber == "triage"
        assert "cannot be enumerated" in str(exc.value)

    async def test_the_gap_can_be_accepted_explicitly(self, wired) -> None:
        _, log, marks, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await fill(log, 5)
        await marks.commit("triage", TOPIC, "1")
        await manager.quarantine("triage", TOPIC, "abandoned")
        from loom.events.models import RetentionPolicy

        await log.retain(TOPIC, RetentionPolicy(max_records=2))

        await manager.resume("triage", TOPIC, accept_gap=True)

        assert not (await manager.health())[0].quarantined

    async def test_resuming_one_still_within_retention_just_works(
        self, wired
    ) -> None:
        _, log, marks, manager = wired
        await manager.add(Subscription("triage", TOPIC, "triage"))
        await fill(log, 5)
        await marks.commit("triage", TOPIC, "4")
        await manager.quarantine("triage", TOPIC, "briefly")

        await manager.resume("triage", TOPIC)

        assert (await manager.health())[0].healthy

    async def test_resuming_something_not_quarantined_is_a_no_op(
        self, wired
    ) -> None:
        _, _, _, manager = wired
        await manager.resume("nobody", TOPIC)


# ---------------------------------------------------------------------------
# Bounded backfill
# ---------------------------------------------------------------------------


class TestReplay:
    async def test_a_plan_changes_nothing(self, wired) -> None:
        """The number is the safeguard: replaying a week of Slack into a
        workflow that replies means a week of replies, at once, to real
        people."""
        _, log, marks, manager = wired
        await fill(log, 10)
        await marks.commit("triage", TOPIC, "10")

        plan = await manager.plan_replay("triage", TOPIC, max_events=4)

        assert plan.events == 4
        assert await marks.load("triage", TOPIC) == "10", "planning must not act"

    async def test_the_ceiling_keeps_the_newest_and_says_it_truncated(
        self, wired
    ) -> None:
        """The same rule `max_catch_up` applies to a missed cron. Covering the
        oldest N would rewind past everything since, so the count in the plan
        and the number actually re-read would disagree — in the dangerous
        direction."""
        _, log, marks, manager = wired
        await fill(log, 10)
        await marks.commit("triage", TOPIC, "10")

        plan = await manager.replay("triage", TOPIC, max_events=3)

        assert plan.truncated
        assert plan.events == 3
        assert await marks.load("triage", TOPIC) == "7", (
            "the rewind must cover exactly the 3 the plan promised"
        )

    async def test_the_plan_and_the_rewind_agree(self, wired) -> None:
        """The bug this pins: a plan reporting 3 while the rewind covered 5."""
        _, log, marks, manager = wired
        await fill(log, 12)
        await marks.commit("triage", TOPIC, "12")

        plan = await manager.replay("triage", TOPIC, max_events=5)
        remaining = await log.read(
            TOPIC, after=await marks.load("triage", TOPIC), limit=100
        )

        assert len(remaining) == plan.events

    async def test_a_since_window_narrows_it(self, wired) -> None:
        _, log, _, manager = wired
        await fill(log, 5)

        recent = await manager.plan_replay(
            "triage", TOPIC, since=timedelta(hours=1), max_events=100
        )
        ancient = await manager.plan_replay(
            "triage", TOPIC, since=timedelta(seconds=0), max_events=100
        )

        assert recent.events == 5
        assert ancient.events == 0, "nothing was appended zero seconds ago"

    async def test_replaying_everything_rewinds_to_before_the_first_event(
        self, wired
    ) -> None:
        _, log, marks, manager = wired
        await fill(log, 3)
        await marks.commit("triage", TOPIC, "3")

        plan = await manager.replay("triage", TOPIC, max_events=100)

        assert plan.events == 3
        assert not plan.truncated
        after = await marks.load("triage", TOPIC)
        assert len(await log.read(TOPIC, after=after, limit=100)) == 3

    async def test_replaying_an_empty_topic_is_a_no_op(self, wired) -> None:
        _, _, _, manager = wired

        plan = await manager.replay("triage", "app.nothing.here")

        assert plan.events == 0

    async def test_it_rewinds_but_never_dispatches(self, wired) -> None:
        """Rewinding and letting the normal loop do the work means a backfill
        goes through the same admission, grants and dead-lettering as everything
        else, rather than a second path re-implementing all of it."""
        store, log, marks, manager = wired
        from loom import Runtime, workflow
        from loom.events import EventDispatcher

        @workflow(name="counting")
        async def counting(ctx: Any, message: dict) -> str:
            return "ok"

        runtime = Runtime(store=store, events=log)
        dispatcher = EventDispatcher(runtime, log=log, checkpoints=marks)
        await dispatcher.subscribe(Subscription("triage", TOPIC, "counting"))
        runtime.register(counting)
        await fill(log, 3)
        first = (await dispatcher.poll_once())[0]

        submitted: list[str] = []
        original = runtime.submit

        async def counting_submit(*args: Any, **kw: Any) -> str:
            submitted.append(kw.get("idempotency_key", ""))
            return await original(*args, **kw)

        runtime.submit = counting_submit  # type: ignore[method-assign]
        await manager.replay("triage", TOPIC, max_events=100)

        assert submitted == [], "replay must not dispatch on its own"

        second = (await dispatcher.poll_once())[0]
        assert second.read == 3, "the ordinary loop re-reads the window"
        assert second.started == first.started, (
            "and the dispatch key resolves each one to its original run rather "
            "than starting a duplicate"
        )


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------


class TestTopics:
    async def test_it_lists_what_the_log_knows_and_what_is_subscribed(
        self, wired
    ) -> None:
        """A broker that multiplexes may not be able to enumerate, and a
        subscription is proof a topic exists either way."""
        _, log, _, manager = wired
        await fill(log, 1, topic="app.slack.message")
        await manager.add(Subscription("s", "app.jira.issue_created", "w"))

        assert await manager.topics() == [
            "app.jira.issue_created",
            "app.slack.message",
        ]

    async def test_a_log_that_cannot_enumerate_degrades(self, wired) -> None:
        store, _, marks, _ = wired

        class Opaque:
            async def head(self, topic: str) -> None:
                return None

            async def read(self, topic: str, **kw: Any) -> list[Any]:
                return []

        manager = SubscriptionManager(store, log=Opaque(), checkpoints=marks)
        await manager.add(Subscription("s", TOPIC, "w"))

        assert await manager.topics() == [TOPIC]


# ---------------------------------------------------------------------------
# The CLI's duration parsing
# ---------------------------------------------------------------------------


class TestDuration:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("30m", 1800), ("12h", 43200), ("7d", 604800), ("2w", 1209600)],
    )
    def test_it_reads_a_number_and_a_unit(self, text: str, seconds: int) -> None:
        from loom.cli.event_commands import _duration

        assert _duration(text) == timedelta(seconds=seconds)

    def test_a_bare_number_is_refused_rather_than_guessed(self) -> None:
        """`--since 7` meaning seconds when the writer meant days turns a
        deliberate backfill into a no-op that reports success."""
        from loom.cli.event_commands import _duration

        with pytest.raises(SystemExit, match="not a duration"):
            _duration("7")
