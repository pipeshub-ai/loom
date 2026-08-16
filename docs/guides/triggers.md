# Triggers

Triggers define how workflows are started. A workflow can have multiple triggers, and they are declared as part of the `@workflow` decorator.

<!-- docs-preamble -->

Every example on this page assumes these imports, and two steps standing in for
whatever real work a workflow does:

```python
from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore
from loom.triggers.specs import (
    Chat,
    EmailInbox,
    Form,
    Interval,
    Manual,
    OnEvent,
    OnFailure,
    Poll,
    Schedule,
    Webhook,
)


@step
async def fetch_metrics() -> dict:
    """Stand-in for the real work."""
    return {"visits": 1}


@step
async def format_report(data: dict) -> str:
    """Stand-in for the real work."""
    return str(data)
```


## Trigger Types

### Schedule (Cron)

Run on a cron schedule:

```python
@workflow(name="daily_report", triggers=[Schedule(cron="0 9 * * *")])
async def daily_report(ctx: Context) -> str:
    data = await ctx.step(fetch_metrics)
    return await ctx.step(format_report, data)
```

Cron syntax:

| Expression | Meaning |
|------------|---------|
| `0 9 * * *` | Daily at 9:00 AM |
| `*/15 * * * *` | Every 15 minutes |
| `0 0 * * 1` | Every Monday at midnight |
| `0 8 1 * *` | First day of every month at 8:00 AM |
| `0 */6 * * *` | Every 6 hours |

Options:

```python
Schedule(
    cron="0 9 * * *",
    timezone="America/New_York",  # default: UTC
    catch_up=True,                # replay missed fires (default: False)
)
```

### Interval

Simpler than cron when the exact phase does not matter:

```python
from loom.triggers.specs import Interval

@workflow(name="health_check", triggers=[Interval(every=300)])  # every 5 minutes
async def health_check(ctx: Context) -> str:
    ...
```

`every` accepts seconds (int/float) or a duration string.

### Manual

Invoked from the CLI, a test, or the dev UI. This is the default when no trigger is declared:

```python
from loom.triggers.specs import Manual

@workflow(name="one_off", triggers=[Manual(label="run_once")])
async def one_off(ctx: Context) -> str:
    ...
```

### Webhook

An HTTP endpoint that starts the workflow:

```python
from loom.triggers.specs import Webhook, AuthMode

@workflow(name="github_push", triggers=[
    Webhook(
        path="/github",
        methods=("POST",),
        auth=AuthMode.HMAC,
        auth_config={"secret_env": "GITHUB_WEBHOOK_SECRET"},
    )
])
async def github_push(ctx: Context) -> str:
    payload = ctx.trigger_event.payload
    ...
```

Auth modes: `NONE`, `BASIC`, `HEADER`, `HMAC`, `BEARER`.

Response modes: `ACK` (return 202 immediately), `RESULT` (hold connection for output), `STREAM` (SSE).

Requires `pip install loomflow[api]` for the FastAPI server.

`loom.server.create_app(runtime)` serves the two URLs `Webhook.describe()`
advertises — `/webhook{path}` and `/webhook-test{path}` — and the second exists
so that pointing a provider at a laptop cannot fire production runs. One
delivery starts every workflow whose trigger matches the path; a path nothing
listens on answers **404** rather than a quiet 202, because a silent accept
looks exactly like a working integration until somebody asks why nothing
happened.

Reach for `OnAppEvent` instead when the event comes from a provider LOOM knows
(Slack, Jira, Gmail) or when more than one workflow wants it — see below.

### OnAppEvent

An event from the outside world, recorded in a durable log that many workflows
read independently:

```python
from loom.triggers.filter import FilterSpec
from loom.triggers.specs import OnAppEvent


@workflow(name="triage", triggers=[
    OnAppEvent(
        "app.slack.message",
        where=FilterSpec(conditions={"channel": "C_TECH"}),
    ),
])
async def triage(ctx: Context, message: dict) -> str:
    return message["text"]
```

The difference from `Webhook` is fan-out and resume. A `Webhook` trigger routes
one delivery to one workflow; `OnAppEvent` appends it to a topic, and **every**
subscriber reads that topic at its own pace with its own cursor. A subscriber
that was down catches up; a slow one holds nobody back; a redelivery
deduplicates.

The payload is the second parameter, as it is for any workflow — not
`ctx.trigger_event`.

Wire it up with an `EventDispatcher`, and give the Runtime a log:

```python
from loom.events import EventDispatcher, StoreBackedEventLog

store = MemoryStore()
runtime = Runtime(store=store, events=StoreBackedEventLog(store))
dispatcher = EventDispatcher(runtime)
```

`await dispatcher.register(triage)` reads the trigger and subscribes — it is
async because `start_at=LATEST` is pinned *then*, so nothing arriving between
subscribing and the first poll is skipped. `await dispatcher.start()` polls
until `runtime.shutdown()`.

`start_at=EARLIEST` is deliberately **refused** in a declaration: replaying a
retained backlog into a workflow that replies performs every one of those
replies at once, and the author cannot see how many. Backfill is
`loom events replay --subscriber s --topic t --since 7d --max-events 1000`,
which prints the number before it does anything.

Where the events come from — a signed webhook, a Pub/Sub pointer, a pull-log
like Salesforce — is the *source's* business, and adding one costs a verifier
and a normaliser: `docs/guides/event-sources.md`. Worked end to end in
`examples/cookbook/29_event_backbone.py`.

### OnEvent

Consume from a queue or event bus:

```python
from loom.triggers.specs import OnEvent

@workflow(name="order_handler", triggers=[
    OnEvent(topic="orders.created", source="kafka")
])
async def order_handler(ctx: Context) -> str:
    order = ctx.trigger_event.payload
    ...
```

Options: `group` (consumer group), `batch_size`, `idempotency_field`.

### Poll

Poll a source that lacks webhooks, with cursor persistence:

```python
from loom.triggers.specs import Poll

@workflow(name="check_updates", triggers=[
    Poll(every=60, cursor_key="last_id")
])
async def check_updates(ctx: Context) -> str:
    ...
```

The host persists the cursor between invocations. Idle polls (no new items) do not create executions.

### Chat

A conversational endpoint with session continuity:

```python
from loom.triggers.specs import Chat

@workflow(name="assistant", triggers=[
    Chat(path="/assistant", streaming=True, session_scoped=True)
])
async def assistant(ctx: Context) -> str:
    ...
```

### Form

A hosted HTML form that starts the workflow on submit:

```python
from loom.triggers.specs import Form, FormField

@workflow(name="feedback", triggers=[
    Form(
        path="/feedback",
        title="Submit Feedback",
        fields=(
            FormField(name="name", label="Your Name", required=True),
            FormField(name="message", label="Message", type="textarea", required=True),
            FormField(name="rating", label="Rating", type="number"),
        ),
    )
])
async def feedback(ctx: Context) -> str:
    data = ctx.trigger_event.payload
    ...
```

## Three dispatchers, and which one you want

The names are close enough to be worth stating plainly, because picking the
wrong one is a silent mistake rather than an error:

| | Drives | Use it for |
|---|---|---|
| `TriggerDispatcher` (`runtime/dispatcher.py`) | `Schedule`, `Interval` | cron — persists a `TriggerRecord`, so schedules survive a restart |
| `EventDispatcher` (`events/dispatcher.py`) | `OnAppEvent` | the event log — per-subscriber cursors, fan-out, replay |
| `EventRouter` (`triggers/routing.py`) | in-process `OnEvent` | embedded routing with no log behind it |

`start_scheduler` puts the first on the same loop and lease as due timers and
orphan recovery; the second is started with `await dispatcher.start()`, and both
stop with `runtime.shutdown()`.

`EventRouter` is the in-process one, and it keeps no position — an event nobody
was listening for is gone:

```python
from loom.triggers.routing import EventRouter, RoutingEvent

router = EventRouter()

# Register subscriptions
router.subscribe("orders.created", "order_handler")

# Dispatch an event
event = RoutingEvent(name="orders.created", payload={"order_id": "123"})
matched = router.route(event)
```

That is the whole reason `OnAppEvent` and the log exist: durability and resume
are properties of the *record*, not of the delivery.

## Multiple Triggers

A single workflow can have multiple triggers:

```python
@workflow(name="sync", triggers=[
    Schedule(cron="0 */4 * * *"),   # Every 4 hours
    Webhook(path="/sync"),           # On demand via HTTP
    Manual(),                        # CLI/test
])
async def sync(ctx: Context) -> str:
    ...
```
