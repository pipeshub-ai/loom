# The event backbone — a durable log, pub/sub fan-out, and resume

Supersedes the transport half of `event-triggers.md`. That document treated the
webhook as the system; this one treats it as **one producer among many**. The
research and provider notes there still stand — this is the architecture they
should have fed into.

Design principle, stated once and used to settle every argument below:

> **Durability and resume come first.** Every component is judged by what
> happens when it is killed mid-flight and started again. A design that is
> merely correct while running is not a design.

---

## 1. Why a log, not a bus

The requirement is "many workflows listen to the same event and each does
something different", plus resume. Those two together rule out a fire-and-forget
bus, and they rule it out for a specific reason:

| | Bus (fan-out at publish) | **Log (fan-out at read)** |
|---|---|---|
| Subscriber down for an hour | events missed, permanently | reads from its checkpoint, catches up |
| New subscriber added today | sees nothing from yesterday | can start at `EARLIEST` and backfill |
| One slow subscriber | back-pressures everyone, or is dropped | has its own cursor; nobody waits |
| Reprocess after a bug fix | producer must resend | reset one checkpoint |
| "Where are we?" | unanswerable | a position, per subscriber |
| Audit "what did the world tell us?" | gone | the log *is* the record |

A bus makes delivery the durable act. A log makes **the record** the durable act
and delivery a derived, resumable read. Only the second survives the "kill it and
start it again" test, which is why every system that takes durability seriously —
Kafka, Redis Streams, and Salesforce's own event bus with its replay IDs —
converged on it.

This also matches LOOM's existing grain exactly. The journal already makes a
*run* resumable by recording what happened and replaying from the record. The
event log is the same idea one level out: make the *world's input* resumable by
recording it and reading from a position. Two logs, same principle, joined at one
seam (§4).

---

## 2. Six apps, and the four shapes they force

Chosen to be maximally unalike rather than representative. Verified against
vendor documentation; sources at the end.

| App | Delivery | Payload | Resume primitive | Subscription lifetime | Ordering | Provider-side filter |
|---|---|---|---|---|---|---|
| **Slack** | push | full event | none — `event_id` dedupe only | permanent | none | event type only |
| **Jira** | push | full event | none, and no stable delivery id | permanent | none | **JQL** |
| **Gmail** | push via Pub/Sub | **pointer** (`historyId`) | history cursor, *can expire* | **watch dies in 7 days** | per mailbox | `labelIds` |
| **Salesforce** | **pull** (gRPC) | full event | **replay ID, 72h retention**, client- or server-committed | permanent | per channel | channel |
| **MS Graph** | push | data *or* pointer | delta token | **~3 days, with lifecycle events** | per resource | resource path |
| **Stripe** | push | full event | none; official **resend** API | permanent | **not guaranteed** | event types |

### 2.1 Four shapes

- **A — push-data.** Slack, Jira, Stripe, GitHub. The payload is the event.
- **B — push-pointer.** Gmail, Graph delta. The payload is a *position*; the
  event must be reconstructed by asking the provider what changed.
- **C — pull-log.** Salesforce Pub/Sub, and Kafka itself. *They* hold a durable
  log and hand you a replay ID; you hold the position.
- **D — poll-diff.** Anything with no notification at all. You diff on a
  schedule.

### 2.2 The fifth class everybody forgets: control events

Microsoft Graph does not only send data. It sends **lifecycle notifications**:
`reauthorizationRequired`, `subscriptionRemoved`, and — the important one —
**`missed`**, meaning *"there were events you did not get."* Gmail's `watch()`
expires after 7 days. OAuth tokens expire on their own schedule, which Graph's
docs point out is *not* the subscription's schedule.

This is the highest-severity failure in the whole system and it is silent: a
subscription lapses, no events arrive, nothing errors, and the workflow looks
idle rather than broken. **Absence of events is indistinguishable from absence of
activity** unless something is watching for it.

So control events are first-class (§6), not an operational footnote.

### 2.3 The claim that makes one architecture cover all five

All four data shapes converge **if the internal representation is a log with
positions**:

- **A** — ingress verifies and appends. Done.
- **B** — ingress appends the *pointer* event. A reconciler subscriber consumes
  it, asks the provider what changed, and appends the resulting **data** events
  back to the log. Downstream subscribers never learn that Gmail is different.
- **C** — a source connector reads the provider's log from its replay ID and
  appends to ours. **The provider's replay ID is simply our checkpoint for that
  source** — the same primitive, one hop upstream.
- **D** — a poller diffs and appends.
- **Control** — appended to a control topic; the subscription manager is just
  another subscriber.

One log, five producers, one resume primitive. That is the whole design.

---

## 3. The ports — and what the package refuses to ship

**Constraint, taken as binding:** the published package contains no code specific
to any database or event bus. LOOM maintains a *contract*, not a fleet of
adapters. Every adapter shipped is one that must be tested against a real server
forever, and that cost compounds; four brokers and four databases is sixteen
integration matrices nobody funds in year three.

So: **one port pair, one reference implementation that adds no backend-specific
code, one conformance kit. Zero shipped brokers.**

```py-sketch
class EventLog(Protocol):
    """An append-only, resumable record of what the world said."""

    async def append(
        self, topic: str, records: Sequence[EventRecord], *, key: str = ""
    ) -> Sequence[Position]:
        """Durably record, returning each record's position.

        Records sharing a `key` are read back in append order. Records with
        different keys carry no ordering promise -- which is what lets a backend
        partition, and is the only promise every backend can actually keep."""

    async def read(
        self, topic: str, *, after: Position | None, limit: int
    ) -> Sequence[StoredEvent]:
        """Records this reader has not seen, given `after`. `None` starts at the
        beginning of what is retained."""

    async def head(self, topic: str) -> Position | None:
        """The newest position, for a subscriber starting at LATEST."""

    async def retain(self, topic: str, policy: RetentionPolicy) -> int:
        """Discard what the policy allows. The *adapter* decides how -- a
        Kafka adapter may do nothing because the broker already has a retention
        setting, and saying so is better than pretending to control it."""


class Checkpoints(Protocol):
    """Where each subscriber has read to. The resume primitive."""

    async def commit(self, subscriber: str, topic: str, position: Position) -> None: ...
    async def load(self, subscriber: str, topic: str) -> Position | None: ...
    async def active(self, topic: str) -> Mapping[str, Checkpoint]: ...
```

### 3.1 `Position` is opaque and *not* totally ordered

An earlier draft required a total order so that retention could take a position.
That was wrong, and it would have quietly excluded every partitioned backend: a
Kafka topic's position is a set of per-partition offsets with no meaningful total
order, and forcing one collapses the topic to a single partition — capping
throughput permanently, in the name of an ordering nobody asked for.

The contract is therefore weaker and honest:

- `Position` is **opaque**. Callers may store it and hand it back to `read`.
  They may not compare, parse, or do arithmetic on it.
- `read(after=P)` returns records this reader has not yet seen. That is the
  entire promise, and it is exactly what resume needs.
- Ordering is **per key**, never per topic.
- Retention is expressed as a *policy*, not a position, because only the adapter
  knows what it can enforce.

### 3.2 `topic` is a routing key, not a physical resource

`app.slack.message` is a hierarchical name. Whether it becomes a Kafka topic, a
Redis stream, a column value, or a header is the **adapter's** decision — which
is precisely where a backend-specific trade-off belongs. A Kafka adapter is free
to multiplex a thousand logical topics onto one physical topic and filter on a
header, because per-topic cost is a Kafka fact and not a LOOM fact.

This dissolves the "topic granularity" question rather than answering it: the
core always uses the fine-grained name, and the adapter coarsens if its backend
charges for topics.

### 3.3 The one implementation that ships, and why it needs no new backend code

`StoreBackedEventLog` is built **only** on capabilities every store already
implements — `CacheStore` (`get`/`set`) and `LockProvider` — which is the same
move `ctx.state` already made, for the same stated reason:

> Chosen so that ``ctx.state`` needs no new infrastructure — whatever backs the
> journal backs this, which for a laptop is one SQLite file and for production is
> the database already in the deployment.
> — `runtime/state.py::StoreBackedState`

A topic is `head:{topic}` plus `rec:{topic}:{seq}`. An append takes the topic's
lock, reserves the next sequence, writes the records, advances the head, and
releases. Reads are direct key lookups from `after`.

So it runs on **memory, SQLite, Postgres and Mongo the day it lands**, with no
new store methods, no migrations, and nothing added to `stores/`. That is the
whole of the durable default.

Its ceiling is honest and documented: one lock acquisition per append batch
serialises writers per topic. That is ample for provider webhooks and for
`ctx.publish`; it is not a firehose. **A host that outgrows it supplies an
adapter — which is the moment they should be choosing Kafka anyway, and by then
they know why.**

### 3.4 The conformance kit is the deliverable, not the adapters

`loom.testing.eventlog_conformance` ships as an importable suite:

```py-sketch
from loom.testing import eventlog_conformance

def test_my_redis_log():
    eventlog_conformance.verify(lambda: MyRedisStreamsLog(url), anyio_backend="asyncio")
```

It asserts the contract that actually matters, including the parts an adapter
author would not think to test:

- `read(after=P)` never returns a record twice and never skips one;
- resume across a simulated crash mid-`append` leaves no permanent hole
  (a reserved-but-unwritten sequence must be recoverable, not a stall);
- records sharing a key come back in append order;
- records with different keys have no ordering asserted — so an adapter is not
  accidentally held to a promise the port does not make;
- `head()` on an empty topic is `None`, not an error;
- concurrent appends from two writers produce no lost record and no duplicate
  position.

**This is the long-term maintainable trade.** LOOM maintains one protocol and one
suite; a Kafka adapter is fifty lines the host writes and proves in one call.
Contrast: LOOM shipping the adapter means LOOM owns a Kafka test cluster in CI
for the life of the project.

Documentation carries worked examples for Redis Streams and Postgres `LISTEN`
— *as documentation*, exercised by the existing docs-examples check, not as
importable modules with a support burden.

## 4. Durability and resume: where every sync point lives

### 4.1 One rule, applied at every hop

> **Each hop owns exactly one cursor, and commits it only after the next hop's
> write is durable.**

A chain of at-least-once links, each of whose effects is idempotent, composes to
exactly-once *effects*. Nothing anywhere claims exactly-once *delivery*, because
nothing can.

```
world ──▶ ingress ──▶ [EVENT LOG] ──▶ subscriber ──▶ run (journal)
            │             ▲              │             ▲
      provider cursor     │        checkpoint          │
      (replay id,         └── durable       └── committed after submit
       historyId)             append              is durable
```

### 4.2 The three cursors, and what each protects

| Cursor | Owner | Committed after | If committed too early | If committed too late |
|---|---|---|---|---|
| Provider cursor (`historyId`, replay ID, delta token) | source connector | the `append` returns | **events lost** — provider will never resend | re-read, re-append, deduped by `event_id` |
| Checkpoint | subscriber | `submit()`/`send_event()` durable | **runs never created** | events re-read, deduped by idempotency key |
| Journal | the run | — | — | — (already solved) |

Every "too early" column is data loss; every "too late" column is rework. That
asymmetry is why the rule is *commit last*, everywhere, without exception.

### 4.3 What makes the rework harmless

Two keys, both derived, never random:

- **Event identity** — `event_id = f"{source}:{provider_delivery_id}"`, or a
  content hash where the provider gives no id (Jira). Re-appending the same
  event is a no-op via a unique index on `(topic, event_id)`.
- **Dispatch identity** — `idempotency_key = f"{event_id}:{subscriber}"`, so N
  subscribers each get exactly one run and a redelivery gives none of them a
  second. This is `Runtime.submit(idempotency_key=)`, which is already checked
  *before admission*, so a redelivery does not even consume a rate-limit slot.

The subscriber suffix is the part that is easy to get wrong: keying on the event
alone means the second subscriber's run is silently deduped away against the
first's.

### 4.4 Retention cannot outrun the slowest reader

`truncate_before` refuses to pass `Checkpoints.slowest(topic)`. Dropping records
a live subscriber has not read is silent, permanent data loss that appears as
"that workflow just never ran". A subscriber abandoned forever must therefore be
*explicitly retired* rather than merely ignored — and until it is, the log grows,
loudly, which is the right way round.

### 4.5 Resume in practice

A process restarts: each subscriber loads its checkpoint and reads on. No
coordination, no replay of anything already handled, and a subscriber that was
down for a day catches up at its own pace while the others are unaffected.

A subscriber added tomorrow chooses where to begin:

```py-sketch
Subscribe(topic="app.slack", start_at=StartAt.EARLIEST)   # backfill everything retained
Subscribe(topic="app.slack", start_at=StartAt.LATEST)     # only new events
Subscribe(topic="app.slack", start_at="2026-08-01T00:00Z")
```

`EARLIEST` is the capability a bus structurally cannot offer, and it is the
difference between "we can add that workflow" and "we can add that workflow, and
it will process last week too."

---

## 5. Fan-out and filtering

```py-sketch
@workflow(triggers=[
    OnAppEvent(
        "slack.message",
        where=Filter(channel_id="C024BE91L", thread_ts={"$exists": False}),
        start_at=StartAt.LATEST,
    ),
])
async def triage(ctx: Context, message: SlackMessage) -> str: ...
```

Three placements, and the design uses all three because they cost different
amounts:

| Layer | Cost of a rejected event | Expressiveness | Example |
|---|---|---|---|
| **Provider-side** | zero — never sent | poor, and configured out of band | Jira JQL, Gmail `labelIds` |
| **Topic** | one log read | coarse | one topic per `source.event_type` |
| **Subscriber filter** | one read + one predicate | arbitrary, versioned with the code | `Filter(channel_id=…)` |

Topic granularity is the load-bearing middle: `app.slack.message` rather than
`app.slack` means a workflow interested only in messages never reads reactions.
Too fine and you get thousands of topics; the rule is **one topic per
`{source}.{event_type}`**, which is what a subscriber actually declares anyway.

### 5.1 Filtering has the id-versus-name problem

Slack's payload carries `"channel": "C024BE91L"`. A filter written as
`channel="tech"` matches **nothing, forever, with no error** — the same failure
`resolves=` exists to prevent in the toolsets, reappearing one layer up.

Two mitigations, and both are worth having:

1. **Normalize to carry both.** The source adapter emits
   `channel: {"id": "C024BE91L", "name": "tech"}`, so `channel.name == "tech"`
   works naturally. (This is what pipeshub does, and it is the friendlier half.)
2. **Resolve at registration.** `where=SlackChannel("#tech")` runs the Slack
   toolset's own `slack_find_channel` **once, when the subscription is
   registered**, and freezes the id in. A misspelled channel then fails at
   registration, loudly, rather than at runtime, never.

Name matching alone is not enough: a channel rename silently stops the workflow.
So normalization makes filters *writable*, and registration-time resolution makes
them *correct*; the docs recommend the second for anything that matters.

### 5.2 One evaluator, used twice

The predicate evaluator is shared between the subscription registry's
*validation* and the dispatcher's *matching*. Two evaluators means a filter that
validates and then does not match — pipeshub hit exactly this, where a filter
written as `channel="C123"` could not match a normalized `channel: {id, name}`
object. `FilterSpec` (which already exists, §1.2 of the previous doc) is that
evaluator; it needs no changes beyond being wired in.

---

## 6. Subscription lifecycle — the silent-failure class

Everything above assumes events keep arriving. §2.2 is the reason that assumption
needs its own machinery.

**Registry.** Every provider-side subscription is a record: source, resource,
expiry, cursor, health. Not a config file — a row, because it has state that
changes.

**Renewal.** A leased periodic task renews anything nearing expiry. LOOM already
has exactly this: `start_scheduler(dispatcher=…, elector=…)` gives single-leader
periodic work with a lease. Renewal cadence is a *fraction* of the lifetime —
daily against Gmail's 7 days — so several consecutive failures are survivable.

**Gap detection, three ways.** This is the part that turns a silent failure into
a loud one:

1. **The provider says so.** Graph's `missed` lifecycle notification, or a
   `reauthorizationRequired`. Append to the control topic.
2. **The cursor is too old.** Gmail expires history; Salesforce retains 72 hours.
   A cursor the provider will no longer honour means *we do not know what we
   missed*. Reset to the current position, emit a **`*.gap` event**, and let a
   reconciliation subscriber decide what to do. Silently jumping to now is the
   failure mode where "no email arrived today" is indistinguishable from "we lost
   a day of email".
3. **Nothing arrived.** A heartbeat per subscription: if a source that normally
   sees traffic goes quiet for longer than its expected interval, that is a
   *symptom*, and it should surface in `loom hooks status` rather than be
   discovered by a human wondering why nobody was paged.

**Gaps are events, not exceptions.** `slack.gap`, `gmail.gap` are appended to the
log like anything else, so a workflow can subscribe to "we lost visibility" and
do something about it. That falls straight out of having a log and would be
awkward in any other shape.

---

## 7. Loops, poison, and backpressure

**Loops.** A workflow can publish (`ctx.publish`) an event that triggers a
workflow that publishes… Every envelope carries `chain_depth`, incremented per
hop, capped (5). Past the cap the event is recorded and dropped with a warning.
Without this the first cycle anybody writes takes the system down, and it will be
written — this is exactly the risk of unifying `ctx.publish` with external
events, which §9 does.

**Poison.** A subscriber whose handler fails must not block its own progress
forever, and must not skip silently. Bounded attempts, then the record goes to a
dead-letter topic and the checkpoint advances — so one bad event costs one event,
not the stream. A dead-letter is a real topic, so it is itself subscribable and
inspectable.

**Backpressure.** Per-subscriber cursors mean a slow subscriber slows only
itself. What it *can* exhaust is the runtime, so dispatch goes through the
existing `AdmissionController` — and because admission is evaluated before the
record is created, a rejected dispatch leaves no run behind and the checkpoint
simply does not advance. It retries. That is the correct behaviour and it is
already built.

---

## 8. Security, carried over and extended

- **Raw bytes** to signature verification, always.
- **Order: verify → rate-limit → dedupe → append.** Rate limiting *after*
  verification, so an unsigned request cannot consume a tenant's budget.
- **Tenant resolved server-side** from the registered endpoint, never from a
  request header — otherwise one caller publishes into another tenant's
  workflows.
- **Dedupe keys scoped by tenant.** Provider event ids are unique per install,
  not globally; a global key silently drops the second tenant's copy of the same
  `event_id`.
- **Log records are untrusted input.** A run started from an external event
  starts tainted (`TaintBroker`), so its first write or destructive call needs a
  human.

The middle two are lifted directly from pipeshub's ingress, which had already
reasoned them through.

---

## 9. What this changes in LOOM

| Piece | Today | After |
|---|---|---|
| `ctx.publish` | wakes parked runs only; cannot start a workflow | appends to the log — same path as an external event |
| `EventRouter` | a stub that returns names | the dispatcher: read → filter → submit → commit |
| `Webhook` | declared, unserved | one producer into the log |
| `OnEvent` (queue) | its own consumer | a `Subscribe` over a log whose backend is the broker |
| `Poll` | declared, undriven | a producer; also Gmail's fallback |
| `FilterSpec` | wired to nothing | the shared evaluator |
| `RetentionManager` | runs and blobs | plus log truncation, floored at the slowest checkpoint |

The pleasing part is how little is new: one port pair, one dispatcher, one
registry. `submit(idempotency_key=)`, `send_event(dedupe_key=)`,
`claim_event_delivery`, `LockProvider`, `AdmissionController`,
`start_scheduler(elector=…)` and `supervise()` all already exist and all do
exactly what this needs.

Unifying `ctx.publish` with external events is the change with the most reach: it
means a workflow emitting "invoice.approved" and Stripe emitting
`invoice.payment_succeeded` are the same kind of thing, subscribable the same
way — and it is why §7's loop cap is not optional.

---

## 10. Extensibility — building on LOOM without forking it

A third party must be able to add a **trigger source, an event-log adapter, a
node, a toolset, or a workflow** without editing a line of LOOM. That is a
requirement, not a nicety: an ecosystem that requires a pull request to add a
connector does not become an ecosystem.

LOOM already has the shape for this and the event backbone adopts it unchanged
rather than inventing a parallel one.

### 10.1 The five SOLID pressures, and where each lands

**Single responsibility.** Each piece answers one question, and the split is
along the lines the *failure modes* fall:

| Component | Its one question |
|---|---|
| `EventSource` | "is this really from the provider, and what happened?" |
| `EventLog` | "record it, and let me read from where I was" |
| `Checkpoints` | "where had this subscriber got to?" |
| `Dispatcher` | "who wants it, and did their run get created?" |
| `SubscriptionManager` | "is the provider still going to tell us?" |

`EventSource` verifying *and* deciding who cares would put a Slack signature and
a workflow's filter in one class, and neither would be testable without the
other.

**Open/closed.** New sources, adapters and nodes arrive by **registration**,
never by editing a `match` statement. LOOM already ships two entry-point groups
(`loom_toolset`, `loom_node`) with parent-chained registries; the backbone adds
`loom_event_source` in exactly the same shape:

```py-sketch
# in a third-party package's pyproject.toml
[project.entry-points.loom_event_source]
shopify = "acme_shopify.source:ShopifySource"
```

Nothing in LOOM names Shopify. The registry chains to the process-global one, so
`register_event_source(...)` and the entry point reach every `Runtime`, while
`rt.sources.register(...)` stays local — the identical rule
`ToolsetRegistry`/`NodeRegistry` already follow.

**Liskov.** This is what the conformance kits (§3.4) are *for*. A protocol
without an executable contract is a suggestion, and substitutability is exactly
the property that holds for the in-memory implementation and quietly fails for
the distributed one. Every port ships a kit: `EventLog`, `Checkpoints`,
`EventSource`. A third-party adapter proves itself in one call, and — more
importantly — LOOM cannot tighten a contract later without the kits failing
loudly in every downstream repo, which is the point.

**Interface segregation.** `EventLog` and `Checkpoints` are deliberately two
ports, not one. A Kafka adapter implements both (consumer groups already store
offsets); a Postgres adapter implements the log and takes the default
checkpoints; a read-only provider source (Salesforce) implements *neither* and is
a producer. Fusing them would force every adapter to implement storage it does
not own.

Likewise `EventSource` is four small methods (`verify`, `challenge`,
`delivery_id`, `expand`) rather than one `handle()`, so a source that has no
handshake writes `return None` instead of re-implementing a dispatch loop.

**Dependency inversion.** `loom.events` imports protocols and nothing else. The
composition happens at the edge — `Runtime(events=…, checkpoints=…)` — which is
already how `store`, `human`, `blobs`, `sandbox`, `broker` and `clock` are wired.
The event backbone adds two more constructor arguments to a pattern with nine.

### 10.2 What a third party actually writes

To add a new provider end to end — say Shopify — nobody touches LOOM:

```py-sketch
class ShopifySource:                       # implements EventSource
    id = "shopify"
    def verify(self, headers, body): ...   # HMAC-SHA256, base64, X-Shopify-Hmac-Sha256
    def challenge(self, headers, body): return None
    def delivery_id(self, headers, payload): return headers["x-shopify-webhook-id"]
    async def expand(self, payload, ctx):  # shape A: one event
        return [InboundEvent(type=f"shopify.{headers['x-shopify-topic']}", payload=payload)]
```

register it, and every existing capability applies without further work:
subscriptions, filters, dedupe, resume, backfill, gap events, dead-lettering,
`loom events tail`. **That is the test of whether the seams are in the right
place** — a new provider should cost a verifier and a normaliser, and nothing
else.

The same holds for the other extension axes, which already work this way:
`@register_node` for nodes, `loom_toolset` for integrations, `@workflow` for
workflows, and `TriggerSpec` subclassing for a trigger kind LOOM has never heard
of.

### 10.3 The rule that keeps it maintainable

**LOOM ships protocols, one reference implementation per protocol, and a
conformance kit. It does not ship the long tail.** The reference implementation
exists so `pip install loomflow` works with no infrastructure; the kit exists so
the long tail can be correct without being vendored. Every time the answer to
"should we add an adapter for X" is *no, but here is the kit*, the maintenance
surface stays flat while the ecosystem grows.

---

## 11. Phasing

| Phase | Delivers | Proves |
|---|---|---|
| **1** ✅ | `EventLog` + `Checkpoints` protocols, `StoreBackedEventLog`, conformance kits | resume: kill mid-read, restart, no loss and no duplicate |
| **2** ✅ | Dispatcher (read → filter → submit → commit), `OnAppEvent`, `start_at` | fan-out: two workflows, one event, independent cursors |
| **3** ✅ | `EventSource` protocol + registry + entry point; webhook ingress; Slack and Jira (shape A) | a provider costs a verifier and a normaliser |
| **4** ✅ | Gmail (shape B): pointer event, reconciler, watch renewal, gap events | the silent-failure class is loud |
| **5** ✅ | Subscription manager, dead-letter, `loom events` CLI | operability |
| **6** ✅ | Salesforce (shape C) as a *documented* third-party source | the extension story is real, not aspirational |

Phase 6 is deliberately outside the package: if Salesforce cannot be built on the
published seams by someone who is not us, the seams are wrong, and building it
in-tree would hide that.

---

## 12. Decisions

Every question the earlier drafts left open, with the reasoning that settled it.

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Topic granularity | **Dissolved.** `topic` is a hierarchical routing key; the adapter maps it to physical resources | Per-topic cost is a Kafka fact, not a LOOM fact. Core always uses the fine name; adapters coarsen |
| 2 | Ordering promise | **Per key only.** `Position` is opaque, not totally ordered | A total order excludes every partitioned backend, or collapses it to one partition and caps throughput forever |
| 3 | Where checkpoints live | **Separate port.** Default rides `CacheStore` (no new backend code); an adapter that has native offsets implements `Checkpoints` and wins | Same move `ctx.state` already made. Kafka keeps its consumer groups; Postgres does not have to invent them |
| 4 | Retention vs abandoned subscribers | **Two clocks.** Retain past `max(policy floor, slowest *active* checkpoint)`. A subscriber whose checkpoint has not moved in `subscriber_ttl` is **quarantined**, not retired — retention proceeds, and resuming it raises `GapDetected` naming what it missed | Neither silent data loss nor unbounded growth. The third option — pretend nothing happened — is the only unacceptable one |
| 5 | `OnEvent` vs `OnAppEvent` | **Both.** `OnEvent` stays; `QueueConsumer` becomes a *producer* into the log rather than a parallel path. Nothing is deprecated until the log path has parity | It works and is tested. Replacing a working path with an unproven one is how a rewrite eats a year |
| 6 | Webhook route shape | **Both.** Honour the published `/webhook{path}`; add `/hooks/{source}` for provider-typed sources | `Webhook.describe()` already advertises the first — that is a contract |
| 7 | `DirectSink` vs durable sink | **Dissolved.** The append *is* the durable accept; there is no sink choice | The log removed the question, which is the strongest sign it was the right shape |
| 8 | `loom hooks install` | **No.** Ship read-only `loom events status` (drift, expiry, lag). Provider registration stays in the host's deployment | Shipping it means owning N provider admin APIs forever, for a one-time operation |
| 9 | Which brokers ship | **None.** Protocols + one store-backed reference + conformance kits | Sixteen integration matrices is not a maintainable position in year three |

The pattern across 1, 2 and 7 is worth naming: **three of the nine were dissolved
rather than answered.** A question that keeps having two defensible answers is
usually a sign the abstraction is drawn in the wrong place, and moving the line
made the question stop existing.

---

## 13. The five deferred questions, settled

### 13.1 Tenancy — the host's, carried in the topic name

**Decision: LOOM is not multi-tenant. `topic` is an opaque hierarchical string,
and a multi-tenant host namespaces it.** A single-tenant deployment passes
`app.slack.message`; a product embedding LOOM passes `acme/app.slack.message`.

What makes this safe to defer is that **every derived key already contains the
topic**:

| Key | Shape | Inherits the namespace? |
|---|---|---|
| Event identity | `{topic}/{source}:{delivery_id}` | yes |
| Dispatch idempotency | `{event_id}#{subscriber}` | yes, via `event_id` |
| Checkpoint | `{subscriber}@{topic}` | yes |

Namespace the topic and all three are namespaced. Adding real multi-tenancy
later therefore changes **no persisted key and no envelope field** — which is
precisely the property that made this the deferrable option.

One correction this forces, and it is not cosmetic: **event identity must include
the topic.** The obvious `{source}:{delivery_id}` is wrong. Provider delivery ids
are unique per *install*, not globally, so two tenants each with their own Slack
app can emit the same `event_id` — and a global key silently drops the second
tenant's copy as a duplicate. Cross-tenant event loss, no error. (pipeshub hit
exactly this and scopes its dedupe by `org_id` for the same reason.)

---

### 13.2 Subscriber identity when a filter changes

**The problem, concretely.** Day 1:

```py-sketch
@workflow(name="triage", triggers=[
    OnAppEvent("slack.message", where=Filter(channel_id="C_SUPPORT")),
])
```

It runs for a month; its checkpoint sits at position 50,000. Day 30, someone
widens it:

```py-sketch
    OnAppEvent("slack.message", where=Filter(channel_id={"$in": ["C_SUPPORT", "C_SALES"]})),
```

| Option | Behaviour | Harm |
|---|---|---|
| **A — same subscriber, keep checkpoint** | #sales triaged from now on; the month of #sales already in the log is never seen | under-delivers if the intent was retroactive |
| **B — filter hash in the identity ⇒ new subscriber** | checkpoint resets; **every #support message from the past month re-fires** | a duplicate storm on every filter edit |

B is the tempting one — "the subscription changed, so it is a new subscription" —
and it is a trap. The dispatch key is `{event_id}#{subscriber}`; change the
subscriber identity and every historical event's key changes with it, so nothing
dedupes and a month of triage runs again.

**Recommendation: subscriber identity is a stable name and never includes the
filter.**

```py-sketch
# identity = workflow name, or an explicit id when a workflow has several
triggers=[
    OnAppEvent("slack.message", where=..., subscription="support"),
    OnAppEvent("slack.message", where=..., subscription="sales"),
]
```

That gives:

- **Default is forward-only** (option A), which is what "widen the filter" almost
  always means.
- **Retroactive is an explicit operation** — and it is *safe by construction*:

  ```
  loom events replay --subscriber triage --from 40000
  ```

  Because the dispatch key does not contain the filter, the #support events
  already handled re-derive their original key and dedupe away. **Only the
  newly-matching #sales events create runs.** The exact behaviour someone
  widening a filter retroactively wants, and it falls out of the key design
  rather than needing a diffing engine.

Two consequences worth stating: a workflow declaring two subscriptions **must**
give them explicit ids or they share a checkpoint; and *renaming* a workflow
creates a new identity, so its checkpoint is lost and it resumes at `LATEST` —
a gap, not a storm, but one that should warn rather than pass silently.

---

### 13.3 `EARLIEST` is a foot-gun, and idempotency does not defuse it

**Why the usual protection fails.** Everywhere else, replaying is safe because
the dispatch key dedupes. Backfill is the one case where it does not: a
**genuinely new subscriber has seen none of those events**, so every historical
event is legitimately new and every one fires.

What that means in practice:

| Backfill | What actually happens |
|---|---|
| A week of `#support` into a workflow that auto-replies | ~400 replies sent at once, many to threads resolved days ago |
| A month of `jira:issue_created` into a P1 pager | 30 pages fire simultaneously, at whatever hour the backfill runs |
| A quarter of `invoice.created` into a billing workflow | charges attempted against invoices already paid |

The log makes backfill *possible*, which is what makes it *dangerous*. A bus
never had this problem because it could not do the useful thing either.

**Recommendation — four guards, and no bare `EARLIEST`.**

1. **`start_at=LATEST` is the default, and unbounded `EARLIEST` is not
   declarable.** A subscription may declare a *bounded* start (`since="24h"`);
   "from the beginning of time" is an operational act, not a line in a workflow
   file, because its blast radius depends on data the author cannot see.

2. **Backfill is an explicit command with a required policy.**

   ```
   loom events replay --subscriber triage --since 7d \
       --max-events 1000 --rate 10/s
   ```

   Bounds are mandatory, not defaulted. An operator who has thought about
   `--max-events` has thought about the blast radius.

3. **`--dry-run` reports effects, not counts.** A count is not actionable; what
   an operator needs is *what it would do*. The effect classes are already
   declared on every toolset operation, so this is derivable:

   ```
   412 events would match (2026-08-01 .. 2026-08-08)
     → 412 runs of `triage`
     → 412 WRITE   slack.chat.postMessage
     →   6 WRITE   jira.issues.create
   ```

4. **Backfilled runs start tainted.** A backfilled run's input is by definition
   external data it did not bring with it — exactly `TaintBroker`'s rule — so the
   first write parks on a human unless `--allow-writes` is passed. This reuses
   machinery that exists rather than inventing a backfill-only safety mode, and
   it fails in the safe direction: 412 parked runs is loud and bulk-approvable
   via `loom pending`; 412 sent messages is not recallable.

---

### 13.4 Checkpoint commit cost — where it actually is

**Not where it looks.** Per batch the dispatcher does: one log read, N submits,
one checkpoint write. At N=100 the checkpoint is under 1% of the work; at N=1 it
is a third — but a topic delivering one event per poll is a topic whose
throughput nobody is worried about.

**The real cost is idle bookkeeping**, and it scales with
`subscribers × topics × poll_rate`, not with events:

| Deployment | Pairs | At 100 ms poll | Of which are events |
|---|---|---|---|
| 5 workflows, 3 topics | 15 | 150 reads/s | ~0 |
| 50 workflows, 20 topics | 1,000 | 10,000 reads/s | ~0 |

Ten thousand operations a second to discover that nothing happened is the
failure mode, and it arrives long before event volume does.

**Four mitigations, in order of value:**

1. **Never commit on an empty read.** An idle subscriber must cost one read, not
   a read and a write. Most subscribers are idle most of the time, so this alone
   halves the idle cost.
2. **An optional `wait_for` on the port.**
   ```py-sketch
   async def wait_for(self, topic, *, after, timeout) -> bool:
       """Block until there is something after `after`. Default: sleep and poll."""
   ```
   Adapters that can do better, do — Postgres `LISTEN`, Redis `XREAD BLOCK`,
   a Kafka consumer's own blocking poll — and idle cost collapses to zero. The
   default implementation keeps every adapter valid without it, which is what
   makes it an *optional* capability rather than a contract change.
3. **`commit_many`** so one subscriber across twenty topics writes once.
4. **`commit_interval`** — coalesce commits, at most one per second per
   subscriber.

Point 4 deserves the emphasis: **a delayed checkpoint costs re-dispatch, not
duplicates**, because the dispatch key is what protects correctness. So this
optimisation is safe here in a way it would not be in a system whose checkpoint
*was* the dedupe. It trades a bounded window of *rework* for a large reduction in
writes — and rework is the cheap side of §4.2's asymmetry, by design.

**Recommendation:** commit-per-batch by default; skip on empty; ship `wait_for`
with a polling default from day one so adapters can improve it without a contract
change; expose `commit_interval` as a documented knob, defaulted to 0.

**What still needs measuring rather than reasoning:** the crossover where
checkpoint writes become the dominant store load. My estimate is that it is
irrelevant below ~100 (subscriber × topic) pairs at a 1 s poll, but that is an
estimate, and phase 1 is what makes it measurable.

---

### 13.5 Where the conformance kits ship

**Decision: in the main wheel under `loom.testing.conformance`, with the test
dependencies behind a `[testing]` extra.**

Two facts settle it. `loom/testing/` **already ships in the main package** —
`run_with`, `given`, `assert_replays`, `ManualClock` — so a testing namespace is
established, not novel. And optional dependencies already have a pattern:
`[mongo]`, `[postgres]`, `[identity]`, `[credentials]` all gate an import behind
an extra, with an error that names the fix.

```py-sketch
from loom.testing.conformance import verify_event_log

def test_my_redis_streams_log():
    verify_event_log(lambda: MyRedisStreamsLog(url))
```

Importing it without the extra raises the same shape of error the service-account
path already uses: *"the conformance kit needs `pip install 'loomflow[testing]'`"*.

**Why not a separate `loomflow-testkit`.** The kit's whole job is to assert *this
version's* contract. A separately versioned package can be installed against a
different LOOM, and then it tests a contract that is not the one in force —
silently passing an adapter that is wrong, which is worse than having no kit. One
wheel, one version, one contract.

## Sources

- [Salesforce — event message durability and replay IDs](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/event-message-durability.html)
- [Salesforce — managed subscriptions](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/managed-sub.html)
- [Microsoft Graph — lifecycle notifications](https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events)
- [Microsoft Graph — change notifications via webhooks](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks)
- [Slack — Events API](https://docs.slack.dev/apis/events-api/)
- [Gmail — push notifications](https://developers.google.com/workspace/gmail/api/guides/push)
- [Jira Cloud — webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/)
- Prior art: `pipeshub-ai` — `backend/python/app/services/events/{models,ingress,consumer}.py`
  and `services/tasks/application/engine.py::fire_event`
