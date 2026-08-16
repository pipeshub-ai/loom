# Adding an event source

<!-- docs-preamble -->

Every example on this page assumes:

```python
from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping, Sequence
from typing import Any

from loom import Runtime
from loom.events import (
    Challenge,
    EventRecord,
    InboundEvent,
    SourceContext,
    SourceState,
    StoreBackedEventLog,
    VerificationFailed,
    WatchRegistration,
    WatchRenewer,
    WebhookIngress,
    lifetime_hint,
    register_event_source,
    topic_for,
)
from loom.stores.memory import MemoryStore
from loom.testing.conformance import verify_event_source

_store = MemoryStore()
rt = Runtime(store=_store, events=StoreBackedEventLog(_store))
ingress = WebhookIngress(rt)
client: Any = None
request: Any = None

# Defined in the blocks below. Declared here so that each block on this page
# resolves on its own — which is what the docs check enforces, and the reason
# no snippet here is a fragment.
#
# Stand-in *classes* rather than `None`, because the blocks below construct
# them: `None("shhh")` resolves every name and then raises the moment the page
# is executed, which is the other half of what the docs check is for.
class ShopifySource:
    id = "shopify"

    def __init__(self, secret: str) -> None:
        self._secret = secret


class SalesforceRelay:
    def __init__(self, client: Any, *, log: Any, state: Any) -> None:
        self._client, self._log, self._state = client, log, state

    async def drain(self, channel: str) -> bool:
        return False


def _shopify_signature(body: bytes) -> str:
    import base64

    return base64.b64encode(
        hmac.new(b"shhh", body, hashlib.sha256).digest()
    ).decode()
```

A **source** is how the outside world gets into LOOM. This guide builds one end
to end and then builds a harder one — Salesforce, which does not push at all —
using nothing that LOOM does not publish.

That second half is the point. Salesforce is deliberately **not** in the
package: if it cannot be built by somebody who is not us, on the seams as
shipped, then the seams are wrong, and building it in-tree would hide that. If
you find yourself needing to patch LOOM to finish a source, that is a bug — file
it.

Every snippet here executes in CI (`tests/test_event_source_guide.py`), so what
follows is the code, not a paraphrase of it.

---

## The four shapes

Sources differ more than they look. What matters is not the vendor, it is how
the event reaches you:

| Shape | Who calls whom | Payload | Examples |
|---|---|---|---|
| **A — push-data** | they call you | the event | Slack, Jira, Stripe, GitHub, Shopify |
| **B — push-pointer** | they call you | a *position* | Gmail, Graph delta |
| **C — pull-log** | you call them | the event | Salesforce Pub/Sub, Kafka |
| **D — poll-diff** | you call them | whatever changed since | anything with no notifications |

All four converge, because the internal representation is a log with positions.
A and B arrive through `WebhookIngress`; C and D append directly. Downstream,
nothing can tell them apart — a workflow subscribing to `app.shopify.order` does
not know whether Shopify pushed it or a poller found it.

---

## Shape A: a push-data source

Four methods. A provider with no handshake writes `return None` rather than
re-implementing a dispatch loop.

```python
class ShopifySource:
    """Shopify webhooks. HMAC-SHA256, base64, in X-Shopify-Hmac-Sha256."""

    id = "shopify"

    def __init__(self, secret: str | None = None) -> None:
        self._secret = secret or os.getenv("SHOPIFY_WEBHOOK_SECRET", "")

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        import base64

        if not self._secret:
            raise VerificationFailed(
                "no Shopify webhook secret configured, so this delivery cannot "
                "be verified. Set SHOPIFY_WEBHOOK_SECRET."
            )
        digest = hmac.new(self._secret.encode(), body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode()
        if not hmac.compare_digest(expected, headers.get("x-shopify-hmac-sha256", "")):
            raise VerificationFailed("Shopify signature mismatch")

    def challenge(self, headers: Mapping[str, str], body: bytes) -> Challenge | None:
        return None

    def delivery_id(self, headers: Mapping[str, str], payload: Any) -> str | None:
        return headers.get("x-shopify-webhook-id")

    async def expand(
        self, payload: Any, ctx: SourceContext
    ) -> Sequence[InboundEvent]:
        # Shopify puts the event type in a header, not the body — which is why
        # `expand` is handed the headers rather than only the payload.
        topic = ctx.headers.get("x-shopify-topic", "unknown").replace("/", "_")
        return [
            InboundEvent(
                type=f"shopify.{topic}",
                payload=payload if isinstance(payload, dict) else {},
                key=str(payload.get("id", "")) if isinstance(payload, dict) else "",
            )
        ]
```

Four things are the source's, and four are not.

**Yours:** is this authentic, is it a handshake, what does the provider call
this delivery, and what happened.

**Not yours:** the event id, the topic, deduplication, and who cares. Those
belong to the ingress and the dispatcher, and a source that constructed an event
id would have to know the topic naming scheme — so every source would then
encode it separately, and they would drift.

### Verify over the bytes

`verify` is handed the **raw body** because every scheme in use signs bytes. A
source that parses first and verifies the re-serialised form accepts anything —
and it passes every hand-written test, because a JSON round trip is lossless in
the happy case. The conformance kit exists for exactly this.

### Register it

```python
register_event_source(ShopifySource("shhh"))
```

Or ship it in a package and let the entry point do it, so nobody has to call
anything:

```toml
[project.entry-points.loom_event_source]
shopify = "acme_shopify.source:ShopifySource"
```

Nothing in LOOM names Shopify. `rt.sources` chains to the process-global
registry, so an entry point reaches every Runtime while
`rt.sources.register(...)` stays local to one — the same rule `toolsets` and
`nodes` already follow.

### Prove it

```python
async def test_my_source() -> None:
    await verify_event_source(
        ShopifySource("shhh"),
        sign=lambda body: {"x-shopify-hmac-sha256": _shopify_signature(body)},
        sample=b'{"id": 12345, "email": "a@b.com"}',
        expected_types=["shopify.orders_create"],
    )
```

The kit checks the parts an author would not think to test: that verification is
over the bytes, that a tampered body is rejected, that a delivery with no
signature at all is refused, that the delivery id is stable, and that expansion
is pure. Whether your parser reads the right field is your test.

### Serve it

Nothing further. `POST /hooks/shopify` works the moment the source is
registered, and a host with its own gateway calls the same code:

```python
async def handle(request: Any) -> Any:
    return await ingress.receive("shopify", request.headers, await request.body())
```

`WebhookIngress` is transport-free by construction, so a Lambda, a Cloud Run
handler and a Django view all get identical behaviour, and a verification bug
fixed once is fixed everywhere.

---

## Shape C: Salesforce, which never calls you

Salesforce Pub/Sub is a gRPC **subscribe** API. There is no webhook to receive,
so there is no `EventSource` at all — it is a producer that appends to the log
directly.

This is the interesting case, because it is where the "one resume primitive"
claim gets tested. Salesforce hands you a **replay ID** per event and retains
events for 72 hours. That replay ID *is* our checkpoint, one hop upstream:

```
Salesforce's log  --replay id-->  our log  --position-->  subscribers
```

Nothing new is needed. The replay ID goes in `SourceState`, which rides
`CacheStore` and therefore works on memory, SQLite, Postgres and Mongo without a
migration.

```python
class ReplayIdExpired(Exception):
    """Salesforce no longer holds that replay id."""


class SalesforceRelay:
    """Reads Salesforce's log from our replay id and appends to ours.

    A producer, not an `EventSource`: nothing calls in, so there is no delivery
    to verify. The one thing it must get right is the ordering — append first,
    advance the replay id last — because a replay id stored before the append
    is an event nobody will ever see again.
    """

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
            # 72 hours, and past it Salesforce cannot say what was missed.
            # Recording the gap is the whole difference between "nothing
            # happened" and "we lost a day" — silently resuming from now makes
            # those two indistinguishable.
            await self._record_gap(channel, replay_id)
            batch = await self._client.fetch(channel, None, limit)

        if not batch.events:
            return 0

        records = [
            EventRecord(
                # The replay id is the provider's own delivery id, so a re-read
                # after a crash produces identical event ids and appends
                # nothing twice.
                event_id=f"{topic_for(f'salesforce.{channel}')}/salesforce:{e['replayId']}",
                type=f"salesforce.{channel}",
                payload=e["payload"],
                key=str(e["payload"].get("Id", "")),
                source="salesforce",
            )
            for e in batch.events
        ]
        await self._log.append(topic_for(f"salesforce.{channel}"), records)

        # Last. The events are durable; a crash here costs one repeated read,
        # which the event ids above deduplicate away.
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
```

Run it on the Runtime's own supervisor so `shutdown()` stops it:

```python
class LoopingRelay:
    """Anything with `start()` and `stop()` — `supervise` asks for no more, so
    `rt.shutdown()` stops this without knowing what it is."""

    def __init__(self, relay: SalesforceRelay, channel: str) -> None:
        self._relay, self._channel, self._task = relay, channel, None

    async def start(self) -> None:
        import asyncio

        async def loop() -> None:
            while True:
                if not await self._relay.drain(self._channel):
                    await asyncio.sleep(1.0)

        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()


relay = SalesforceRelay(
    client, log=rt.events, state=SourceState(rt.store, "salesforce")
)
rt.supervise(LoopingRelay(relay, "AccountChangeEvent"))
```

And that is the whole integration. Everything else already applies:
subscriptions, filters, per-subscriber checkpoints, resume, dead-lettering,
`loom events tail`, `loom events status`, bounded replay. A workflow reads
`app.salesforce.AccountChangeEvent` and cannot tell that Salesforce is pull and
Slack is push.

---

## Shape B, in one paragraph

If your provider sends a *position* rather than data — Gmail's `historyId`, a
Graph delta token — write the `EventSource` so `expand` returns **one pointer
event**, then implement
[`Reconciler`](../seams/reconciler.md) to turn a pointer into data events. A
`PointerReconciler` drives it: it is an ordinary subscriber with an ordinary
checkpoint, so it resumes where it stopped, and a `CursorExpired` becomes a
`*.gap` event rather than a silent jump to now. `loom.toolsets.google.gmail.source`
is the shipped worked example.

Fetching inside `expand` is the mistake to avoid: it puts an API round trip
inside whatever budget the provider allows before it retries, so a slow mailbox
produces duplicate notifications *as well as* a slow response.

---

## Keeping the subscription alive

The highest-severity failure in this whole subsystem is silent. Gmail's watch
dies after seven days; Graph subscriptions last about three. When one lapses no
events arrive, nothing errors, and the workflow looks *idle* rather than broken.

If your provider's subscription expires, implement
[`Watch`](../seams/watch.md) and hand it to a `WatchRenewer`:

```python
class ShopifyWatch:
    id = "shopify"

    async def register(self, resource: str) -> WatchRegistration:
        result = await client.create_webhook(resource)
        return WatchRegistration(
            resource=resource,
            expires_at=result.expires_at,
            # Declare it. Without this the renewer assumes a week, which for a
            # three-day subscription means renewing after it died.
            metadata=lifetime_hint(3 * 24 * 3600),
        )

    async def stop(self, resource: str) -> None:
        await client.delete_webhook(resource)
```

Three things it does that are easy to get wrong alone: it renews at a *fraction*
of the lifetime so several consecutive failures are survivable; it sweeps
immediately on start, because a process down for a day comes back to
subscriptions that expired while it was gone; and it appends a
`*.watch_lapsed` event once one is actually dead — once, not once per retry, so
the alert stays worth reading.

`register` must be **idempotent per resource**. Renewal calls it again on a live
subscription, and a provider that creates a second one instead of extending the
first turns a daily renewal into a fan-out of duplicate notifications.

---

## Checklist

- [ ] `verify` reads the **raw body**, and refuses a delivery with no signature
      unless an explicit flag says a gateway does it.
- [ ] `delivery_id` returns the provider's own id, and `None` — not a guess —
      when it publishes none.
- [ ] Event types are namespaced `{source}.{event_type}`; the topic derives
      from it.
- [ ] `key` is whatever must stay ordered: a channel, a mailbox, a record id.
- [ ] Nothing is fetched inside `expand`.
- [ ] `verify_event_source` passes.
- [ ] If the provider's cursor can expire, a `*.gap` event is appended rather
      than resuming silently.
- [ ] If the provider's subscription can expire, a `Watch` is registered with a
      `lifetime_hint`.
