"""Fan-out: many workflows, one event, independent cursors.

The phase-2 claim is that a topic can have several readers, each with its own
filter and its own position, and that none of them can lose an event or hold
another back. These tests are that claim, one property at a time.

The ordering inside a pass — read, filter, submit, *then* commit — is asserted
directly rather than inferred, because committing early is the one mistake here
that loses data permanently and it is invisible until a process dies.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from loom import Context, Runtime, workflow
from loom.core.exceptions import ConfigurationError
from loom.events import (
    CHAIN_DEPTH_CAP,
    EventDispatcher,
    EventRecord,
    StartAt,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
    Subscription,
)
from loom.stores.memory import MemoryStore
from loom.triggers.filter import FilterSpec
from loom.triggers.specs import OnAppEvent

TOPIC = "app.slack.message"

#: Every workflow below appends here, so a test can assert what actually ran.
RAN: list[tuple[str, str]] = []


@pytest.fixture(autouse=True)
def _clear() -> None:
    RAN.clear()


@workflow(name="triage", triggers=[
    OnAppEvent(TOPIC, where=FilterSpec(conditions={"channel": "C_TECH"})),
])
async def triage(ctx: Context, message: dict) -> str:
    RAN.append(("triage", message.get("text", "")))
    return "triaged"


@workflow(name="archive", triggers=[OnAppEvent(TOPIC)])
async def archive(ctx: Context, message: dict) -> str:
    RAN.append(("archive", message.get("text", "")))
    return "archived"


def message(event_id: str, *, channel: str = "C_TECH", text: str = "hi", **kw: Any):
    return EventRecord(
        event_id=event_id,
        type="slack.message",
        payload={"channel": channel, "text": text},
        **kw,
    )


@pytest.fixture
def wired() -> tuple[Runtime, StoreBackedEventLog, EventDispatcher]:
    store = MemoryStore()
    runtime = Runtime(store=store)
    log = StoreBackedEventLog(store)
    dispatcher = EventDispatcher(
        runtime, log=log, checkpoints=StoreBackedCheckpoints(store)
    )
    return runtime, log, dispatcher


async def settle() -> None:
    """Let submitted runs actually execute. `submit` returns a run id, not a
    finished run."""
    for _ in range(50):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Fan-out — the phase's headline claim
# ---------------------------------------------------------------------------


class TestFanOut:
    async def test_two_workflows_see_one_event_and_do_different_things(
        self, wired
    ) -> None:
        _, log, dispatcher = wired
        await dispatcher.register(triage)
        await dispatcher.register(archive)

        await log.append(TOPIC, [
            message("e1", channel="C_TECH", text="in tech"),
            message("e2", channel="C_RANDOM", text="elsewhere"),
        ])
        await dispatcher.poll_once()
        await settle()

        assert sorted(RAN) == [
            ("archive", "elsewhere"),
            ("archive", "in tech"),
            ("triage", "in tech"),
        ], "each subscriber must apply its own filter, not a shared one"

    async def test_each_subscriber_holds_its_own_cursor(self, wired) -> None:
        runtime, log, dispatcher = wired
        marks = StoreBackedCheckpoints(runtime.store)
        await dispatcher.register(triage)
        await dispatcher.register(archive)

        await log.append(TOPIC, [message(f"e{i}") for i in range(3)])
        await dispatcher.poll_once()

        assert await marks.load("triage", TOPIC) == await marks.load("archive", TOPIC)

        # Rewind one of them; the other must be untouched.
        await marks.commit("triage", TOPIC, "1")
        assert await marks.load("archive", TOPIC) == "3"

    async def test_a_slow_subscriber_does_not_hold_the_other_back(
        self, wired
    ) -> None:
        """The property a bus cannot offer: one reader behind, one caught up."""
        runtime, log, dispatcher = wired
        marks = StoreBackedCheckpoints(runtime.store)
        await dispatcher.register(triage)
        await dispatcher.register(archive)

        await log.append(TOPIC, [message(f"e{i}") for i in range(5)])
        # `triage` has fallen behind; `archive` is current.
        await marks.commit("triage", TOPIC, "1")
        await marks.commit("archive", TOPIC, "5")

        reports = {r.subscriber: r for r in await dispatcher.poll_once()}

        assert reports["triage"].read == 4, "the slow subscriber must catch up"
        assert reports["archive"].read == 0, "the caught-up one must read nothing"

    async def test_a_subscriber_added_later_starts_at_latest(self, wired) -> None:
        """Not at the beginning: joining a topic must not replay its backlog."""
        _, log, dispatcher = wired
        await log.append(TOPIC, [message(f"old{i}") for i in range(3)])

        await dispatcher.register(archive)
        await dispatcher.poll_once()
        await settle()

        assert RAN == [], "a new subscriber must not process the existing backlog"

        await log.append(TOPIC, [message("new1", text="fresh")])
        await dispatcher.poll_once()
        await settle()

        assert RAN == [("archive", "fresh")]

    async def test_latest_is_pinned_at_subscribe_not_at_first_poll(
        self, wired
    ) -> None:
        """Events arriving between subscribing and polling must not be skipped.

        Resolving LATEST lazily at the first read looks equivalent and silently
        drops everything that arrived in the gap — which, for a webhook ingress
        appending as fast as it receives, is most of the first second.
        """
        _, log, dispatcher = wired
        await dispatcher.register(archive)

        await log.append(TOPIC, [message("arrived-after-subscribe", text="caught")])
        await dispatcher.poll_once()
        await settle()

        assert RAN == [("archive", "caught")]


# ---------------------------------------------------------------------------
# The commit ordering
# ---------------------------------------------------------------------------


class TestCommitOrdering:
    async def test_the_checkpoint_advances_only_after_dispatch(
        self, wired
    ) -> None:
        runtime, log, dispatcher = wired
        marks = StoreBackedCheckpoints(runtime.store)
        seen: list[str | None] = []

        original = runtime.submit

        async def watching(*args: Any, **kw: Any) -> str:
            # What the checkpoint says *while* the dispatch is happening.
            seen.append(await marks.load("archive", TOPIC))
            return await original(*args, **kw)

        runtime.submit = watching  # type: ignore[method-assign]
        await dispatcher.register(archive)
        await log.append(TOPIC, [message("e1"), message("e2")])

        await dispatcher.poll_once()

        assert seen == [None, None], (
            "the checkpoint must not move until the whole batch is dispatched; "
            f"saw {seen}"
        )
        assert await marks.load("archive", TOPIC) == "2"

    async def test_a_failed_dispatch_leaves_the_checkpoint_where_it_was(
        self, wired
    ) -> None:
        """Committing past an event that never ran is permanent loss: no
        provider will send it again."""
        runtime, log, dispatcher = wired
        marks = StoreBackedCheckpoints(runtime.store)
        await dispatcher.register(archive)
        await log.append(TOPIC, [message("e1")])

        async def broken(*args: Any, **kw: Any) -> str:
            raise OSError("store is down")

        runtime.submit = broken  # type: ignore[method-assign]
        await dispatcher.poll_once()

        assert await marks.load("archive", TOPIC) is None, (
            "a transient failure must not advance the checkpoint"
        )

    async def test_it_retries_from_the_same_position(self, wired) -> None:
        runtime, log, dispatcher = wired
        await dispatcher.register(archive)
        await log.append(TOPIC, [message("e1", text="eventually")])

        original = runtime.submit
        calls = {"n": 0}

        async def flaky(*args: Any, **kw: Any) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient")
            return await original(*args, **kw)

        runtime.submit = flaky  # type: ignore[method-assign]

        assert (await dispatcher.poll_once())[0].read == 1
        await dispatcher.poll_once()
        await settle()

        assert RAN == [("archive", "eventually")], (
            "the deferred event must be picked up on the next pass"
        )

    async def test_a_batch_stops_at_the_first_deferral(self, wired) -> None:
        """Skipping ahead past a failure and committing would lose it."""
        runtime, log, dispatcher = wired
        marks = StoreBackedCheckpoints(runtime.store)
        await dispatcher.register(archive)
        await log.append(TOPIC, [message("e1"), message("e2"), message("e3")])

        original = runtime.submit
        seen: list[str] = []

        async def fail_on_second(target: Any, payload: Any, **kw: Any) -> str:
            seen.append(payload["text"])
            if len(seen) == 2:
                raise OSError("transient")
            return await original(target, payload, **kw)

        runtime.submit = fail_on_second  # type: ignore[method-assign]
        await dispatcher.poll_once()

        assert await marks.load("archive", TOPIC) == "1", (
            "the checkpoint must sit just before the event that failed"
        )

    async def test_the_report_counts_what_was_left_behind(self, wired) -> None:
        """Including the event that failed — it has not been handled either."""
        runtime, log, dispatcher = wired
        await dispatcher.register(archive)
        await log.append(TOPIC, [message(f"e{i}") for i in range(4)])

        original = runtime.submit
        seen: list[str] = []

        async def fail_on_second(target: Any, payload: Any, **kw: Any) -> str:
            seen.append(payload["text"])
            if len(seen) == 2:
                raise OSError("transient")
            return await original(target, payload, **kw)

        runtime.submit = fail_on_second  # type: ignore[method-assign]
        report = (await dispatcher.poll_once())[0]

        assert report.read == 4
        assert report.started and len(report.started) == 1
        assert report.deferred == 3, (
            "events 2, 3 and 4 are all still to do; a count that omits the "
            "failing one under-reports the backlog by exactly the stall"
        )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    async def test_a_redelivered_event_starts_no_second_run(self, wired) -> None:
        _, log, dispatcher = wired
        await dispatcher.register(archive)

        await log.append(TOPIC, [message("e1", text="once")])
        await dispatcher.poll_once()
        await settle()

        # The provider redelivers: same event_id, so the log dedupes on append
        # and there is nothing new to read.
        await log.append(TOPIC, [message("e1", text="once")])
        second = await dispatcher.poll_once()
        await settle()

        assert second[0].read == 0
        assert RAN == [("archive", "once")]

    async def test_a_replayed_position_reuses_the_existing_run(
        self, wired
    ) -> None:
        """Rewinding a checkpoint must be safe — that is what makes a replay a
        supported operation rather than a duplicate storm."""
        runtime, log, dispatcher = wired
        marks = StoreBackedCheckpoints(runtime.store)
        await dispatcher.register(archive)

        await log.append(TOPIC, [message("e1"), message("e2")])
        first = await dispatcher.poll_once()
        await settle()
        started = list(first[0].started)

        await marks.commit("archive", TOPIC, "0")
        again = await dispatcher.poll_once()
        await settle()

        assert again[0].started == started, (
            "re-reading the same events must resolve to the same runs"
        )
        assert len(RAN) == 2, f"a replay duplicated work: {RAN}"

    async def test_two_subscribers_each_get_their_own_run(self, wired) -> None:
        """The dispatch key includes the subscriber. Without that, the second
        subscriber's run deduplicates away against the first's."""
        _, log, dispatcher = wired
        await dispatcher.register(triage)
        await dispatcher.register(archive)

        await log.append(TOPIC, [message("e1", text="both")])
        reports = {r.subscriber: r for r in await dispatcher.poll_once()}
        await settle()

        assert len(reports["triage"].started) == 1
        assert len(reports["archive"].started) == 1
        assert reports["triage"].started != reports["archive"].started
        assert sorted(RAN) == [("archive", "both"), ("triage", "both")]


# ---------------------------------------------------------------------------
# Poison, loops, and isolation
# ---------------------------------------------------------------------------


class TestPoisonAndLoops:
    async def test_an_unprocessable_event_is_dead_lettered_and_stepped_over(
        self, wired
    ) -> None:
        """Retrying forever stalls the subscriber behind one bad event;
        skipping silently loses it. Bounded attempts, then a real topic."""
        runtime, log, dispatcher = wired
        marks = StoreBackedCheckpoints(runtime.store)
        await dispatcher.subscribe(
            Subscription("s", TOPIC, "archive", max_attempts=2)
        )
        runtime.register(archive)
        await log.append(TOPIC, [message("bad"), message("good", text="after")])

        original = runtime.submit

        async def fail_the_first(target: Any, payload: Any, **kw: Any) -> str:
            if payload["text"] == "hi":
                raise OSError("cannot handle this one")
            return await original(target, payload, **kw)

        runtime.submit = fail_the_first  # type: ignore[method-assign]

        await dispatcher.poll_once()   # attempt 1 -> defer
        await dispatcher.poll_once()   # attempt 2 -> dead-letter, step over
        await settle()

        assert await marks.load("s", TOPIC) == "2", "it must get past the bad event"
        assert RAN == [("archive", "after")], "the following event must still run"

        dead = await log.read(f"{TOPIC}.dead", after=None, limit=10)
        assert len(dead) == 1, "the bad event must be inspectable, not vanished"
        assert dead[0].payload["error"].startswith("OSError")

    async def test_a_missing_workflow_is_permanent_not_retried(
        self, wired
    ) -> None:
        runtime, log, dispatcher = wired
        marks = StoreBackedCheckpoints(runtime.store)
        await dispatcher.subscribe(Subscription("s", TOPIC, "does-not-exist"))
        await log.append(TOPIC, [message("e1")])

        await dispatcher.poll_once()

        assert await marks.load("s", TOPIC) == "1", (
            "an event for a workflow that does not exist will fail identically "
            "forever; it must not stall the subscriber"
        )
        assert len(await log.read(f"{TOPIC}.dead", after=None, limit=10)) == 1

    async def test_the_chain_depth_cap_stops_a_loop(self, wired) -> None:
        """A workflow publishing an event that re-triggers it."""
        _, log, dispatcher = wired
        await dispatcher.register(archive)

        await log.append(TOPIC, [
            message("deep", chain_depth=CHAIN_DEPTH_CAP),
            message("fine", text="ok", chain_depth=CHAIN_DEPTH_CAP - 1),
        ])
        await dispatcher.poll_once()
        await settle()

        assert RAN == [("archive", "ok")], "the capped event must not start a run"

    async def test_one_broken_subscription_does_not_stop_the_others(
        self, wired
    ) -> None:
        """Same isolation a scheduler tick gives one failing trigger."""
        _, log, dispatcher = wired
        await dispatcher.register(archive)
        await dispatcher.subscribe(Subscription("wedged", TOPIC, "archive"))

        original = dispatcher.drain

        async def wedge(subscription: Subscription) -> Any:
            if subscription.subscriber == "wedged":
                raise OSError("this subscription is broken outright")
            return await original(subscription)

        dispatcher.drain = wedge  # type: ignore[method-assign]
        await log.append(TOPIC, [message("e1", text="still runs")])
        reports = {r.subscriber: r for r in await dispatcher.poll_once()}
        await settle()

        assert ("archive", "still runs") in RAN
        assert reports["wedged"].read == 0, (
            "the broken one reports nothing done, and its checkpoint stays put"
        )


# ---------------------------------------------------------------------------
# Declaration rules
# ---------------------------------------------------------------------------


class TestDeclarationRules:
    def test_earliest_is_not_declarable(self) -> None:
        """Its blast radius depends on data the author cannot see."""
        spec = OnAppEvent(TOPIC, start_at="earliest")

        with pytest.raises(ValueError) as exc:
            spec.subscription_for("some-workflow")

        assert "not declarable" in str(exc.value)
        assert "loom events replay" in str(exc.value), (
            "the error must name the supported way to do this"
        )

    def test_earliest_is_reachable_programmatically(self) -> None:
        """That is how a replay is driven; only the *declaration* is refused."""
        Subscription("s", TOPIC, "w", start_at=StartAt.EARLIEST)

    async def test_earliest_reads_the_retained_backlog(self, wired) -> None:
        runtime, log, dispatcher = wired
        runtime.register(archive)
        await log.append(TOPIC, [message(f"e{i}", text=f"old{i}") for i in range(3)])

        await dispatcher.subscribe(
            Subscription("backfill", TOPIC, "archive", start_at=StartAt.EARLIEST)
        )
        report = (await dispatcher.poll_once())[0]

        assert report.read == 3, "EARLIEST must see what is retained"

    def test_the_subscription_defaults_to_the_workflow_name(self) -> None:
        assert OnAppEvent(TOPIC).subscription_for("triage").subscriber == "triage"

    def test_an_explicit_subscription_name_wins(self) -> None:
        spec = OnAppEvent(TOPIC, subscription="support")
        assert spec.subscription_for("triage").subscriber == "support"

    def test_identity_does_not_change_when_the_filter_does(self) -> None:
        """The decision the whole subscriber-identity design rests on.

        Hashing the filter in would make every filter edit a new subscriber,
        and every historical event would re-fire against a key that no longer
        deduplicates.
        """
        narrow = OnAppEvent(TOPIC, where=FilterSpec(conditions={"channel": "A"}))
        wide = OnAppEvent(
            TOPIC, where=FilterSpec(conditions={"channel": {"$in": ["A", "B"]}})
        )

        assert (
            narrow.subscription_for("triage").subscriber
            == wide.subscription_for("triage").subscriber
        )

    async def test_two_subscriptions_sharing_a_name_are_refused(
        self, wired
    ) -> None:
        """They would share a checkpoint and consume each other's backlog."""
        _, _log, dispatcher = wired
        await dispatcher.subscribe(
            Subscription("shared", TOPIC, "archive", filter=None)
        )

        with pytest.raises(ConfigurationError, match="shared"):
            await dispatcher.subscribe(
                Subscription(
                    "shared", TOPIC, "triage",
                    filter=FilterSpec(conditions={"channel": "X"}),
                )
            )

    async def test_registering_the_same_subscription_twice_is_a_no_op(
        self, wired
    ) -> None:
        _, _log, dispatcher = wired
        await dispatcher.register(archive)
        await dispatcher.register(archive)

        assert len(dispatcher.subscriptions) == 1

    def test_a_dispatcher_without_a_log_says_what_to_pass(self) -> None:
        runtime = Runtime(store=MemoryStore())

        with pytest.raises(ConfigurationError) as exc:
            EventDispatcher(runtime)

        assert "StoreBackedEventLog" in str(exc.value)

    def test_the_runtime_seam_the_error_names_actually_exists(self) -> None:
        """An error that suggests a keyword the constructor rejects sends the
        reader somewhere that does not work."""
        store = MemoryStore()
        runtime = Runtime(store=store, events=StoreBackedEventLog(store))

        dispatcher = EventDispatcher(runtime)

        assert dispatcher._log is runtime.events

    def test_checkpoints_default_to_the_store_when_only_a_log_is_given(
        self,
    ) -> None:
        """A host may keep the log in Kafka and the cursors in its own database,
        so they are separate seams — but one alone must still work."""
        store = MemoryStore()
        runtime = Runtime(store=store, events=StoreBackedEventLog(store))

        assert isinstance(EventDispatcher(runtime)._marks, StoreBackedCheckpoints)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_it_dispatches_from_the_background_loop(self, wired) -> None:
        _, log, dispatcher = wired
        await dispatcher.register(archive)
        await dispatcher.start()
        try:
            await log.append(TOPIC, [message("e1", text="background")])
            for _ in range(100):
                await asyncio.sleep(0)
                if RAN:
                    break
            await settle()
            assert RAN == [("archive", "background")]
        finally:
            await dispatcher.stop()

    async def test_runtime_shutdown_stops_it(self, wired) -> None:
        runtime, _, dispatcher = wired
        await dispatcher.register(archive)
        await dispatcher.start()

        assert dispatcher._task is not None
        await runtime.shutdown(drain=0)
        assert dispatcher._task is None, (
            "it must register with supervise() so a host need not know it exists"
        )

    async def test_stop_is_safe_before_start_and_twice(self, wired) -> None:
        _, _, dispatcher = wired
        await dispatcher.stop()
        await dispatcher.start()
        await dispatcher.stop()
        await dispatcher.stop()


class TestWaitingRuns:
    async def test_an_event_also_resumes_a_parked_run(self, wired) -> None:
        """A trigger starts a run; a wait continues one. One event can do both,
        and without this `ctx.wait_for_event` parks on something nothing
        delivers."""
        runtime, log, dispatcher = wired

        @workflow(name="waiter")
        async def waiter(ctx: Context, _in: Any = None) -> str:
            payload = await ctx.wait_for_event("slack.message")
            RAN.append(("waiter", payload.get("text", "")))
            return "resumed"

        runtime.register(waiter)
        await dispatcher.subscribe(Subscription("s", TOPIC, "archive"))
        runtime.register(archive)

        parked = await runtime.run(waiter, None)
        assert parked.status.value == "suspended"

        await log.append(TOPIC, [message("e1", text="wakes it")])
        await dispatcher.poll_once()
        await settle()

        assert ("waiter", "wakes it") in RAN
