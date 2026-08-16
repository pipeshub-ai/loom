"""Shape B: the payload is a position, not the event.

Gmail is why the reconciler exists. The claim being tested is the one in §2.3 of
the design — that a pointer provider converges with a push-data one **if the
internal representation is a log with positions** — and the way to test it is
that a downstream subscriber reading ``app.gmail.message`` cannot tell.

The rest is the silent-failure class, which is what makes this shape dangerous
rather than merely awkward: a watch that lapses, a cursor the provider has
forgotten, and a notification stream that reorders. None of the three raises on
its own, and all three are indistinguishable from a quiet week.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from loom import Runtime
from loom.events import (
    CursorExpired,
    EventRecord,
    Expansion,
    Heartbeat,
    PointerReconciler,
    Reconciler,
    SourceState,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
    VerificationFailed,
    Watch,
    WatchRegistration,
    WatchRenewer,
    WebhookIngress,
    lifetime_hint,
)
from loom.stores.memory import MemoryStore
from loom.toolsets.google.gmail.source import (
    GmailReconciler,
    GmailSource,
    GmailWatcher,
    decode_push,
)

POINTER_TOPIC = "app.gmail.push"


def push_body(history_id: str = "1000", *, mailbox: str = "a@b.com") -> bytes:
    """A Pub/Sub push envelope, as Google actually sends it."""
    inner = json.dumps({"emailAddress": mailbox, "historyId": int(history_id)})
    return json.dumps({
        "message": {
            "data": base64.b64encode(inner.encode()).decode(),
            "messageId": f"psm-{history_id}",
            "publishTime": "2026-01-01T00:00:00Z",
        },
        "subscription": "projects/p/subscriptions/s",
    }).encode()


@pytest.fixture
def wired():
    store = MemoryStore()
    runtime = Runtime(store=store, events=StoreBackedEventLog(store))
    runtime.sources.register(GmailSource(require_token=False))
    ingress = WebhookIngress(runtime)
    return runtime, runtime.events, ingress


# ---------------------------------------------------------------------------
# The pointer, and what it is not
# ---------------------------------------------------------------------------


class TestPointerDecoding:
    def test_the_notification_is_base64_inside_a_pubsub_envelope(self) -> None:
        """A handler reading `payload["historyId"]` finds nothing and reports an
        empty mailbox — the failure this decode exists to prevent."""
        decoded = decode_push(json.loads(push_body("9876543")))

        assert decoded["historyId"] == "9876543"
        assert decoded["emailAddress"] == "a@b.com"

    def test_the_history_id_is_carried_as_a_string(self) -> None:
        """Gmail sends it as a number here and as a string everywhere else, and
        a cursor compared across the two is never equal."""
        decoded = decode_push(json.loads(push_body("42")))

        assert isinstance(decoded["historyId"], str)

    def test_a_bare_notification_with_no_envelope_is_malformed(self) -> None:
        from loom.events import MalformedDelivery

        with pytest.raises(MalformedDelivery, match="envelope"):
            decode_push({"emailAddress": "a@b.com", "historyId": 1})

    def test_a_notification_with_no_history_id_is_malformed(self) -> None:
        from loom.events import MalformedDelivery

        data = base64.b64encode(json.dumps({"emailAddress": "a@b"}).encode()).decode()

        with pytest.raises(MalformedDelivery, match="historyId"):
            decode_push({"message": {"data": data}})

    async def test_a_push_becomes_a_pointer_event_not_a_message(
        self, wired
    ) -> None:
        """A workflow subscribing to `gmail.message` and receiving a history id
        would have a sender-less email; the topics are deliberately different."""
        _, log, ingress = wired

        result = await ingress.receive("gmail", {}, push_body())

        assert result.topics == [POINTER_TOPIC]
        assert await log.head("app.gmail.message") is None

    async def test_nothing_is_fetched_during_ingress(self, wired) -> None:
        """A history read inside the three-second Pub/Sub budget makes a slow
        mailbox produce duplicate pushes as well as a slow response."""
        _, log, ingress = wired

        await ingress.receive("gmail", {}, push_body())

        stored = await log.read(POINTER_TOPIC, after=None, limit=10)
        assert set(stored[0].payload) == {
            "emailAddress", "historyId", "publishTime", "subscription"
        }

    async def test_the_ordering_key_is_the_mailbox(self, wired) -> None:
        """Two pointers for one inbox must reconcile in order, or the later
        history id is consumed first and the earlier read returns nothing."""
        _, log, ingress = wired

        await ingress.receive("gmail", {}, push_body())

        stored = await log.read(POINTER_TOPIC, after=None, limit=10)
        assert stored[0].record.key == "a@b.com"

    def test_the_delivery_id_is_the_pubsub_message_id(self) -> None:
        """Not the historyId: Pub/Sub reorders, so two different pushes can
        carry overlapping history ids and keying on one drops a real one."""
        source = GmailSource(require_token=False)
        payload = json.loads(push_body("1000"))

        assert source.delivery_id({}, payload) == "psm-1000"

    async def test_a_pubsub_redelivery_appends_once(self, wired) -> None:
        _, log, ingress = wired

        await ingress.receive("gmail", {}, push_body("1000"))
        await ingress.receive("gmail", {}, push_body("1000"))

        assert len(await log.read(POINTER_TOPIC, after=None, limit=10)) == 1


class TestGmailVerification:
    def test_no_audience_refuses_rather_than_accepting_anything(self) -> None:
        """An endpoint that appends to your event log on any POST is not a
        thing to arrive at by omission."""
        with pytest.raises(VerificationFailed, match="GMAIL_PUSH_AUDIENCE"):
            GmailSource(audience="").verify({}, push_body())

    def test_a_missing_bearer_token_is_refused(self) -> None:
        with pytest.raises(VerificationFailed, match="bearer token"):
            GmailSource(audience="https://example.com/hooks/gmail").verify(
                {}, push_body()
            )

    def test_skipping_verification_takes_an_explicit_opt_in(self) -> None:
        GmailSource(require_token=False).verify({}, push_body())

    def test_an_unverifiable_token_is_never_decoded_unverified(self) -> None:
        """Reading claims without checking the signature accepts a token anyone
        can mint, which is worse than no verification because it looks like
        verification."""
        source = GmailSource(audience="aud")
        forged = ".".join([
            base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("="),
            base64.urlsafe_b64encode(b'{"aud":"aud","email":"x"}').decode().rstrip("="),
            "",
        ])

        with pytest.raises(VerificationFailed):
            source.verify({"authorization": f"Bearer {forged}"}, push_body())


# ---------------------------------------------------------------------------
# The reconciler — a subscriber, not a special case
# ---------------------------------------------------------------------------


class FakeReconciler:
    """A provider that reports what changed between two positions."""

    id = "fake"

    def __init__(self, *, expire_below: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self._expire_below = expire_below

    async def expand(self, pointer: dict[str, Any], cursor: str) -> Expansion:
        position = str(pointer.get("historyId", ""))
        self.calls.append((cursor, position))
        if not cursor:
            return Expansion(cursor=position)
        if self._expire_below and cursor < self._expire_below:
            raise CursorExpired("history no longer held", cursor=cursor)
        from loom.events import InboundEvent

        return Expansion(
            events=[
                InboundEvent(
                    type="fake.message",
                    payload={"id": f"m{n}"},
                    dedupe_suffix=f"m{n}",
                )
                for n in range(int(cursor), int(position))
            ],
            cursor=position,
        )


def reconciler_over(runtime: Runtime, provider: Any, **kw: Any) -> PointerReconciler:
    return PointerReconciler(
        provider,
        log=runtime.events,
        checkpoints=StoreBackedCheckpoints(runtime.store),
        state=SourceState(runtime.store, provider.id),
        **kw,
    )


@pytest.fixture
def pointing():
    store = MemoryStore()
    runtime = Runtime(store=store, events=StoreBackedEventLog(store))
    return runtime, runtime.events


async def send(log: Any, history_id: str, *, source: str = "fake") -> None:
    await log.append(f"app.{source}.push", [
        EventRecord(
            event_id=f"app.{source}.push/{source}:psm-{history_id}",
            type=f"{source}.push",
            payload={"historyId": history_id, "emailAddress": "a@b.com"},
            key="a@b.com",
            source=source,
        )
    ])


class TestReconciler:
    async def test_a_pointer_becomes_data_events(self, pointing) -> None:
        runtime, log = pointing
        driver = reconciler_over(runtime, FakeReconciler())

        await send(log, "1")
        await driver.drain()          # first sight: adopt, emit nothing
        await send(log, "4")
        appended = await driver.drain()

        assert appended == 3
        rows = await log.read("app.fake.message", after=None, limit=10)
        assert [r.payload["id"] for r in rows] == ["m1", "m2", "m3"]

    async def test_the_first_pointer_adopts_rather_than_backfills(
        self, pointing
    ) -> None:
        """A reconciler that back-fills on first sight replays a mailbox into a
        workflow that replies — and the dispatch key does not protect against
        it, because every one of those messages is genuinely new."""
        runtime, log = pointing
        provider = FakeReconciler()
        driver = reconciler_over(runtime, provider)

        await send(log, "500")
        appended = await driver.drain()

        assert appended == 0
        assert provider.calls == [("", "500")]

    async def test_the_provider_cursor_is_not_the_checkpoint(
        self, pointing
    ) -> None:
        """Conflating them means a replay of our log re-reads the provider, and
        a provider outage rewinds our log."""
        runtime, log = pointing
        marks = StoreBackedCheckpoints(runtime.store)
        state = SourceState(runtime.store, "fake")
        driver = reconciler_over(runtime, FakeReconciler())

        await send(log, "1")
        await driver.drain()
        await send(log, "3")
        await driver.drain()

        assert await state.get("cursor") == "3"
        assert await marks.load("fake-reconciler", "app.fake.push") == "2"

    async def test_it_resumes_where_it_stopped(self, pointing) -> None:
        runtime, log = pointing
        provider = FakeReconciler()

        await send(log, "1")
        await reconciler_over(runtime, provider).drain()
        await send(log, "3")
        # A different instance entirely — a restart, not a variable.
        await reconciler_over(runtime, FakeReconciler()).drain()
        await send(log, "5")
        await reconciler_over(runtime, FakeReconciler()).drain()

        rows = await log.read("app.fake.message", after=None, limit=20)
        assert [r.payload["id"] for r in rows] == ["m1", "m2", "m3", "m4"]

    async def test_an_overlapping_read_appends_nothing_twice(
        self, pointing
    ) -> None:
        """What makes an out-of-order pointer harmless: the data events dedupe
        on the *message* id, so two overlapping history reads collapse."""
        runtime, log = pointing
        marks = StoreBackedCheckpoints(runtime.store)
        driver = reconciler_over(runtime, FakeReconciler())

        await send(log, "1")
        await driver.drain()
        await send(log, "4")
        await driver.drain()

        # Rewind our checkpoint *and* the provider cursor: a full replay.
        await marks.commit("fake-reconciler", "app.fake.push", "1")
        await SourceState(runtime.store, "fake").set("cursor", "1")
        await driver.drain()

        rows = await log.read("app.fake.message", after=None, limit=20)
        assert len(rows) == 3, f"a replay duplicated events: {rows}"

    async def test_the_cursor_advances_only_after_the_append(
        self, pointing
    ) -> None:
        runtime, log = pointing
        state = SourceState(runtime.store, "fake")
        seen: list[Any] = []
        driver = reconciler_over(runtime, FakeReconciler())
        original = log.append

        async def watching(topic: str, records: Any) -> Any:
            if topic == "app.fake.message":
                seen.append(await state.get("cursor"))
            return await original(topic, records)

        await send(log, "1")
        await driver.drain()
        log.append = watching  # type: ignore[method-assign]
        await send(log, "3")
        await driver.drain()

        assert seen == ["1"], (
            f"the cursor must still be the old one while appending; saw {seen}"
        )
        assert await state.get("cursor") == "3"

    async def test_a_failed_expansion_leaves_the_checkpoint_where_it_was(
        self, pointing
    ) -> None:
        runtime, log = pointing
        marks = StoreBackedCheckpoints(runtime.store)

        class Broken(FakeReconciler):
            async def expand(self, pointer: Any, cursor: str) -> Expansion:
                raise OSError("provider is down")

        await send(log, "1")
        await reconciler_over(runtime, Broken()).drain()

        assert await marks.load("fake-reconciler", "app.fake.push") is None

    def test_it_satisfies_the_protocol_structurally(self) -> None:
        assert isinstance(FakeReconciler(), Reconciler)
        assert isinstance(GmailReconciler(), Reconciler)


# ---------------------------------------------------------------------------
# The gap — where the silent failure is made loud
# ---------------------------------------------------------------------------


class TestGap:
    async def test_an_expired_cursor_appends_a_gap_event(self, pointing) -> None:
        """Silently jumping to now is the failure where 'no email arrived
        today' and 'we lost a day of email' are indistinguishable."""
        runtime, log = pointing
        driver = reconciler_over(runtime, FakeReconciler(expire_below="100"))

        await send(log, "1")
        await driver.drain()
        await send(log, "200")
        await driver.drain()

        gaps = await log.read("app.fake.gap", after=None, limit=10)
        assert len(gaps) == 1
        assert gaps[0].payload["expired_cursor"] == "1"
        assert gaps[0].payload["resumed_at"] == "200"

    async def test_a_gap_is_an_event_not_an_exception(self, pointing) -> None:
        """Which falls straight out of having a log: a workflow can subscribe to
        'we lost visibility on this mailbox' and re-scan or page."""
        runtime, log = pointing
        driver = reconciler_over(runtime, FakeReconciler(expire_below="100"))

        await send(log, "1")
        await driver.drain()
        await send(log, "200")
        appended = await driver.drain()  # must not raise

        assert appended == 0

    async def test_it_resets_forward_so_reading_resumes(self, pointing) -> None:
        """Without the reset every later pass raises the same expiry, so
        nothing is ever read again."""
        runtime, log = pointing
        provider = FakeReconciler(expire_below="100")
        driver = reconciler_over(runtime, provider)

        await send(log, "1")
        await driver.drain()
        await send(log, "200")
        await driver.drain()
        await send(log, "203")
        appended = await driver.drain()

        assert appended == 3, "reading must resume after a gap"
        assert await SourceState(runtime.store, "fake").get("cursor") == "203"

    async def test_the_checkpoint_advances_past_a_gapped_pointer(
        self, pointing
    ) -> None:
        """An expiry is not a retryable failure — asking again produces the same
        404 forever, so leaving the pointer unconsumed stalls the reconciler."""
        runtime, log = pointing
        marks = StoreBackedCheckpoints(runtime.store)
        driver = reconciler_over(runtime, FakeReconciler(expire_below="100"))

        await send(log, "1")
        await driver.drain()
        await send(log, "200")
        await driver.drain()

        assert await marks.load("fake-reconciler", "app.fake.push") == "2"

    async def test_the_gap_carries_what_was_lost_and_where_it_resumed(
        self, pointing
    ) -> None:
        runtime, log = pointing
        driver = reconciler_over(runtime, FakeReconciler(expire_below="100"))

        await send(log, "1")
        await driver.drain()
        await send(log, "200")
        await driver.drain()

        gap = (await log.read("app.fake.gap", after=None, limit=10))[0]
        assert "no longer held" in gap.payload["reason"]
        assert gap.payload["pointer"]["historyId"] == "200"
        assert gap.record.key == "a@b.com", "a gap belongs to its mailbox"


# ---------------------------------------------------------------------------
# The watch — the highest-severity silent failure
# ---------------------------------------------------------------------------


class FakeWatch:
    id = "fake"

    def __init__(self, *, lifetime: float = 7 * 24 * 3600, fail: bool = False) -> None:
        self.registrations: list[str] = []
        self.stopped: list[str] = []
        self._lifetime = lifetime
        self.fail = fail
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    async def register(self, resource: str) -> WatchRegistration:
        if self.fail:
            raise OSError("provider refused")
        self.registrations.append(resource)
        return WatchRegistration(
            resource=resource,
            expires_at=self.now + timedelta(seconds=self._lifetime),
            cursor="1000",
            metadata=lifetime_hint(self._lifetime),
        )

    async def stop(self, resource: str) -> None:
        self.stopped.append(resource)


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, **kw: Any) -> None:
        self._at += timedelta(**kw)

    async def sleep(self, seconds: float) -> None:
        raise AssertionError("the loop must not be driven in these tests")


class TestWatchRenewal:
    async def test_it_registers_on_the_first_sweep(self) -> None:
        """Which is what 'on restart' means: a process down for a day comes back
        to watches that expired while it was gone."""
        watch = FakeWatch()
        renewer = WatchRenewer(
            watch, clock=FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        )
        renewer.track("a@b.com")

        assert await renewer.sweep() == ["a@b.com"]

    async def test_a_live_watch_is_not_renewed_again(self) -> None:
        watch = FakeWatch()
        renewer = WatchRenewer(
            watch, clock=FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        )
        renewer.track("a@b.com")

        await renewer.sweep()
        assert await renewer.sweep() == []
        assert watch.registrations == ["a@b.com"]

    async def test_it_renews_at_a_fraction_of_the_lifetime_not_at_expiry(
        self,
    ) -> None:
        """A margin fixed in seconds under-protects a short-lived subscription;
        renewing at half the lifetime means several failures are survivable."""
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        watch = FakeWatch()
        renewer = WatchRenewer(watch, clock=clock)
        renewer.track("a@b.com")
        await renewer.sweep()

        clock.advance(days=3)
        assert await renewer.sweep() == [], "three days into seven is not due"
        clock.advance(days=1)
        assert await renewer.sweep() == ["a@b.com"], "past half the lifetime is"

    async def test_a_short_lived_watch_is_renewed_sooner(self) -> None:
        """Graph lasts about three days. Assuming a week would renew it at the
        three-and-a-half-day mark — which is to say, after it died."""
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        watch = FakeWatch(lifetime=3 * 24 * 3600)
        renewer = WatchRenewer(watch, clock=clock)
        renewer.track("res")
        await renewer.sweep()

        clock.advance(days=2)
        assert await renewer.sweep() == ["res"]

    async def test_an_unknown_expiry_is_treated_as_dying(self) -> None:
        """An unnecessary renewal costs one API call; a missed one costs a
        silent week."""
        assert WatchRegistration("r", expires_at=None).due(
            now=datetime(2026, 1, 1, tzinfo=UTC)
        )

    async def test_one_failing_resource_does_not_stop_the_others(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))

        class Picky(FakeWatch):
            async def register(self, resource: str) -> WatchRegistration:
                if resource == "bad@b.com":
                    raise OSError("no")
                return await FakeWatch.register(self, resource)

        renewer = WatchRenewer(Picky(), clock=clock)
        renewer.track("bad@b.com")
        renewer.track("good@b.com")

        assert await renewer.sweep() == ["good@b.com"]

    async def test_a_failure_is_recorded_not_only_logged(self) -> None:
        """This is what a status command reads; a failure that exists only in a
        log line is one nobody finds."""
        renewer = WatchRenewer(
            FakeWatch(fail=True), clock=FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        )
        renewer.track("a@b.com")
        await renewer.sweep()
        await renewer.sweep()

        status = renewer.statuses["a@b.com"]
        assert status.consecutive_failures == 2
        assert "OSError" in status.last_error
        assert not status.healthy

    async def test_a_lapsed_watch_appends_an_event(self) -> None:
        """The moment absence of events stops being indistinguishable from
        absence of activity."""
        store = MemoryStore()
        log = StoreBackedEventLog(store)
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        watch = FakeWatch()
        renewer = WatchRenewer(watch, log=log, clock=clock)
        renewer.track("a@b.com")
        await renewer.sweep()

        watch.fail = True
        clock.advance(days=8)          # past the expiry
        await renewer.sweep()

        lapsed = await log.read("app.fake.watch_lapsed", after=None, limit=10)
        assert len(lapsed) == 1
        assert lapsed[0].payload["resource"] == "a@b.com"

    async def test_an_early_renewal_failure_does_not_cry_wolf(self) -> None:
        """The whole point of renewing at a fraction of the lifetime is that
        early failures are survivable; alerting on each trains people to ignore
        the one that matters."""
        store = MemoryStore()
        log = StoreBackedEventLog(store)
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        watch = FakeWatch()
        renewer = WatchRenewer(watch, log=log, clock=clock)
        renewer.track("a@b.com")
        await renewer.sweep()

        watch.fail = True
        clock.advance(days=4)          # due, but not yet expired
        await renewer.sweep()

        assert await log.head("app.fake.watch_lapsed") is None

    async def test_repeated_failures_past_expiry_append_one_event(self) -> None:
        """A renewer retrying hourly against a dead credential must not append
        one event an hour."""
        store = MemoryStore()
        log = StoreBackedEventLog(store)
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        watch = FakeWatch()
        renewer = WatchRenewer(watch, log=log, clock=clock)
        renewer.track("a@b.com")
        await renewer.sweep()

        watch.fail = True
        clock.advance(days=8)
        await renewer.sweep()
        clock.advance(hours=1)
        await renewer.sweep()

        assert len(await log.read("app.fake.watch_lapsed", after=None, limit=10)) == 1

    async def test_stopping_does_not_deafen_the_mailbox(self) -> None:
        """A deployment that tore its subscriptions down on every rolling
        restart would lose every event in the gap."""
        watch = FakeWatch()
        renewer = WatchRenewer(
            watch, clock=FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        )
        renewer.track("a@b.com")
        await renewer.sweep()

        await renewer.stop()

        assert watch.stopped == []

    def test_it_satisfies_the_protocol_structurally(self) -> None:
        assert isinstance(FakeWatch(), Watch)
        assert isinstance(GmailWatcher("projects/p/topics/t"), Watch)


class TestHeartbeat:
    def test_a_topic_past_its_interval_is_reported(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        beat = Heartbeat(clock=clock)
        beat.expect("app.gmail.message", within_seconds=3600)

        clock.advance(hours=2)

        assert "app.gmail.message" in beat.quiet()

    def test_traffic_resets_it(self) -> None:
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        beat = Heartbeat(clock=clock)
        beat.expect("app.gmail.message", within_seconds=3600)

        clock.advance(minutes=50)
        beat.saw("app.gmail.message")
        clock.advance(minutes=50)

        assert beat.quiet() == {}


# ---------------------------------------------------------------------------
# The convergence claim
# ---------------------------------------------------------------------------


class TestDownstreamCannotTell:
    async def test_a_subscriber_reads_gmail_exactly_as_it_reads_slack(
        self, pointing
    ) -> None:
        """The claim of §2.3: four delivery shapes converge if the internal
        representation is a log with positions. A workflow subscribing to
        `app.fake.message` sees data events with no trace of the pointer hop."""
        runtime, log = pointing
        driver = reconciler_over(runtime, FakeReconciler())

        await send(log, "1")
        await driver.drain()
        await send(log, "3")
        await driver.drain()

        rows = await log.read("app.fake.message", after=None, limit=10)
        assert [r.type for r in rows] == ["fake.message", "fake.message"]
        assert all("historyId" not in r.payload for r in rows)


class TestGmailHistoryModel:
    def test_message_ids_are_deduplicated_across_history_records(self) -> None:
        """One message appears added, then labelled, then labelled again — and a
        workflow wants it once."""
        from loom.toolsets.google.gmail.models import GmailHistory

        history = GmailHistory(
            start_history_id="1",
            history_id="9",
            records=[
                {"messagesAdded": [{"message": {"id": "m1"}}]},
                {"labelsAdded": [{"message": {"id": "m1"}}]},
                {"messagesAdded": [{"message": {"id": "m2"}}]},
            ],
        )

        assert history.message_ids == ["m1", "m2"]

    def test_an_unknown_history_type_is_passed_through_not_dropped(self) -> None:
        """Gmail adds history types over time; a model that dropped one would
        silently lose changes."""
        from loom.toolsets.google.gmail.models import GmailHistory

        history = GmailHistory(
            start_history_id="1",
            history_id="2",
            records=[{"somethingNew": [{"message": {"id": "m9"}}]}],
        )

        assert history.records[0]["somethingNew"][0]["message"]["id"] == "m9"

    def test_an_expiry_in_milliseconds_is_read_as_such(self) -> None:
        """Read as seconds it lands in 1970, and it arrives quoted, so
        arithmetic on it silently concatenates."""
        from loom.toolsets.google.gmail.client import _epoch_ms

        assert _epoch_ms("1767225600000") == datetime(2026, 1, 1, tzinfo=UTC)

    def test_a_watch_with_no_expiry_reports_itself_due(self) -> None:
        from loom.toolsets.google.gmail.models import GmailWatch

        assert GmailWatch(history_id="1").expires_within(60)
