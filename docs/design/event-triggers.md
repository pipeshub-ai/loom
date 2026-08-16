# Webhooks, app events, and filtering — investigation, research, design

> **Status: partially superseded.** `event-backbone.md` replaces the transport
> and delivery model here — the webhook is one producer into a durable event log,
> not the system. The provider research (§2), the sync-point rules (§3.6) and the
> security notes (§3.8) still stand and are referenced from that document.

Written before any implementation. Part 1 is what LOOM has today, read out of the
code. Part 2 is what the three providers actually do, verified against vendor
documentation in August 2026. Part 3 is the design those two parts force —
including two places where the obvious design is wrong for a reason that only
shows up in Part 2.

---

## 1. What exists today

### 1.1 Triggers are declarations; hosts are supposed to drive them

`triggers/base.py` is explicit about the split:

> A trigger is a *declarative spec* attached to a workflow, not executable
> machinery. […] a host (the dev server, a worker fleet, a serverless adapter)
> reads the specs and wires up the actual listeners.

That is a good split. The problem is how few of the listeners exist.

| Trigger spec | Declared in | Driver today | Status |
|---|---|---|---|
| `Schedule`, `Interval` | `specs.py` | `runtime/dispatcher.py` | **works** — claims, catch-up, jitter, leases |
| `OnEvent` | `specs.py` | `triggers/queue.py` | **works** — submit-then-ack, idempotency from message id |
| `OnFailure` | `specs.py` | `runtime/engine.py:_run_failure_handlers` | **works** |
| `Manual` | `specs.py` | CLI / `runtime.run` | **works** |
| **`Webhook`** | `specs.py` | *nothing* | **declared only** |
| `Poll` | `specs.py` | *nothing* | declared only |
| `EmailInbox` | `specs.py` | *nothing* | declared only |
| `Form`, `Chat` | `specs.py` | *nothing* | declared only |

`Webhook` is the sharp one. It has `path`, `methods`, `auth: AuthMode`,
`idempotency_header`, `raw_body`, and a `describe()` that advertises
`production_url = /webhook{path}` and `test_url = /webhook-test{path}` — and
**`server/app.py` has no webhook route at all.** Its routes are `/health`,
`/workflows`, `/runs*`, `/artifacts*`, `/blobs*`. Nothing serves `/webhook/…`.
`AuthMode.HMAC` exists in the enum and is implemented nowhere.

So a user can declare `@workflow(triggers=[Webhook("/orders", auth=AuthMode.HMAC)])`,
see it in `loom workflows`, and never receive a request.

### 1.2 Filtering exists and is wired to nothing

`triggers/filter.py::FilterSpec` is real and good: dotted paths, and `$in`,
`$nin`, `$gt`, `$gte`, `$lt`, `$lte`, `$ne`, `$regex`, `$exists`. It has tests.

But **no trigger spec carries a filter.** The only `FilterSpec` field in the
package is on `routing.py::Subscription`, and `Subscription` is not reachable
from a workflow declaration — nothing constructs one from `@workflow(triggers=…)`.

### 1.3 The router is a stub, and says so

`routing.py::EventRouter.route()` matches subscriptions, applies the filter, and
returns a list of workflow names. Its own docstring:

> In the full implementation this would create runs via the Runtime and resume
> waiting runs via the ExecutionStore. This base implementation handles
> subscription matching and filtering; subclasses or integration code handle the
> actual run creation.

`EventRouter` is referenced by exactly one file outside itself:
`tests/test_filter_routing.py`. It is not constructed by `Runtime`, the server,
or the CLI. **Nothing turns an event into a run.**

### 1.4 What is worth reusing rather than reinventing

This is the good news, and it is substantial:

| Primitive | Where | Why it matters here |
|---|---|---|
| `Runtime.submit(idempotency_key=…)` | `engine.py:625` | Checked **before admission**, so a redelivery neither starts a run nor consumes a rate-limit slot |
| `Runtime.send_event(dedupe_key=…)` | `engine.py:790` | Resumes a parked run at-most-once |
| `ExecutionStore.claim_event_delivery(key, ttl=7d)` | `stores/base.py:93` | **Atomic on all four backends**, with a conformance suite. Exactly the primitive a provider delivery-id needs |
| `QueueConsumer` | `triggers/queue.py` | A working durable ingress, and the *pattern*: submit → then ack |
| `Runtime.supervise()` | `engine.py:952` | Background services stop on `shutdown()` |
| `TriggerDispatcher` + `start_scheduler` | `dispatcher.py` | Leased, single-leader periodic work — which is what a Gmail watch renewal is |
| `LockProvider` | every store | Serialising a shared cursor across processes |
| `Runtime.state` (`StateStore`) | `engine.py:269` | Keyed `(workflow, key)`, usable outside a run — where a Gmail `historyId` cursor belongs |
| `TaintBroker` | `runtime/effects.py` | "Data the run did not bring with it" — a webhook body is exactly that |
| `AdmissionController` | `engine.py:_admit` | Flow control before a record exists |

**The QueueConsumer contract is the model to copy**, stated in its own module
docstring: poll → submit under an idempotency key derived from the message id →
*only then* ack. At-least-once in, exactly-once executed.

---

## 2. What the three providers actually do

Verified against vendor docs. Sources at the end.

### 2.1 They are not three variations of one thing

| | Slack Events API | Jira Cloud webhooks | Gmail push |
|---|---|---|---|
| **Transport** | direct HTTP POST | direct HTTP POST | Cloud **Pub/Sub** → HTTP push |
| **Payload** | the whole event | the whole event | **`{emailAddress, historyId}` — no message** |
| **Auth** | `X-Slack-Signature: v0=…` HMAC-SHA256 over `v0:{ts}:{raw body}` | `X-Hub-Signature: sha256=…` (WebSub) | Pub/Sub OIDC bearer token |
| **Replay window** | reject if `X-Slack-Request-Timestamp` > 300s old | — | JWT `exp` |
| **Ack deadline** | **3 seconds** | none published | Pub/Sub ack deadline |
| **Retries** | 3 more: ~immediate, 1 min, 5 min | provider-side | Pub/Sub redelivery |
| **Retry signal** | `x-slack-retry-num`, `x-slack-retry-reason` | — | `deliveryAttempt` |
| **Dedupe id** | `event_id` | delivery has no stable id → hash body | `message.messageId` |
| **Provider-side filter** | event-type subscription only | **JQL** (`project = PROJ AND …`) | `labelIds` on `watch()` |
| **Setup handshake** | `url_verification` → echo `challenge` | none | none |
| **Expiry** | none | none | **watch dies after 7 days** |

Three consequences fall straight out of that table, and each one changes the
design rather than decorating it.

### 2.2 Gmail is not a webhook

A Gmail notification carries **only** the mailbox address and a new `historyId`.
It does not carry the message, the sender, the subject, or the labels. To learn
what actually happened you must:

1. hold a **persisted cursor** — the last `historyId` you processed, per mailbox;
2. call `history.list(startHistoryId=<cursor>)` to get the deltas;
3. call `messages.get` for each new id;
4. advance the cursor.

So "on email received" is a *reconciliation*, not a delivery. And it drags in
three more obligations:

- **The cursor is shared mutable state.** Two deliveries arriving together both
  read the same cursor and both process the same messages. It needs a lock —
  and `LockProvider` is already on every store.
- **`watch()` expires after 7 days** (Google recommends renewing daily), so the
  integration includes a *scheduled maintenance task*. LOOM already has leased
  periodic work: `start_scheduler(dispatcher=…)`.
- **Notifications can be dropped**, and Google's own guidance is to keep a
  fallback poll. So `Poll` — currently a spec with no driver — is not optional
  for Gmail.

A design that models Gmail as "a webhook that delivers an email" is wrong at the
first line, and the failure mode is silent: you get a run per notification whose
payload is a history id nobody can use.

### 2.3 Three seconds forces ack-then-process

Slack retries anything that is not a 2xx within three seconds. Anything that
holds the connection while it verifies, expands, filters, and submits N runs is
one slow store away from a duplicate storm — and the duplicates arrive as
retries of an event that *did* succeed.

The correct shape is the one `QueueConsumer` already uses, adapted: **acknowledge
as soon as the delivery is durably accepted, not when it has been processed.**
That means the ack cannot come before the delivery is somewhere it will survive a
crash, which is the whole reason `QueueConsumer` acks after submit rather than
before.

### 2.4 Signature verification needs the bytes, not the parsed body

Slack signs `v0:{timestamp}:{raw request body}`. Jira signs the raw body. Any
route that lets a framework parse JSON first and re-serialises it to verify will
fail on whitespace, key order, and unicode escaping — intermittently, and only
against real traffic. The ingress must see `bytes`.

### 2.5 Filtering happens in two places and they are not interchangeable

- **Provider-side.** Jira's JQL is the strong case: `project = SUPPORT AND
  priority = Blocker` is evaluated at Atlassian and the traffic never leaves.
  Gmail's `labelIds` is a weaker version. Slack's is weakest — you subscribe to
  event *types*, not to channels.
- **App-side.** `FilterSpec` over the delivered payload. Arbitrary, versioned
  with the workflow code, reviewable in a diff — and you pay for delivery,
  verification, and dedupe before you discard it.

The user's example — *"process messages only from the tech channel"* — is
app-side, because Slack cannot filter by channel at subscription time.

**And it has the entity-resolution problem this codebase already knows.** The
obvious filter is:

```py-sketch
FilterSpec(conditions={"event.channel": "tech"})
```

Slack's payload carries `"channel": "C024BE91L"`. That filter matches **nothing,
forever, with no error** — the exact failure `resolves=` exists to prevent,
reappearing one layer up. The design has to make the id-not-name rule as
unavoidable here as it is in the toolsets.

---

## 3. Design

> The code blocks below are marked ``py-sketch`` rather than ``python``: they
> describe an interface that does not exist yet, and the repository's docs check
> executes every ``python`` block's names against the real package. A design
> sketch is not a documented example, and tagging it as one would either break
> that check or force it to be weakened.

### 3.1 Shape

```
provider  ──HTTP──▶  WebhookIngress  ──▶ EventSource ──▶ InboundEvent(s)
                          │                (verify,        │
                          │                 challenge,     │
                          │                 expand)        ▼
                          │                          EventRouter
                          ▼                          (match + filter)
                   claim_event_delivery                    │
                   (dedupe, atomic)                        ▼
                          │                    submit(idempotency_key=…)
                          ▼                    send_event(dedupe_key=…)
                       202 ACK
```

### 3.2 `EventSource` — the provider seam

One protocol, four methods, and it is where every provider difference lives:

```py-sketch
class EventSource(Protocol):
    id: str

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        """Raise WebhookRejected unless this really came from the provider."""

    def challenge(self, headers, body) -> bytes | None:
        """A setup handshake to echo back, or None. Slack's url_verification."""

    def delivery_id(self, headers, payload) -> str:
        """Stable across retries of the same logical event. Slack's event_id."""

    async def expand(self, payload, ctx: SourceContext) -> Sequence[InboundEvent]:
        """One delivery to zero or more events."""
```

`expand` is async and takes a context (credentials, state, lock) **because of
Gmail**. For Slack and Jira it is one line returning a single event. For Gmail it
takes the lock, reads the cursor, calls `history.list`, hydrates each message,
advances the cursor, and returns N events. Making it async for all three is the
price of not special-casing one.

Ship three: `SlackSource`, `JiraSource`, `GmailSource`, plus `GenericSource` for
`Webhook` (HMAC or bearer or none, one event, body as payload).

### 3.3 `InboundEvent` — the normalized unit

```py-sketch
@dataclass(frozen=True)
class InboundEvent:
    type: str          # "slack.message", "jira:issue_created", "gmail.message"
    payload: dict      # already expanded — a real message, not a history id
    source: str        # "slack"
    delivery_id: str   # for dedupe
    received_at: datetime
```

Filters run against `payload`. Everything downstream is provider-agnostic.

### 3.4 Trigger specs — `filter` becomes a first-class field

```py-sketch
@workflow(triggers=[
    OnAppEvent("slack", "message", filter=FilterSpec(
        conditions={"channel": "C024BE91L", "subtype": {"$exists": False}},
    )),
])
async def triage(ctx: Context, message: SlackMessage) -> str: ...
```

and `Webhook` gains the same `filter` field, so a generic hook can drop traffic
without starting a run.

**The name-vs-id trap gets a helper, not a warning.** Because a hand-written
`"channel": "tech"` silently matches nothing:

```py-sketch
OnAppEvent("slack", "message", where=SlackChannel("#tech"))
```

`where=` takes a *resolver* that runs once at registration — using the Slack
toolset's own `slack_find_channel`, which already raises rather than answering
`None` from a truncated scan — and freezes the resolved id into the filter. The
same shape as the coding agent's "resolve now, bake the id, keep the human name
in a comment" ladder. A misspelled channel then fails at **registration**, loudly,
instead of at runtime, never.

### 3.5 Delivery, ack, and where durability actually is

The route does the minimum that must be synchronous:

1. read raw `bytes`;
2. `source.verify(...)` — constant-time compare, replay window;
3. `source.challenge(...)` — echo and return if this is a setup handshake;
4. `store.claim_event_delivery(f"{source}:{delivery_id}")` — **as a fast path
   only**: a claim that already exists means "we have finished this one", so
   skip to the ack. A first claim does *not* commit us to anything;
5. hand to the **sink**, which does the durable idempotent work (§3.6);
6. **claim on success** — the marker is written *after* the work, not before;
7. `202`.

The order of 4/5/6 is the whole correctness argument, and an earlier draft of
this document had it wrong. Claiming *before* processing makes the claim the
correctness mechanism, and a crash between the claim and the submit then loses
the event permanently: the claim says "seen", the provider has been told 202, and
no run exists. That converts an at-least-once transport into at-most-once
delivery, which is the one thing a webhook ingress must never do.

Claiming *after* is safe because every operation in step 5 is independently
idempotent (§3.6) — a crash mid-way is repaired by the provider's own retry,
which re-derives the same keys and converges.

A duplicate at step 4 still returns **202**. Answering an error would make Slack
retry an event we have already handled — a retry storm caused by correct dedupe.
The same applies to an event that matches no workflow, and to one every filter
rejects: **ack, log, do nothing.** A 404 there would also leak which paths exist.

The **sink** is the seam where a deployment chooses its durability:

- `DirectSink` — expand, filter, `submit()` inline, then ack. Correct and simple;
  fine for one or two subscriptions. This is the default and what a laptop wants.
- `QueueSink` — put the verified raw delivery on a `QueueBackend` and ack. The
  existing `QueueConsumer` then drives it with the submit-then-ack contract that
  is already written and tested. This is the production answer, and it costs no
  new durability machinery because **LOOM already has a durable ingress.**

Being explicit about the trade rather than hiding it: with `DirectSink`, a crash
between the 202 and the submit loses the event, because we have already told the
provider it succeeded. That is a real window, it is small, and `QueueSink` closes
it. Pretending otherwise would be the dishonest part.

### 3.6 Sync points — never processing the same event twice

One rule, applied four times:

> **The sync point is the identity of the thing you are about to act on,
> recorded durably by the act itself. Claims, cursors and acks are bounds on
> rework, not correctness mechanisms. Do the idempotent durable thing first;
> advance the marker last.**

| Shape | Correctness marker (permanent) | Rework bound (disposable) | Failure if inverted |
|---|---|---|---|
| Push, payload-carrying (Slack, Jira, generic) | `submit(idempotency_key="{source}:{delivery_id}:{workflow}")` — on the execution record | `claim_event_delivery(delivery_id)`, 7-day TTL | claim before submit ⇒ **event lost** |
| Push → a run parked on `wait_for_event` | `send_event(dedupe_key="{source}:{delivery_id}")` | same claim | run advances **twice** |
| Cursor (Gmail) | `submit(idempotency_key="gmail:{address}:{message_id}:{workflow}")` — per **item** | `historyId` cursor + `LockProvider` lease | advance cursor before submit ⇒ **email never processed** |
| Queue (`OnEvent`, exists) | `submit(idempotency_key=message.id)` | broker visibility timeout | ack before submit ⇒ **message lost** |

Four things follow that are not obvious.

**The delivery id is the wrong key for a cursor source.** One Gmail notification
expands to N messages. Keying on the notification means a crash after three of
five submits leaves the delivery either wholly unclaimed (all five reprocessed —
harmless) or wholly claimed (two lost — not). Keying on the *message id* makes
the unit of idempotency the unit of work, so re-expanding the same history range
creates zero new runs. **For a cursor source the cursor is an optimisation; the
item identity is the sync point.**

**Advance the cursor last, and per page.** Advancing early and crashing skips
mail; advancing late and crashing re-scans it. Only one of those is recoverable.
Advancing per page rather than per notification bounds the re-scan to one page
instead of the whole backlog.

**Two markers with different lifetimes, and they must not be conflated.**
`claim_event_delivery` is a TTL cache — 7 days, which covers Slack's ~6-minute
retry window and Pub/Sub's maximum retention. The run's `idempotency_key` lives
on the execution record and is *permanent*, until `RetentionManager` eventually
drops the record. So the claim expiring cannot cause a duplicate run; only
retention, months later and far outside any provider's retry window, can. Worth
stating rather than implying the claim is what protects a run.

**Losing the lock race is safe for a cursor source and unsafe for a push
source**, which is why they are treated differently. Two concurrent Gmail
notifications for one mailbox: the loser can simply **drop**, because
`history.list(startHistoryId=…)` returns everything current *at call time*, so
the winner's reconciliation already covers whatever the loser was told about.
A dropped Slack delivery, by contrast, is a lost message — its payload exists
nowhere else — so a push source must queue behind the lock, not discard.

**Deliberate reprocessing stays possible.** `QueueConsumer` already exposes
`idempotency_prefix` — "change it to deliberately reprocess a queue that has
already been consumed". The ingress takes the same knob for the same reason: a
backfill after a bug fix is a legitimate operation, and it should be one
argument rather than a manual purge of claim rows.

### 3.7 Finishing `EventRouter`

It gains a `Runtime` and does what its docstring already promises:

```py-sketch
async def route(self, event: InboundEvent) -> RoutingResult:
    # 1. trigger subscriptions -> submit(), idempotency_key derived from
    #    (delivery_id, workflow) so two subscribers each get exactly one run
    # 2. runs parked on ctx.wait_for_event(name) -> send_event(dedupe_key=...)
```

Deriving the key from *delivery id + workflow name* is deliberate: two workflows
subscribed to the same event must each get a run, and a redelivery must give
neither a second one.

### 3.8 Security

- **Raw bytes** through to `verify`. Non-negotiable (§2.4).
- `hmac.compare_digest`, never `==`.
- Replay window, default 300s, matching Slack and the Standard Webhooks
  recommendation.
- **Refuse to serve an unauthenticated source on a non-loopback bind**, exactly
  as `loom serve` already refuses to bind a public interface with no identity
  configured. An unsigned public webhook is an unauthenticated `POST /runs`.
- **Webhook-started runs start tainted.** A webhook body is data the run did not
  bring with it and that an attacker may control — precisely `TaintBroker`'s
  rule. `taint_from_trigger=True` makes the first write or destructive call need
  a human. Off by default (it changes behaviour), on in the recipe for a
  public hook.
- Never log a body or a signature; log source, delivery id, event type, decision.

### 3.9 Gmail, concretely

- `GmailSource.expand` — Pub/Sub envelope → base64-decode `message.data` →
  `{emailAddress, historyId}` → **lock** on `gmail:{address}` → read cursor from
  `Runtime.state` → `history.list(startHistoryId=cursor)` → `messages.get` each →
  advance cursor → N `InboundEvent`s.
- **Watch renewal** as a shipped `@workflow(triggers=[Schedule("0 3 * * *")])`
  that re-calls `watch()`. Daily, against a 7-day expiry, so three consecutive
  failures are survivable.
- **Fallback poll** — the same reconciliation on a slower `Schedule`, because
  Google says notifications can be dropped. This is also what finally gives
  `Poll` a driver.
- A cursor that is too old to be honoured (Gmail expires history) must
  **reset and report**, not silently skip: advance to the current `historyId`,
  emit nothing, and log a gap. Silently skipping is "no email arrived today".

### 3.10 What the operator has to do out-of-band

Provider-side registration is not something a workflow file can do implicitly.
A `loom hooks` command surfaces it:

```
loom hooks list                  # every declared hook: source, event, URL, filter
loom hooks install --source jira # register with the provider, JQL from the spec
loom hooks verify                # is the registered config still what the code says
```

`hooks verify` is the one that earns its place: a JQL filter edited in the Jira
admin UI silently changes what the workflow sees, and nothing in the repository
would show it.

---

## 4. Phasing

| Phase | Delivers | Depends on |
|---|---|---|
| **1** | `EventSource`, `InboundEvent`, `GenericSource`, `WebhookIngress`, `/hooks/{source}` route, `DirectSink`, HMAC + replay + dedupe, `EventRouter` creating runs | nothing new |
| **2** | `filter=` on `Webhook`/`OnAppEvent`; `SlackSource`, `JiraSource`; `where=` resolvers | phase 1 |
| **3** | `GmailSource`, cursor + lock, watch-renewal workflow, fallback poll (`Poll` driver) | phase 2, Gmail toolset |
| **4** | `QueueSink`, `loom hooks` CLI, taint-from-trigger | phases 1–3 |

Phase 1 alone closes the largest gap: `Webhook` stops being a declaration that
does nothing.

## 5. Open questions

1. **Route shape** — `/hooks/{source}/{path}` (source-first, one route per
   provider) or keep `Webhook.describe()`'s promised `/webhook{path}` and infer
   the source from the spec? The second keeps a published contract; the first is
   clearer when one path serves several providers.
2. **Is `DirectSink` an acceptable default** given §3.5's loss window, or should
   a store-backed inbox be the default and the queue an optimisation?
3. **Should `OnAppEvent` subsume `OnEvent`?** They are different things — a
   broker topic versus a SaaS event — but the names will be confused.
4. Does `loom hooks install` belong in the SDK at all, or is provider
   registration a deployment concern that should stay in Terraform?

## Sources

- [Slack — Events API](https://docs.slack.dev/apis/events-api/)
- [Slack — verifying requests](https://docs.slack.dev/authentication/verifying-requests-from-slack/)
- [Jira Cloud — webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/)
- [Gmail — push notifications](https://developers.google.com/workspace/gmail/api/guides/push)
- [Standard Webhooks specification](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md)
