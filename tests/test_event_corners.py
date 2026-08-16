"""The corners: provider code paths, extensibility wiring, and lifecycle.

Everything here was reachable and untested after the six phases — found by
coverage rather than by reading, which is the point of running it. Three groups:

**The real Gmail client and reconciler**, as opposed to the fakes the shape-B
tests drive. `list_history` pages by hand and maps a 404 onto an expiry, and
neither is exercised by a `FakeReconciler`.

**The extensibility wiring** — entry points, builtin fallback, `require`. The
claim that a third party adds a provider without touching LOOM is worth exactly
as much as its test.

**Lifecycle and degradation** — background loops, supervision, and what happens
when the thing underneath is broken rather than merely empty.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from loom import Runtime
from loom.core.exceptions import AdmissionRejected, ConfigurationError
from loom.events import (
    CursorExpired,
    EventDispatcher,
    EventRecord,
    EventSourceRegistry,
    Expansion,
    InboundEvent,
    PointerReconciler,
    SourceContext,
    SourceState,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
    Subscription,
    WatchRegistration,
    WatchRenewer,
    WebhookIngress,
    topic_for,
)
from loom.events.source_registry import (
    builtin_source,
    discover_source_entry_points,
)
from loom.stores.memory import MemoryStore
from loom.toolsets.google.auth import GoogleAuth, GoogleCredentials
from loom.toolsets.google.errors import GmailHistoryExpired
from loom.toolsets.google.gmail.client import GmailClient
from loom.toolsets.google.gmail.source import GmailReconciler, GmailWatcher


def token_auth() -> GoogleAuth:
    return GoogleAuth(GoogleCredentials(access_token="test-token"))


# ---------------------------------------------------------------------------
# The Gmail client's push and history endpoints
# ---------------------------------------------------------------------------


class TestGmailWatchEndpoint:
    async def test_it_returns_the_history_id_and_the_expiry(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"historyId": "9876", "expiration": "1767225600000"}
            )

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))
        watch = await client.watch("projects/p/topics/t")

        assert watch.history_id == "9876"
        assert watch.expiration == datetime(2026, 1, 1, tzinfo=UTC)

    async def test_the_expiry_is_milliseconds_and_arrives_quoted(self) -> None:
        """Read as seconds it lands in 1970, and arithmetic on the string
        silently concatenates."""
        from loom.toolsets.google.gmail.client import _epoch_ms

        assert _epoch_ms("1767225600000") == datetime(2026, 1, 1, tzinfo=UTC)
        assert _epoch_ms(None) is None
        assert _epoch_ms("") is None
        assert _epoch_ms("not-a-number") is None

    async def test_label_filtering_is_sent_as_provider_side_filtering(
        self,
    ) -> None:
        """The cheapest of the three filter placements: a rejected event is
        never sent at all."""
        seen: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"historyId": "1"})

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))
        await client.watch("projects/p/topics/t", label_ids=["INBOX"])

        assert seen[0]["labelIds"] == ["INBOX"]
        assert seen[0]["labelFilterBehavior"] == "INCLUDE"

    async def test_no_labels_sends_no_filter_keys(self) -> None:
        """Sending an empty list is not the same as sending nothing — Gmail
        reads it as "match no labels"."""
        seen: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"historyId": "1"})

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))
        await client.watch("projects/p/topics/t")

        assert "labelIds" not in seen[0]

    async def test_stopping_posts_to_stop(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={})

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))
        await client.stop_watch()

        assert seen[0].endswith("/users/me/stop")


class TestGmailHistoryEndpoint:
    async def test_it_returns_the_next_cursor_not_the_one_it_was_given(
        self,
    ) -> None:
        """The value that matters is not in the items: `historyId` sits at the
        top level and is what the caller must persist."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
                    "historyId": "2000",
                },
            )

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))
        history = await client.list_history("1000")

        assert history.history_id == "2000"
        assert history.start_history_id == "1000"
        assert history.message_ids == ["m1"]

    async def test_it_follows_pages_and_keeps_the_last_history_id(self) -> None:
        """`paginate` returns rows and a page token, so the field a caller must
        persist would be dropped — and the next poll would re-read the old id
        forever."""
        pages = [
            {
                "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
                "historyId": "1500",
                "nextPageToken": "p2",
            },
            {
                "history": [{"messagesAdded": [{"message": {"id": "m2"}}]}],
                "historyId": "2000",
            },
        ]
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=pages[len(seen) - 1])

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))
        history = await client.list_history("1000")

        assert history.message_ids == ["m1", "m2"]
        assert history.history_id == "2000"
        assert "pageToken=p2" in seen[1]

    async def test_an_expired_history_id_raises_rather_than_returning_empty(
        self,
    ) -> None:
        """Returning `[]` reads as "nothing happened", which is exactly the case
        where a great deal happened and we cannot say what."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))

        with pytest.raises(GmailHistoryExpired) as exc:
            await client.list_history("1")

        assert "about a week" in str(exc.value)
        assert "cannot be enumerated" in str(exc.value), (
            "the message must say what it means: not that nothing happened, but "
            "that we cannot say what did"
        )

    async def test_another_error_is_not_dressed_up_as_an_expiry(self) -> None:
        """A 403 is a scope problem and has a different fix; reporting it as an
        expiry sends somebody to resync a mailbox that was never behind."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"message": "Forbidden"}})

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))

        with pytest.raises(Exception) as exc:
            await client.list_history("1")

        assert not isinstance(exc.value, GmailHistoryExpired)

    async def test_a_response_with_no_history_key_is_empty_not_an_error(
        self,
    ) -> None:
        """Gmail omits `history` entirely when nothing changed."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"historyId": "1000"})

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))
        history = await client.list_history("1000")

        assert history.records == [] and history.history_id == "1000"


# ---------------------------------------------------------------------------
# The real GmailReconciler
# ---------------------------------------------------------------------------


class FakeGmail:
    """Enough of GmailClient for the reconciler, with retention modelled."""

    def __init__(self, *, oldest_held: int = 0) -> None:
        self.oldest_held = oldest_held
        self.history_calls: list[tuple[str, list[str]]] = []
        self.fetched: list[str] = []
        self.unfetchable: set[str] = set()
        self.records: list[dict[str, Any]] = []
        self.latest = "1000"

    def add(self, message_id: str, *, kind: str = "messagesAdded") -> None:
        self.records.append({kind: [{"message": {"id": message_id}}]})

    async def list_history(
        self, start_history_id: str, *, max_results: int = 100,
        history_types: list[str] | None = None, label_id: str = "",
    ) -> Any:
        from loom.toolsets.google.gmail.models import GmailHistory

        self.history_calls.append((start_history_id, history_types or []))
        if int(start_history_id) < self.oldest_held:
            raise GmailHistoryExpired("gone")
        return GmailHistory(
            start_history_id=start_history_id,
            history_id=self.latest,
            records=self.records,
        )

    async def get_message(self, message_id: str) -> Any:
        self.fetched.append(message_id)
        if message_id in self.unfetchable:
            raise RuntimeError("404 not found")

        class Message:
            def model_dump(self) -> dict[str, Any]:
                return {"id": message_id, "subject": "hi"}

        return Message()


def pointer(history_id: str, *, mailbox: str = "a@b.com") -> dict[str, Any]:
    return {"historyId": history_id, "emailAddress": mailbox}


class TestGmailReconciler:
    async def test_the_first_pointer_adopts_without_backfilling(self) -> None:
        """Back-filling on first sight replays a mailbox into a workflow that
        replies, and the dispatch key does not protect against it."""
        gmail = FakeGmail()
        result = await GmailReconciler(gmail).expand(pointer("500"), "")

        assert result.events == () and result.cursor == "500"
        assert gmail.history_calls == [], "nothing should be asked of Gmail yet"

    async def test_it_hydrates_each_message_by_default(self) -> None:
        gmail = FakeGmail()
        gmail.add("m1")
        gmail.add("m2")

        result = await GmailReconciler(gmail).expand(pointer("2000"), "1000")

        assert [e.payload["subject"] for e in result.events] == ["hi", "hi"]
        assert [e.type for e in result.events] == ["gmail.message", "gmail.message"]

    async def test_the_dedupe_suffix_is_the_gmail_message_id(self) -> None:
        """Which is what makes an out-of-order pointer harmless: two
        overlapping history reads produce identical event ids."""
        gmail = FakeGmail()
        gmail.add("m1")

        result = await GmailReconciler(gmail).expand(pointer("2000"), "1000")

        assert result.events[0].dedupe_suffix == "m1"

    async def test_the_mailbox_is_the_ordering_key(self) -> None:
        gmail = FakeGmail()
        gmail.add("m1")

        result = await GmailReconciler(gmail).expand(
            pointer("2000", mailbox="team@x.com"), "1000"
        )

        assert result.events[0].key == "team@x.com"

    async def test_without_hydration_it_emits_ids_only(self) -> None:
        """One API call per message is what a triage workflow needs and what a
        high-volume mailbox cannot afford."""
        gmail = FakeGmail()
        gmail.add("m1")

        result = await GmailReconciler(gmail, hydrate=False).expand(
            pointer("2000"), "1000"
        )

        assert result.events[0].payload == {"id": "m1", "emailAddress": "a@b.com"}
        assert gmail.fetched == [], "nothing should be fetched"

    async def test_it_asks_only_for_message_additions_by_default(self) -> None:
        """A label change is a history record too, and a triage workflow woken
        by its own labelling is the loop this shape makes easiest to write."""
        gmail = FakeGmail()
        await GmailReconciler(gmail).expand(pointer("2000"), "1000")

        assert gmail.history_calls[0][1] == ["messageAdded"]

    async def test_the_history_types_are_configurable(self) -> None:
        gmail = FakeGmail()
        await GmailReconciler(gmail, history_types=["labelAdded"]).expand(
            pointer("2000"), "1000"
        )

        assert gmail.history_calls[0][1] == ["labelAdded"]

    async def test_one_unfetchable_message_does_not_cost_the_others(self) -> None:
        """A message deleted between the history read and the fetch is normal,
        not an error."""
        gmail = FakeGmail()
        gmail.add("m1")
        gmail.add("gone")
        gmail.add("m3")
        gmail.unfetchable = {"gone"}

        result = await GmailReconciler(gmail).expand(pointer("2000"), "1000")

        assert [e.dedupe_suffix for e in result.events] == ["m1", "m3"]

    async def test_one_message_touched_twice_is_emitted_once(self) -> None:
        """Added, then labelled, then labelled again — a workflow wants it
        once."""
        gmail = FakeGmail()
        gmail.add("m1")
        gmail.add("m1", kind="labelsAdded")

        result = await GmailReconciler(gmail).expand(pointer("2000"), "1000")

        assert len(result.events) == 1

    async def test_the_cursor_is_gmails_position_not_the_pointers(self) -> None:
        """The pointer may be behind — Pub/Sub reorders — and rewinding to it
        would re-read the same history on every notification."""
        gmail = FakeGmail()
        gmail.latest = "3000"

        result = await GmailReconciler(gmail).expand(pointer("2000"), "1000")

        assert result.cursor == "3000"

    async def test_an_expired_history_id_becomes_a_cursor_expiry(self) -> None:
        """Which the PointerReconciler turns into a gap event rather than a
        silent jump to now."""
        gmail = FakeGmail(oldest_held=500)

        with pytest.raises(CursorExpired) as exc:
            await GmailReconciler(gmail).expand(pointer("2000"), "1")

        assert exc.value.cursor == "1"

    async def test_the_max_caps_how_many_are_hydrated(self) -> None:
        gmail = FakeGmail()
        for n in range(10):
            gmail.add(f"m{n}")

        result = await GmailReconciler(gmail, max_messages=3).expand(
            pointer("2000"), "1000"
        )

        assert len(result.events) == 3 and len(gmail.fetched) == 3

    async def test_it_drives_end_to_end_through_a_pointer_reconciler(self) -> None:
        """The claim: downstream reads `app.gmail.message` and never learns
        that a history call happened."""
        store = MemoryStore()
        log = StoreBackedEventLog(store)
        gmail = FakeGmail()
        driver = PointerReconciler(
            GmailReconciler(gmail),
            log=log,
            checkpoints=StoreBackedCheckpoints(store),
            state=SourceState(store, "gmail"),
        )

        async def push(history_id: str) -> None:
            await log.append("app.gmail.push", [
                EventRecord(
                    event_id=f"app.gmail.push/gmail:psm-{history_id}",
                    type="gmail.push",
                    payload=pointer(history_id),
                    key="a@b.com",
                    source="gmail",
                )
            ])

        await push("1000")
        await driver.drain()
        gmail.add("m1")
        gmail.latest = "2000"
        await push("2000")
        assert await driver.drain() == 1

        (row,) = await log.read("app.gmail.message", after=None, limit=10)
        assert row.payload["subject"] == "hi"
        assert "historyId" not in row.payload


class TestGmailWatcher:
    async def test_it_declares_the_seven_day_lifetime(self) -> None:
        """Without the hint the renewer assumes a week — which here is right,
        and for Graph would be badly wrong. Declared rather than inferred."""
        registration = await GmailWatcher(
            "projects/p/topics/t", client_for=lambda _: _FakeWatchClient()
        ).register("a@b.com")

        assert registration.metadata["lifetime_seconds"] == 7 * 24 * 3600

    async def test_it_carries_the_position_to_adopt(self) -> None:
        """A watch established now says nothing about what came before it."""
        registration = await GmailWatcher(
            "projects/p/topics/t", client_for=lambda _: _FakeWatchClient()
        ).register("a@b.com")

        assert registration.cursor == "9876"
        assert registration.resource == "a@b.com"

    async def test_each_mailbox_gets_its_own_client(self) -> None:
        """Gmail's `me` alias only works for the authenticated user, so a
        service account watching several mailboxes has to name each one."""
        asked: list[str] = []

        def build(resource: str) -> Any:
            asked.append(resource)
            return _FakeWatchClient()

        watcher = GmailWatcher("projects/p/topics/t", client_for=build)
        await watcher.register("a@b.com")
        await watcher.register("b@b.com")

        assert asked == ["a@b.com", "b@b.com"]

    async def test_stopping_reaches_the_provider(self) -> None:
        client = _FakeWatchClient()
        await GmailWatcher(
            "projects/p/topics/t", client_for=lambda _: client
        ).stop("a@b.com")

        assert client.stopped


class _FakeWatchClient:
    def __init__(self) -> None:
        self.stopped = False

    async def watch(self, topic_name: str, *, label_ids: Any = None) -> Any:
        from loom.toolsets.google.gmail.models import GmailWatch

        return GmailWatch(
            history_id="9876",
            expiration=datetime(2026, 1, 8, tzinfo=UTC),
        )

    async def stop_watch(self) -> None:
        self.stopped = True


# ---------------------------------------------------------------------------
# Extensibility wiring — the claim that a third party needs no PR
# ---------------------------------------------------------------------------


class Acme:
    id = "acme"

    def verify(self, headers: Any, body: bytes) -> None:
        return None

    def challenge(self, headers: Any, body: bytes) -> None:
        return None

    def delivery_id(self, headers: Any, payload: Any) -> str:
        return "d1"

    async def expand(self, payload: Any, ctx: SourceContext) -> list[InboundEvent]:
        return [InboundEvent(type="acme.thing", payload={})]


class _EntryPoint:
    def __init__(self, name: str, value: Any) -> None:
        self.name = name
        self._value = value

    def load(self) -> Any:
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


@pytest.fixture
def clean_catalog():
    """A pristine process-global catalog, restored afterwards."""
    from loom.events import source_registry

    saved = source_registry._catalog
    source_registry._catalog = EventSourceRegistry()
    yield source_registry._catalog
    source_registry._catalog = saved


class TestEntryPoints:
    def test_a_class_is_instantiated(self, clean_catalog, monkeypatch) -> None:
        """A source with no configuration is naturally a class."""
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda **kw: [_EntryPoint("acme", Acme)],
        )

        assert discover_source_entry_points() == 1
        assert isinstance(clean_catalog.get("acme"), Acme)

    def test_a_factory_is_called(self, clean_catalog, monkeypatch) -> None:
        """One with configuration is naturally a factory. Forcing either shape
        makes somebody write a wrapper."""
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda **kw: [_EntryPoint("acme", lambda: Acme())],
        )

        assert discover_source_entry_points() == 1
        assert clean_catalog.get("acme") is not None

    def test_something_that_is_not_a_source_is_skipped_not_fatal(
        self, clean_catalog, monkeypatch
    ) -> None:
        """One broken package must not stop every other provider loading."""
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda **kw: [
                _EntryPoint("broken", lambda: object()),
                _EntryPoint("acme", Acme),
            ],
        )

        assert discover_source_entry_points() == 1
        assert clean_catalog.get("acme") is not None

    def test_an_entry_point_that_raises_is_skipped(
        self, clean_catalog, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda **kw: [
                _EntryPoint("bad", ImportError("no such module")),
                _EntryPoint("acme", Acme),
            ],
        )

        assert discover_source_entry_points() == 1

    def test_no_entry_points_is_zero_not_an_error(
        self, clean_catalog, monkeypatch
    ) -> None:
        monkeypatch.setattr("importlib.metadata.entry_points", lambda **kw: [])

        assert discover_source_entry_points() == 0


class TestBuiltinFallback:
    def test_an_unknown_id_is_none(self) -> None:
        assert builtin_source("nope") is None

    def test_a_builtin_that_will_not_import_is_absent_not_fatal(
        self, monkeypatch
    ) -> None:
        """Which is what it was before the fallback existed — a missing optional
        dependency must not break the registry for every other provider."""
        from loom.events import source_registry

        monkeypatch.setitem(
            source_registry.BUILTIN_SOURCES, "ghost", "no.such.module.Ghost"
        )

        assert builtin_source("ghost") is None

    def test_a_builtin_that_will_not_construct_is_absent_not_fatal(
        self, monkeypatch
    ) -> None:
        from loom.events import source_registry

        class Exploding:
            def __init__(self) -> None:
                raise RuntimeError("needs configuration")

        monkeypatch.setattr(source_registry, "_TEST_EXPLODING", Exploding, raising=False)
        monkeypatch.setitem(
            source_registry.BUILTIN_SOURCES,
            "exploding",
            "loom.events.source_registry._TEST_EXPLODING",
        )

        assert builtin_source("exploding") is None


class TestRegistryApi:
    def test_require_names_what_is_reachable(self) -> None:
        registry = EventSourceRegistry()

        with pytest.raises(ConfigurationError) as exc:
            registry.require("nope")

        assert "slack" in str(exc.value), "the message must list what is there"

    def test_membership_and_length_read_naturally(self) -> None:
        registry = EventSourceRegistry()
        registry.register(Acme())

        assert "acme" in registry
        assert "nope" not in registry
        assert len(registry) == len(list(registry))
        assert "acme" in list(registry)

    def test_unregistering_a_local_falls_back_to_the_parent(self) -> None:
        parent = EventSourceRegistry()
        parent.register(Acme())
        child = EventSourceRegistry(parent=parent)
        local = Acme()
        child.register(local)

        child.unregister("acme")

        assert child.get("acme") is not local
        assert child.get("acme") is not None

    def test_repr_says_how_many_are_local(self) -> None:
        registry = EventSourceRegistry()
        registry.register(Acme())

        assert "1 local" in repr(registry)


# ---------------------------------------------------------------------------
# Ingress degradation
# ---------------------------------------------------------------------------


class TestIngressDecoding:
    @pytest.fixture
    def ingress(self) -> WebhookIngress:
        store = MemoryStore()
        runtime = Runtime(store=store, events=StoreBackedEventLog(store))
        runtime.sources.register(_Echo())
        return WebhookIngress(runtime)

    async def test_an_empty_body_is_an_empty_payload(self, ingress) -> None:
        result = await ingress.receive("echo", {}, b"")

        assert result.count == 1

    async def test_a_plain_text_body_is_carried_rather_than_rejected(
        self, ingress
    ) -> None:
        """A provider that posts text/plain is unusual, not wrong, and dropping
        it loses an event that arrived."""
        await ingress.receive("echo", {}, b"just some text")

        assert _Echo.last == {"body": "just some text"}

    async def test_a_form_body_without_a_payload_field_is_read_as_pairs(
        self, ingress
    ) -> None:
        await ingress.receive("echo", {}, b"a=1&b=2")

        assert _Echo.last == {"a": "1", "b": "2"}

    async def test_a_form_payload_that_is_not_json_degrades_to_the_pairs(
        self, ingress
    ) -> None:
        """Rather than raising: the pairs are still what arrived, and a caller
        that can read them is better off than one handed an exception."""
        await ingress.receive("echo", {}, b"payload=not-json")

        assert _Echo.last == {"payload": "not-json"}

    async def test_a_body_that_is_not_utf8_is_malformed(self, ingress) -> None:
        from loom.events import MalformedDelivery

        with pytest.raises(MalformedDelivery):
            await ingress.receive("echo", {}, b"\xff\xfe\x00\x01")

    async def test_a_source_that_publishes_no_delivery_id_still_dedupes(
        self, ingress
    ) -> None:
        """The body hash is the fallback, and it deduplicates an identical
        redelivery — which is the case that actually happens."""
        body = json.dumps({"a": 1}).encode()

        first = await ingress.receive("echo", {}, body)
        second = await ingress.receive("echo", {}, body)

        assert first.event_ids == second.event_ids


class _Echo:
    """A source with no delivery id, so the ingress must hash the body."""

    id = "echo"
    last: Any = None

    def verify(self, headers: Any, body: bytes) -> None:
        return None

    def challenge(self, headers: Any, body: bytes) -> None:
        return None

    def delivery_id(self, headers: Any, payload: Any) -> None:
        return None

    async def expand(self, payload: Any, ctx: SourceContext) -> list[InboundEvent]:
        _Echo.last = payload
        return [InboundEvent(type="echo.thing", payload={"seen": True})]


# ---------------------------------------------------------------------------
# Dispatcher: backpressure and lifecycle
# ---------------------------------------------------------------------------


TOPIC = "app.test.thing"


def event(n: int) -> EventRecord:
    return EventRecord(
        event_id=f"{TOPIC}/test:e{n}", type="test.thing", payload={"n": n}
    )


@pytest.fixture
def dispatching():
    store = MemoryStore()
    runtime = Runtime(store=store, events=StoreBackedEventLog(store))
    marks = StoreBackedCheckpoints(store)
    dispatcher = EventDispatcher(runtime, log=runtime.events, checkpoints=marks)
    return runtime, runtime.events, marks, dispatcher


class TestBackpressure:
    async def test_a_retryable_admission_rejection_defers(
        self, dispatching
    ) -> None:
        """Backpressure is already built: a rejected dispatch leaves no run
        behind and the checkpoint simply does not advance, so it retries."""
        runtime, log, marks, dispatcher = dispatching
        await dispatcher.subscribe(Subscription("s", TOPIC, "w"))
        await log.append(TOPIC, [event(1)])

        async def rejecting(*a: Any, **k: Any) -> str:
            raise AdmissionRejected("at capacity", decision="delay")

        runtime.submit = rejecting  # type: ignore[method-assign]
        report = (await dispatcher.poll_once())[0]

        assert report.deferred == 1
        assert report.dead_lettered == 0
        assert await marks.load("s", TOPIC) is None

    async def test_a_permanent_admission_rejection_dead_letters(
        self, dispatching
    ) -> None:
        """"This will never be admitted" retried forever stalls the subscriber
        behind an event that cannot run."""
        runtime, log, marks, dispatcher = dispatching
        await dispatcher.subscribe(Subscription("s", TOPIC, "w"))
        await log.append(TOPIC, [event(1), event(2)])

        async def rejecting(*a: Any, **k: Any) -> str:
            raise AdmissionRejected("never", decision="skip")

        runtime.submit = rejecting  # type: ignore[method-assign]
        report = (await dispatcher.poll_once())[0]

        assert report.dead_lettered == 2
        assert await marks.load("s", TOPIC) == "2", "it must get past them"
        assert len(await log.read(f"{TOPIC}.dead", after=None, limit=10)) == 2

    async def test_a_retryable_rejection_clears_and_proceeds(
        self, dispatching
    ) -> None:
        runtime, log, marks, dispatcher = dispatching
        await dispatcher.register(_registered(runtime))
        await log.append(TOPIC, [event(1)])
        original = runtime.submit
        rejected = {"once": False}

        async def flaky(*a: Any, **k: Any) -> str:
            if not rejected["once"]:
                rejected["once"] = True
                raise AdmissionRejected("at capacity", decision="delay")
            return await original(*a, **k)

        runtime.submit = flaky  # type: ignore[method-assign]
        await dispatcher.poll_once()
        assert await marks.load("thing_worker", TOPIC) is None

        await dispatcher.poll_once()
        assert await marks.load("thing_worker", TOPIC) == "1"

    async def test_the_report_carries_what_a_pass_did(self, dispatching) -> None:
        """`committed_through` is what an operator reads to answer "did that
        pass get anywhere"."""
        runtime, log, _, dispatcher = dispatching
        await dispatcher.register(_registered(runtime))
        await log.append(TOPIC, [event(1), event(2)])

        report = (await dispatcher.poll_once())[0]

        assert report.committed_through == "2"
        assert report.matched == 2 and report.filtered == 0


def _registered(runtime: Runtime) -> Any:
    from loom import workflow
    from loom.triggers.specs import OnAppEvent

    @workflow(name="thing_worker", triggers=[OnAppEvent(TOPIC)])
    async def thing_worker(ctx: Any, payload: dict) -> str:
        return "ok"

    return thing_worker


class TestDispatcherIdle:
    async def test_the_loop_waits_rather_than_spinning(self, dispatching) -> None:
        """A quiet system must not cost thousands of reads a second."""
        _, log, _, dispatcher = dispatching
        await dispatcher.subscribe(Subscription("s", TOPIC, "w"))
        waited: list[Any] = []
        original = log.wait_for

        async def counting(topic: str, **kw: Any) -> bool:
            waited.append(topic)
            return await original(topic, **kw)

        log.wait_for = counting  # type: ignore[method-assign]
        dispatcher._idle_wait = 0.01
        await dispatcher.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0)
                if waited:
                    break
            await asyncio.sleep(0.05)
        finally:
            await dispatcher.stop()

        assert waited, "an idle pass must wait on the log, not re-poll immediately"

    async def test_it_idles_safely_with_no_subscriptions(
        self, dispatching
    ) -> None:
        """A dispatcher started before any workflow registers must not spin."""
        _, _, _, dispatcher = dispatching
        dispatcher._idle_wait = 0.01
        await dispatcher.start()
        await asyncio.sleep(0.03)
        await dispatcher.stop()

    async def test_a_broken_wait_still_waits(self, dispatching) -> None:
        """An adapter whose `wait_for` raises — unsupported, or a broker
        mid-outage — must degrade to a timed sleep.

        Swallowing the error and returning instantly turns the idle path into a
        busy spin, and that is not merely wasteful. A store whose ``async def``
        methods never actually await yields no control, so the dispatcher task
        **starves every other coroutine in the process**: it presents as a
        hang, not as a hot CPU.

        Timed directly rather than driven through ``start()``, and that is
        forced rather than stylistic — under the bug the loop never yields, so
        ``asyncio.wait_for`` around it never fires either. There is no in-loop
        timeout that can catch this; only not entering the loop can.
        """
        runtime, log, marks, dispatcher = dispatching
        await dispatcher.register(_registered(runtime))

        async def broken(topic: str, **kw: Any) -> bool:
            raise OSError("wait is not supported here")

        log.wait_for = broken  # type: ignore[method-assign]
        dispatcher._idle_wait = 0.05

        started = asyncio.get_running_loop().time()
        await dispatcher._idle()
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed >= 0.04, (
            f"an idle pass returned in {elapsed:.4f}s when the wait failed; it "
            "must fall back to a timed sleep or the loop spins without yielding"
        )

        # Only now start the loop. The order is the safeguard: driving it while
        # `_idle` returns instantly would hang this test rather than fail it,
        # and there is no timeout that escapes a task which never yields.
        dispatcher._idle_wait = 0.01
        await dispatcher.start()
        try:
            await log.append(TOPIC, [event(1)])
            for _ in range(200):
                await asyncio.sleep(0.005)
                if await marks.load("thing_worker", TOPIC):
                    break
            assert await marks.load("thing_worker", TOPIC) == "1", (
                "a degraded idle must still be a working one"
            )
        finally:
            await dispatcher.stop()


# ---------------------------------------------------------------------------
# Watch lifecycle
# ---------------------------------------------------------------------------


class CountingWatch:
    id = "counting"

    def __init__(self) -> None:
        self.calls = 0

    async def register(self, resource: str) -> WatchRegistration:
        self.calls += 1
        return WatchRegistration(
            resource=resource,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )

    async def stop(self, resource: str) -> None:
        pass


class TestWatchLifecycle:
    async def test_start_sweeps_immediately(self) -> None:
        """"On restart" means now, not one interval from now: a process down
        for a day comes back to watches that expired while it was gone."""
        watch = CountingWatch()
        renewer = WatchRenewer(watch, interval_seconds=3600)
        renewer.track("a@b.com")

        await renewer.start()
        try:
            assert watch.calls == 1
        finally:
            await renewer.stop()

    async def test_starting_twice_is_a_no_op(self) -> None:
        watch = CountingWatch()
        renewer = WatchRenewer(watch, interval_seconds=3600)
        renewer.track("a@b.com")

        await renewer.start()
        await renewer.start()
        try:
            assert watch.calls == 1
        finally:
            await renewer.stop()

    async def test_stop_is_safe_before_start_and_twice(self) -> None:
        renewer = WatchRenewer(CountingWatch())
        await renewer.stop()
        await renewer.start()
        await renewer.stop()
        await renewer.stop()

    async def test_a_runtime_shutdown_stops_it(self) -> None:
        """It registers through `supervise()`, so a host does not have to know
        which background services it happens to have wired up."""
        runtime = Runtime(store=MemoryStore())
        renewer = WatchRenewer(CountingWatch(), runtime=runtime)
        renewer.track("a@b.com")
        await renewer.start()

        assert renewer._task is not None
        await runtime.shutdown(drain=0)

        assert renewer._task is None

    async def test_untracking_stops_renewing_it(self) -> None:
        watch = CountingWatch()
        renewer = WatchRenewer(watch, fraction=1.0)
        renewer.track("a@b.com")
        await renewer.sweep()
        renewer.untrack("a@b.com")

        await renewer.sweep()

        assert watch.calls == 1
        assert renewer.statuses == {}

    async def test_the_callback_sees_each_registration(self) -> None:
        """How a host persists the cursor a fresh watch hands back."""
        seen: list[tuple[str, str]] = []

        async def remember(resource: str, reg: WatchRegistration) -> None:
            seen.append((resource, reg.cursor))

        renewer = WatchRenewer(CountingWatch(), on_registered=remember)
        renewer.track("a@b.com")
        await renewer.sweep()

        assert seen == [("a@b.com", "")]

    async def test_a_failing_callback_does_not_undo_the_renewal(self) -> None:
        """The watch is registered; a broken bookkeeping hook must not make
        that look like a failure."""

        async def broken(resource: str, reg: WatchRegistration) -> None:
            raise OSError("the host's database is down")

        watch = CountingWatch()
        renewer = WatchRenewer(watch, on_registered=broken)
        renewer.track("a@b.com")

        assert await renewer.sweep() == ["a@b.com"]
        assert renewer.statuses["a@b.com"].healthy

    async def test_tracking_the_same_resource_twice_is_one_entry(self) -> None:
        renewer = WatchRenewer(CountingWatch())
        first = renewer.track("a@b.com")

        assert renewer.track("a@b.com") is first
        assert len(renewer.statuses) == 1

    async def test_a_lapse_with_no_log_configured_is_not_an_error(self) -> None:
        """The renewer is useful without an event log; it just cannot announce."""

        class Failing(CountingWatch):
            async def register(self, resource: str) -> WatchRegistration:
                raise OSError("no")

        renewer = WatchRenewer(Failing(), log=None)
        renewer.track("a@b.com")

        assert await renewer.sweep() == []
        assert renewer.statuses["a@b.com"].consecutive_failures == 1


# ---------------------------------------------------------------------------
# Reconciler degradation
# ---------------------------------------------------------------------------


class TestReconcilerDegradation:
    async def test_it_works_with_no_state_configured(self) -> None:
        """Degraded rather than broken: with no cursor store it re-adopts each
        pointer, which processes nothing but never claims to have."""
        store = MemoryStore()
        log = StoreBackedEventLog(store)

        class Adopting:
            id = "x"

            async def expand(self, pointer: dict[str, Any], cursor: str) -> Expansion:
                assert cursor == "", "with no state there is never a cursor"
                return Expansion(cursor="1")

        driver = PointerReconciler(
            Adopting(), log=log, checkpoints=StoreBackedCheckpoints(store)
        )
        await log.append("app.x.push", [
            EventRecord(event_id="app.x.push/x:1", type="x.push", payload={})
        ])

        assert await driver.drain() == 0

    async def test_an_incomplete_expansion_still_advances(self) -> None:
        """The cursor advances to what *was* read, so the next pass continues
        rather than re-reading; what must not happen is advancing past unread
        changes."""
        store = MemoryStore()
        log = StoreBackedEventLog(store)
        state = SourceState(store, "x")

        class Partial:
            id = "x"

            async def expand(self, pointer: dict[str, Any], cursor: str) -> Expansion:
                return Expansion(
                    events=[InboundEvent(type="x.item", payload={}, dedupe_suffix="a")],
                    cursor="500",
                    complete=False,
                )

        driver = PointerReconciler(
            Partial(),
            log=log,
            checkpoints=StoreBackedCheckpoints(store),
            state=state,
        )
        await log.append("app.x.push", [
            EventRecord(event_id="app.x.push/x:1", type="x.push", payload={})
        ])

        assert await driver.drain() == 1
        assert await state.get("cursor") == "500"

    async def test_the_gap_callback_fires_and_cannot_break_recovery(self) -> None:
        store = MemoryStore()
        log = StoreBackedEventLog(store)
        called: list[tuple[str, str]] = []

        async def on_gap(expired: str, resumed: str) -> None:
            called.append((expired, resumed))
            raise OSError("the host's alerting is down")

        class Expiring:
            id = "x"

            async def expand(self, pointer: dict[str, Any], cursor: str) -> Expansion:
                if not cursor:
                    return Expansion(cursor="1")
                raise CursorExpired("gone", cursor=cursor)

        driver = PointerReconciler(
            Expiring(),
            log=log,
            checkpoints=StoreBackedCheckpoints(store),
            state=SourceState(store, "x"),
            on_gap=on_gap,
        )
        for n in (1, 2):
            await log.append("app.x.push", [
                EventRecord(
                    event_id=f"app.x.push/x:{n}",
                    type="x.push",
                    payload={"cursor": str(n * 100)},
                )
            ])
            await driver.drain()

        assert called == [("1", "200")]
        assert await log.head(topic_for("x.gap")) is not None

    async def test_a_custom_topic_and_subscriber_name_are_honoured(self) -> None:
        """A host running two reconcilers over one provider needs distinct
        names, or they share a checkpoint and consume each other's backlog."""
        store = MemoryStore()
        log = StoreBackedEventLog(store)

        class Noop:
            id = "x"

            async def expand(self, pointer: dict[str, Any], cursor: str) -> Expansion:
                return Expansion(cursor="1")

        driver = PointerReconciler(
            Noop(),
            log=log,
            checkpoints=StoreBackedCheckpoints(store),
            pointer_topic="custom.topic",
            subscriber="custom-name",
        )

        assert driver.topic == "custom.topic"
        assert driver.subscriber == "custom-name"


# ---------------------------------------------------------------------------
# SourceState
# ---------------------------------------------------------------------------


class TestSourceState:
    async def test_it_round_trips_structured_values(self) -> None:
        state = SourceState(MemoryStore(), "gmail")
        await state.set("watch", {"expires": "2026-01-01", "labels": ["INBOX"]})

        assert await state.get("watch") == {
            "expires": "2026-01-01",
            "labels": ["INBOX"],
        }

    async def test_a_missing_key_returns_the_default(self) -> None:
        state = SourceState(MemoryStore(), "gmail")

        assert await state.get("nothing") is None
        assert await state.get("nothing", "fallback") == "fallback"

    async def test_two_sources_do_not_share_a_namespace(self) -> None:
        store = MemoryStore()
        await SourceState(store, "gmail").set("cursor", "1")
        await SourceState(store, "jira").set("cursor", "2")

        assert await SourceState(store, "gmail").get("cursor") == "1"

    async def test_deleting_clears_it(self) -> None:
        state = SourceState(MemoryStore(), "gmail")
        await state.set("cursor", "1")
        await state.delete("cursor")

        assert await state.get("cursor") is None

    async def test_a_cursor_is_stored_without_a_ttl(self) -> None:
        """A cursor that quietly evicted would reset the source to now and lose
        whatever happened while it was gone — the silent class this design
        exists to make loud."""
        seen: list[float] = []
        store = MemoryStore()
        original = store.set

        async def watching(key: str, value: Any, ttl_seconds: float) -> None:
            seen.append(ttl_seconds)
            return await original(key, value, ttl_seconds)

        store.set = watching  # type: ignore[method-assign]
        await SourceState(store, "gmail").set("cursor", "1")

        assert seen == [0], "0 means no expiry"


# ---------------------------------------------------------------------------
# Slack and Jira corners
# ---------------------------------------------------------------------------


class TestProviderCorners:
    def test_a_slack_timestamp_that_is_nonsense_is_none_not_a_crash(self) -> None:
        from loom.toolsets.slack.source import _slack_time

        assert _slack_time("not-a-time") is None
        assert _slack_time(None) is None
        assert _slack_time(10**20) is None

    def test_a_slack_challenge_that_is_not_a_string_is_malformed(self) -> None:
        from loom.events import MalformedDelivery
        from loom.toolsets.slack.source import SlackSource

        body = json.dumps({"type": "url_verification", "challenge": {"a": 1}}).encode()

        with pytest.raises(MalformedDelivery):
            SlackSource("s").challenge({}, body)

    def test_a_slack_challenge_on_a_non_json_body_is_none(self) -> None:
        from loom.toolsets.slack.source import SlackSource

        assert SlackSource("s").challenge({}, b"not json") is None

    def test_a_non_integer_slack_timestamp_is_refused(self) -> None:
        from loom.events import VerificationFailed
        from loom.toolsets.slack.source import SlackSource

        with pytest.raises(VerificationFailed, match="not an integer"):
            SlackSource("s").verify(
                {"x-slack-signature": "v0=x", "x-slack-request-timestamp": "soon"},
                b"{}",
            )

    def test_a_slack_slash_command_is_keyed_by_channel(self) -> None:
        from loom.toolsets.slack.source import SlackSource

        events = _sync(
            SlackSource("s").expand(
                {"command": "/deploy", "channel_id": "C1", "text": "prod"},
                _ctx("slack"),
            )
        )

        assert events[0].type == "slack.slash_command"
        assert events[0].key == "C1"

    def test_a_jira_comment_without_an_issue_still_orders_by_something(
        self,
    ) -> None:
        """Losing the key entirely means a comment's edits can be reordered."""
        from loom.toolsets.jira.source import issue_key

        assert issue_key({"comment": {"id": "10500"}}) == "comment:10500"

    def test_a_jira_payload_with_nothing_to_key_on_has_no_key(self) -> None:
        from loom.toolsets.jira.source import issue_key

        assert issue_key({}) == ""

    def test_a_jira_timestamp_that_is_a_string_is_none(self) -> None:
        """Jira sends epoch milliseconds as a number; a string means the shape
        changed, and guessing a parse would invent a time."""
        from loom.toolsets.jira.source import _jira_time

        assert _jira_time("1701234567000") is None
        assert _jira_time(None) is None

    def test_jira_has_no_handshake(self) -> None:
        from loom.toolsets.jira.source import JiraSource

        assert JiraSource("s").challenge({}, b"{}") is None

    def test_the_gmail_bearer_parser_ignores_other_schemes(self) -> None:
        from loom.toolsets.google.gmail.source import _bearer

        assert _bearer("Bearer abc") == "abc"
        assert _bearer("bearer abc") == "abc"
        assert _bearer("Basic abc") == ""
        assert _bearer("") == ""

    def test_a_gmail_push_with_undecodable_data_is_malformed(self) -> None:
        from loom.events import MalformedDelivery
        from loom.toolsets.google.gmail.source import decode_push

        with pytest.raises(MalformedDelivery, match="base64"):
            decode_push({"message": {"data": "!!!not base64!!!"}})

    def test_a_gmail_push_with_no_data_is_malformed(self) -> None:
        from loom.events import MalformedDelivery
        from loom.toolsets.google.gmail.source import decode_push

        with pytest.raises(MalformedDelivery, match=r"message\.data"):
            decode_push({"message": {"messageId": "1"}})

    def test_a_gmail_push_carrying_a_json_scalar_is_malformed(self) -> None:
        from loom.events import MalformedDelivery
        from loom.toolsets.google.gmail.source import decode_push

        data = base64.b64encode(b'"just a string"').decode()

        with pytest.raises(MalformedDelivery, match="historyId"):
            decode_push({"message": {"data": data}})

    def test_a_gmail_push_with_no_message_id_falls_back_to_a_hash(self) -> None:
        from loom.toolsets.google.gmail.source import GmailSource

        assert GmailSource(require_token=False).delivery_id({}, {"message": {}}) is None


def _ctx(source_id: str) -> SourceContext:
    return SourceContext(
        source_id=source_id, state=SourceState(MemoryStore(), source_id)
    )


def _sync(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# A backend that is not the reference implementation
# ---------------------------------------------------------------------------


class OpaqueLog:
    """An `EventLog` whose positions are genuinely opaque.

    The reference implementation numbers sequentially, so every helper that
    quietly assumes arithmetic works passes against it and fails against the
    first Kafka or Redis Streams adapter somebody writes — which is the exact
    substitutability failure the conformance kits exist to prevent, reappearing
    one layer up in the *manager*.
    """

    def __init__(self, count: int = 5, *, readable: bool = True) -> None:
        self._rows = [
            StoredEventStub(f"seg-7:{n}", EventRecord(event_id=f"e{n}", type="t"))
            for n in range(count)
        ]
        self._readable = readable

    async def head(self, topic: str) -> str | None:
        return self._rows[-1].position if self._rows else None

    async def read(self, topic: str, *, after: Any, limit: int) -> list[Any]:
        if not self._readable:
            raise OSError("this broker cannot enumerate a range")
        if after is None:
            return self._rows[:limit]
        index = next(
            (i for i, r in enumerate(self._rows) if r.position == after), None
        )
        return self._rows[index + 1 : index + 1 + limit] if index is not None else []

    async def append(self, topic: str, records: Any) -> list[str]:
        return []

    async def retain(self, topic: str, policy: Any) -> int:
        return 0

    async def wait_for(self, topic: str, *, after: Any, timeout: float) -> bool:
        return False


class StoredEventStub:
    def __init__(self, position: str, record: EventRecord) -> None:
        self.position = position
        self.record = record
        self.appended_at = datetime.now(UTC)

    @property
    def payload(self) -> Any:
        return self.record.payload

    @property
    def event_id(self) -> str:
        return self.record.event_id


class TestOpaquePositions:
    async def test_lag_is_counted_against_an_opaque_position(self) -> None:
        from loom.events.manager import SubscriptionManager

        store = MemoryStore()
        marks = StoreBackedCheckpoints(store)
        manager = SubscriptionManager(store, log=OpaqueLog(5), checkpoints=marks)
        await manager.add(Subscription("s", "t", "w"))
        await marks.commit("s", "t", "seg-7:2")

        (row,) = await manager.health()

        assert row.lag == 2, "counted by reading, not by subtracting"
        assert row.head == "seg-7:4"

    async def test_a_log_that_cannot_count_reports_unknown_not_zero(self) -> None:
        """Guessing would put a wrong number in front of somebody deciding
        whether to page, and zero is the most reassuring wrong number."""
        from loom.events.manager import SubscriptionManager

        store = MemoryStore()
        marks = StoreBackedCheckpoints(store)
        manager = SubscriptionManager(
            store, log=OpaqueLog(5, readable=False), checkpoints=marks
        )
        await manager.add(Subscription("s", "t", "w"))
        await marks.commit("s", "t", "seg-7:2")

        (row,) = await manager.health()

        assert row.lag is None

    async def test_a_replay_refuses_to_claim_a_start_it_cannot_compute(
        self,
    ) -> None:
        """`_before` can only decrement a position it understands. Returning
        `None` — read from the oldest retained — is the honest answer for an
        opaque one; inventing a neighbour would silently skip or repeat."""
        from loom.events.manager import SubscriptionManager

        store = MemoryStore()
        marks = StoreBackedCheckpoints(store)
        manager = SubscriptionManager(store, log=OpaqueLog(5), checkpoints=marks)
        await marks.commit("s", "t", "seg-7:4")

        plan = await manager.replay("s", "t", max_events=3)

        assert plan.from_position is None
        assert await marks.load("s", "t") is None

    async def test_resuming_a_quarantined_subscriber_fails_open(self) -> None:
        """When the log cannot say whether a position is still readable, the
        conservative answer is to allow the resume and let `accept_gap` be the
        deliberate act — refusing on a backend that simply cannot answer would
        make quarantine a one-way door."""
        from loom.events.manager import SubscriptionManager

        store = MemoryStore()
        marks = StoreBackedCheckpoints(store)
        manager = SubscriptionManager(
            store, log=OpaqueLog(5, readable=False), checkpoints=marks
        )
        await manager.add(Subscription("s", "t", "w"))
        await marks.commit("s", "t", "seg-7:0")
        await manager.quarantine("s", "t", "abandoned")

        await manager.resume("s", "t")

        assert (await manager.health())[0].healthy

    async def test_a_manager_with_no_log_still_answers(self) -> None:
        """A host that keeps the registry here and the log elsewhere gets the
        registry, not an exception."""
        from loom.events.manager import SubscriptionManager

        store = MemoryStore()
        manager = SubscriptionManager(store, checkpoints=StoreBackedCheckpoints(store))
        await manager.add(Subscription("s", "t", "w"))

        (row,) = await manager.health()
        assert row.lag is None and row.head is None
        assert (await manager.plan_replay("s", "t")).events == 0

    async def test_a_manager_with_no_checkpoints_does_not_pretend_to_rewind(
        self,
    ) -> None:
        from loom.events.manager import SubscriptionManager

        manager = SubscriptionManager(MemoryStore(), log=OpaqueLog(5))

        plan = await manager.replay("s", "t", max_events=2)

        assert plan.events == 2, "the plan is still honest about the window"


class TestRegistryPersistence:
    async def test_a_corrupt_registry_reads_as_empty_rather_than_raising(
        self,
    ) -> None:
        """A store somebody wrote to by hand must not make every `loom events`
        command a traceback."""
        from loom.events.manager import SubscriptionManager

        store = MemoryStore()
        await store.set("eventsub:registry", "{not json", 0)
        await store.set("eventsub:quarantined", "{also not json", 0)
        manager = SubscriptionManager(store)

        assert await manager.subscriptions() == []
        assert await manager.health() == []

    async def test_a_filter_without_model_dump_still_round_trips(self) -> None:
        """A host's own filter type only has to expose `conditions`."""
        from loom.events.manager import SubscriptionManager

        class HandRolled:
            def __init__(self) -> None:
                self.conditions = {"channel": "C1"}

        store = MemoryStore()
        manager = SubscriptionManager(store)
        await manager.add(
            Subscription("s", "t", "w", filter=HandRolled())  # type: ignore[arg-type]
        )

        loaded = await manager.get("s", "t")
        assert loaded is not None and loaded.filter is not None
        assert loaded.accepts({"channel": "C1"})
        assert not loaded.accepts({"channel": "C2"})

    async def test_a_filter_whose_dumper_raises_falls_back_to_conditions(
        self,
    ) -> None:
        from loom.events.manager import SubscriptionManager

        class Awkward:
            conditions: dict[str, str] = {"channel": "C1"}  # noqa: RUF012

            def model_dump(self) -> dict[str, Any]:
                raise RuntimeError("not serialisable")

        store = MemoryStore()
        manager = SubscriptionManager(store)
        await manager.add(
            Subscription("s", "t", "w", filter=Awkward())  # type: ignore[arg-type]
        )

        loaded = await manager.get("s", "t")
        assert loaded is not None and loaded.filter is not None

    async def test_a_stored_filter_that_no_longer_parses_keeps_its_conditions(
        self,
    ) -> None:
        """A `FilterSpec` field removed in a later version must not make every
        subscription unreadable — the conditions are the part that matters."""
        from loom.events.manager import SubscriptionManager

        store = MemoryStore()
        await store.set(
            "eventsub:registry",
            json.dumps({
                "s@t": {
                    "subscriber": "s",
                    "topic": "t",
                    "workflow": "w",
                    "filter": {"conditions": {"a": 1}, "removed_field": True},
                }
            }),
            0,
        )
        manager = SubscriptionManager(store)

        (loaded,) = await manager.subscriptions()
        assert loaded.filter is not None
        assert loaded.accepts({"a": 1})

    async def test_a_missing_field_takes_its_default(self) -> None:
        from loom.events.manager import SubscriptionManager

        store = MemoryStore()
        await store.set(
            "eventsub:registry",
            json.dumps({"s@t": {"subscriber": "s", "topic": "t", "workflow": "w"}}),
            0,
        )

        (loaded,) = await SubscriptionManager(store).subscriptions()

        assert loaded.max_attempts == 3
        assert loaded.start_at.value == "latest"
