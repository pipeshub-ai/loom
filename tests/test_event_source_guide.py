"""Build event sources by following the guide, and check that they work.

Every snippet in ``docs/guides/event-sources.md`` compiles in CI, which proves
each one is valid Python and proves nothing about whether *following the guide*
gets you a working source. The steps could each run and still not compose — a
source that never reaches the registry, a verifier the ingress never calls,
event ids that do not deduplicate.

The Salesforce half is the phase's acid test rather than a demo. It is
deliberately **not** in the package: if it cannot be built by somebody who is
not us, on the seams as shipped, then the seams are wrong and building it
in-tree would hide that. So this file imports nothing private, patches nothing,
and reaches past no seam — and a final test greps itself to prove it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from loom import Runtime
from loom.events import (
    Challenge,
    EventRecord,
    EventSource,
    InboundEvent,
    SourceContext,
    SourceState,
    StoreBackedCheckpoints,
    StoreBackedEventLog,
    Subscription,
    VerificationFailed,
    Watch,
    WatchRegistration,
    WatchRenewer,
    WebhookIngress,
    lifetime_hint,
    register_event_source,
    topic_for,
    unregister_event_source,
)
from loom.stores.memory import MemoryStore
from loom.testing.conformance import verify_event_source

SECRET = "shhh"


# ---------------------------------------------------------------------------
# Shape A — exactly as the guide writes it
# ---------------------------------------------------------------------------


class ShopifySource:
    """Shopify webhooks. HMAC-SHA256, base64, in X-Shopify-Hmac-Sha256."""

    id = "shopify"

    def __init__(self, secret: str = SECRET) -> None:
        self._secret = secret

    def verify(self, headers: Any, body: bytes) -> None:
        if not self._secret:
            raise VerificationFailed(
                "no Shopify webhook secret configured, so this delivery cannot "
                "be verified. Set SHOPIFY_WEBHOOK_SECRET."
            )
        digest = hmac.new(self._secret.encode(), body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode()
        if not hmac.compare_digest(expected, headers.get("x-shopify-hmac-sha256", "")):
            raise VerificationFailed("Shopify signature mismatch")

    def challenge(self, headers: Any, body: bytes) -> Challenge | None:
        return None

    def delivery_id(self, headers: Any, payload: Any) -> str | None:
        return headers.get("x-shopify-webhook-id")

    async def expand(self, payload: Any, ctx: SourceContext) -> list[InboundEvent]:
        topic = ctx.headers.get("x-shopify-topic", "unknown").replace("/", "_")
        return [
            InboundEvent(
                type=f"shopify.{topic}",
                payload=payload if isinstance(payload, dict) else {},
                key=str(payload.get("id", "")) if isinstance(payload, dict) else "",
            )
        ]


def shopify_headers(body: bytes, *, topic: str = "orders/create") -> dict[str, str]:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).digest()
    return {
        "x-shopify-hmac-sha256": base64.b64encode(digest).decode(),
        "x-shopify-topic": topic,
        "x-shopify-webhook-id": "whid-1",
    }


ORDER = json.dumps({"id": 12345, "email": "a@b.com"}).encode()


@pytest.fixture
def wired():
    store = MemoryStore()
    runtime = Runtime(store=store, events=StoreBackedEventLog(store))
    runtime.sources.register(ShopifySource())
    return runtime, runtime.events, WebhookIngress(runtime)


class TestShapeA:
    async def test_the_guides_source_works_end_to_end(self, wired) -> None:
        _, log, ingress = wired

        result = await ingress.receive("shopify", shopify_headers(ORDER), ORDER)

        assert result.topics == ["app.shopify.orders_create"]
        (stored,) = await log.read("app.shopify.orders_create", after=None, limit=10)
        assert stored.payload["email"] == "a@b.com"
        assert stored.record.key == "12345"

    async def test_the_event_type_comes_from_a_header_not_the_body(
        self, wired
    ) -> None:
        """Which is why `expand` is handed the headers — the signature would
        otherwise force a source to stash them on itself between two calls."""
        _, _log, ingress = wired

        result = await ingress.receive(
            "shopify", shopify_headers(ORDER, topic="customers/update"), ORDER
        )

        assert result.topics == ["app.shopify.customers_update"]

    async def test_a_forged_delivery_never_reaches_the_log(self, wired) -> None:
        _, log, ingress = wired

        with pytest.raises(VerificationFailed):
            await ingress.receive("shopify", {"x-shopify-topic": "orders/create"}, ORDER)

        assert await log.head("app.shopify.orders_create") is None

    async def test_a_redelivery_appends_once(self, wired) -> None:
        _, log, ingress = wired

        await ingress.receive("shopify", shopify_headers(ORDER), ORDER)
        await ingress.receive("shopify", shopify_headers(ORDER), ORDER)

        rows = await log.read("app.shopify.orders_create", after=None, limit=10)
        assert len(rows) == 1

    async def test_it_passes_the_conformance_kit(self) -> None:
        await verify_event_source(
            ShopifySource(),
            sign=shopify_headers,
            sample=ORDER,
            expected_types=["shopify.orders_create"],
        )

    def test_it_satisfies_the_protocol_with_no_base_class(self) -> None:
        """Which is what lets a third party write one without importing
        anything of ours but the dataclasses."""
        assert isinstance(ShopifySource(), EventSource)

    def test_the_entry_point_shape_reaches_every_runtime(self) -> None:
        register_event_source(ShopifySource())
        try:
            assert Runtime(store=MemoryStore()).sources.get("shopify") is not None
        finally:
            unregister_event_source("shopify")

    async def test_a_subscribed_workflow_receives_it(self, wired) -> None:
        """Nothing further is needed: subscriptions, filters and dedupe all
        apply the moment the source is registered."""
        from loom import workflow
        from loom.events import EventDispatcher
        from loom.triggers.filter import FilterSpec

        runtime, _log, ingress = wired
        seen: list[str] = []

        @workflow(name="fulfil")
        async def fulfil(ctx: Any, order: dict) -> str:
            seen.append(order["email"])
            return "ok"

        runtime.register(fulfil)
        dispatcher = EventDispatcher(runtime)
        await dispatcher.subscribe(
            Subscription(
                "fulfil",
                "app.shopify.orders_create",
                "fulfil",
                filter=FilterSpec(conditions={"email": "a@b.com"}),
            )
        )

        await ingress.receive("shopify", shopify_headers(ORDER), ORDER)
        await dispatcher.poll_once()
        import asyncio

        for _ in range(50):
            await asyncio.sleep(0)

        assert seen == ["a@b.com"]


# ---------------------------------------------------------------------------
# Shape C — Salesforce, built entirely outside the package
# ---------------------------------------------------------------------------


class ReplayIdExpired(Exception):  # noqa: N818 - names the state
    """Salesforce no longer holds that replay id."""


class FakeBatch:
    def __init__(self, events: list[dict[str, Any]], last: str) -> None:
        self.events = events
        self.last_replay_id = last


class FakeSalesforce:
    """A pull-log provider: it holds the events and hands out replay ids.

    Retention is modelled, because that is the whole reason shape C needs a gap
    event — 72 hours, and past it Salesforce cannot say what was missed.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self.oldest_held = 0
        self.fetches: list[str | None] = []

    def publish(self, **payload: Any) -> None:
        self._events.append(
            {"replayId": str(len(self._events) + 1), "payload": payload}
        )

    def expire_before(self, replay_id: int) -> None:
        self.oldest_held = replay_id

    async def fetch(self, channel: str, replay_id: str | None, limit: int) -> FakeBatch:
        self.fetches.append(replay_id)
        start = 0
        if replay_id is not None:
            if int(replay_id) < self.oldest_held:
                raise ReplayIdExpired(replay_id)
            start = int(replay_id)
        window = self._events[start : start + limit]
        last = window[-1]["replayId"] if window else (replay_id or "0")
        return FakeBatch(window, last)


class SalesforceRelay:
    """Reads Salesforce's log from our replay id and appends to ours."""

    id = "salesforce"

    def __init__(self, client: Any, *, log: Any, state: SourceState) -> None:
        self._client = client
        self._log = log
        self._state = state

    async def drain(self, channel: str, *, limit: int = 100) -> int:
        replay_id = await self._state.get(f"replay:{channel}")

        try:
            batch = await self._client.fetch(channel, replay_id, limit)
        except ReplayIdExpired:
            await self._record_gap(channel, replay_id)
            batch = await self._client.fetch(channel, None, limit)

        if not batch.events:
            return 0

        topic = topic_for(f"salesforce.{channel}")
        records = [
            EventRecord(
                event_id=f"{topic}/salesforce:{e['replayId']}",
                type=f"salesforce.{channel}",
                payload=e["payload"],
                key=str(e["payload"].get("Id", "")),
                source="salesforce",
            )
            for e in batch.events
        ]
        await self._log.append(topic, records)
        await self._state.set(f"replay:{channel}", batch.last_replay_id)
        return len(records)

    async def _record_gap(self, channel: str, replay_id: str | None) -> None:
        gap_type = "salesforce.gap"
        await self._log.append(
            topic_for(gap_type),
            [
                EventRecord(
                    event_id=f"{topic_for(gap_type)}/{channel}@{replay_id}",
                    type=gap_type,
                    payload={
                        "channel": channel,
                        "expired_replay_id": replay_id,
                        "reason": "Salesforce retains 72 hours; this is older",
                    },
                    key=channel,
                    source="salesforce",
                )
            ],
        )


CHANNEL = "AccountChangeEvent"
SF_TOPIC = "app.salesforce.AccountChangeEvent"


@pytest.fixture
def relaying():
    store = MemoryStore()
    runtime = Runtime(store=store, events=StoreBackedEventLog(store))
    provider = FakeSalesforce()
    relay = SalesforceRelay(
        provider, log=runtime.events, state=SourceState(store, "salesforce")
    )
    return runtime, provider, relay


class TestShapeC:
    async def test_a_pull_provider_lands_in_the_same_log(self, relaying) -> None:
        runtime, provider, relay = relaying
        provider.publish(Id="001A", Name="Acme")
        provider.publish(Id="001B", Name="Globex")

        assert await relay.drain(CHANNEL) == 2

        rows = await runtime.events.read(SF_TOPIC, after=None, limit=10)
        assert [r.payload["Name"] for r in rows] == ["Acme", "Globex"]

    async def test_the_replay_id_is_our_checkpoint_one_hop_upstream(
        self, relaying
    ) -> None:
        """The claim that makes one architecture cover four shapes."""
        runtime, provider, relay = relaying
        provider.publish(Id="001A")
        await relay.drain(CHANNEL)
        provider.publish(Id="001B")

        await relay.drain(CHANNEL)

        assert provider.fetches == [None, "1"], (
            "the second read must resume from the stored replay id"
        )
        assert len(await runtime.events.read(SF_TOPIC, after=None, limit=10)) == 2

    async def test_it_resumes_across_a_restart(self, relaying) -> None:
        runtime, provider, relay = relaying
        provider.publish(Id="001A")
        await relay.drain(CHANNEL)

        # A different relay instance entirely — a restart, not a variable.
        fresh = SalesforceRelay(
            provider,
            log=runtime.events,
            state=SourceState(runtime.store, "salesforce"),
        )
        provider.publish(Id="001B")
        await fresh.drain(CHANNEL)

        assert len(await runtime.events.read(SF_TOPIC, after=None, limit=10)) == 2

    async def test_a_re_read_after_a_crash_appends_nothing_twice(
        self, relaying
    ) -> None:
        """The replay id is the provider's own delivery id, so a re-read
        produces identical event ids."""
        runtime, provider, relay = relaying
        provider.publish(Id="001A")
        provider.publish(Id="001B")
        await relay.drain(CHANNEL)

        # The crash: the append happened, the replay id never got stored.
        await SourceState(runtime.store, "salesforce").delete(f"replay:{CHANNEL}")
        await relay.drain(CHANNEL)

        assert len(await runtime.events.read(SF_TOPIC, after=None, limit=10)) == 2

    async def test_an_expired_replay_id_records_a_gap(self, relaying) -> None:
        """Silently resuming from now makes 'nothing happened' and 'we lost a
        day' indistinguishable."""
        runtime, provider, relay = relaying
        provider.publish(Id="001A")
        await relay.drain(CHANNEL)
        for n in range(5):
            provider.publish(Id=f"00{n}Z")
        provider.expire_before(4)

        await relay.drain(CHANNEL)

        gaps = await runtime.events.read("app.salesforce.gap", after=None, limit=10)
        assert len(gaps) == 1
        assert gaps[0].payload["expired_replay_id"] == "1"

    async def test_reading_resumes_after_a_gap(self, relaying) -> None:
        runtime, provider, relay = relaying
        provider.publish(Id="001A")
        await relay.drain(CHANNEL)
        for n in range(5):
            provider.publish(Id=f"00{n}Z")
        provider.expire_before(4)

        await relay.drain(CHANNEL)
        provider.publish(Id="last")
        await relay.drain(CHANNEL)

        rows = await runtime.events.read(SF_TOPIC, after=None, limit=20)
        assert rows[-1].payload["Id"] == "last"

    async def test_downstream_cannot_tell_it_was_a_pull(self, relaying) -> None:
        """A workflow reads `app.salesforce.*` exactly as it reads
        `app.shopify.*` — which is the whole convergence claim."""
        from loom import workflow
        from loom.events import EventDispatcher

        runtime, provider, relay = relaying
        seen: list[str] = []

        @workflow(name="sync_account")
        async def sync_account(ctx: Any, account: dict) -> str:
            seen.append(account["Id"])
            return "ok"

        runtime.register(sync_account)
        dispatcher = EventDispatcher(runtime)
        await dispatcher.subscribe(Subscription("crm", SF_TOPIC, "sync_account"))

        provider.publish(Id="001A")
        await relay.drain(CHANNEL)
        await dispatcher.poll_once()
        import asyncio

        for _ in range(50):
            await asyncio.sleep(0)

        assert seen == ["001A"]

    async def test_the_operability_surface_applies_unchanged(
        self, relaying
    ) -> None:
        """Subscriptions, lag, replay — a shape-C source gets all of it for
        free, which is the test of whether the seams are in the right place."""
        from loom.events.manager import SubscriptionManager

        runtime, provider, relay = relaying
        marks = StoreBackedCheckpoints(runtime.store)
        manager = SubscriptionManager(
            runtime.store, log=runtime.events, checkpoints=marks
        )
        await manager.add(Subscription("crm", SF_TOPIC, "sync_account"))
        for n in range(4):
            provider.publish(Id=f"00{n}")
        await relay.drain(CHANNEL)

        (row,) = await manager.health()
        assert row.lag == 4

        plan = await manager.replay("crm", SF_TOPIC, max_events=2)
        assert plan.events == 2 and plan.truncated


# ---------------------------------------------------------------------------
# Watch renewal, as the guide writes it
# ---------------------------------------------------------------------------


class ShopifyWatch:
    id = "shopify"

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.expires_at: Any = None

    async def register(self, resource: str) -> WatchRegistration:
        self.registered.append(resource)
        return WatchRegistration(
            resource=resource,
            expires_at=self.expires_at,
            metadata=lifetime_hint(3 * 24 * 3600),
        )

    async def stop(self, resource: str) -> None:
        pass


class TestWatchFromTheGuide:
    def test_it_satisfies_the_protocol(self) -> None:
        assert isinstance(ShopifyWatch(), Watch)

    async def test_a_declared_lifetime_changes_when_renewal_happens(self) -> None:
        """Without `lifetime_hint` the renewer assumes a week, which for a
        three-day subscription means renewing after it died."""
        from datetime import UTC, datetime, timedelta

        class Frozen:
            def __init__(self) -> None:
                self.at = datetime(2026, 1, 1, tzinfo=UTC)

            def now(self) -> datetime:
                return self.at

        clock = Frozen()
        watch = ShopifyWatch()
        watch.expires_at = clock.at + timedelta(days=3)
        renewer = WatchRenewer(watch, clock=clock)
        renewer.track("orders/create")
        await renewer.sweep()

        clock.at += timedelta(days=2)
        assert await renewer.sweep() == ["orders/create"], (
            "two days into a declared three-day lifetime is past the halfway "
            "mark and therefore due"
        )


# ---------------------------------------------------------------------------
# The acid test
# ---------------------------------------------------------------------------


def test_nothing_here_reaches_past_a_published_seam() -> None:
    """Salesforce is out of the package on purpose: if it cannot be built by
    somebody who is not us, the seams are wrong. A private import or a
    monkey-patch here would mean this file proved the opposite of what it
    claims — so it greps itself, the way `test_host_integration.py` does.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    # Everything above this function, which is the guide's code. Scanning the
    # whole file would match this check's own predicate — a self-grep has to
    # exclude itself or it always fails, which is a passing test's opposite.
    body = source.split("def test_nothing_here_reaches_past_a_published_seam")[0]
    offenders = [
        line.strip()
        for line in body.splitlines()
        if ("import" in line and "loom." in line and "._" in line)
        or "runtime._" in line
        or "ingress._" in line
        or "monkeypatch" in line
    ]
    assert not offenders, (
        "this guide's code reached past a published seam, which means the seam "
        f"is not finished: {offenders}"
    )
