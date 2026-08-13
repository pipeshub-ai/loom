# Triggers

Triggers define how workflows are started. A workflow can have multiple triggers, and they are declared as part of the `@workflow` decorator.

<!-- docs-preamble -->

Every example on this page assumes these imports, and two steps standing in for
whatever real work a workflow does:

```python
from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.state.memory import MemoryStore
from workflow_builder.triggers.specs import (
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
from workflow_builder.triggers.specs import Interval

@workflow(name="health_check", triggers=[Interval(every=300)])  # every 5 minutes
async def health_check(ctx: Context) -> str:
    ...
```

`every` accepts seconds (int/float) or a duration string.

### Manual

Invoked from the CLI, a test, or the dev UI. This is the default when no trigger is declared:

```python
from workflow_builder.triggers.specs import Manual

@workflow(name="one_off", triggers=[Manual(label="run_once")])
async def one_off(ctx: Context) -> str:
    ...
```

### Webhook

An HTTP endpoint that starts the workflow:

```python
from workflow_builder.triggers.specs import Webhook, AuthMode

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

Requires `pip install workflow-builder[api]` for the FastAPI server.

### OnEvent

Consume from a queue or event bus:

```python
from workflow_builder.triggers.specs import OnEvent

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
from workflow_builder.triggers.specs import Poll

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
from workflow_builder.triggers.specs import Chat

@workflow(name="assistant", triggers=[
    Chat(path="/assistant", streaming=True, session_scoped=True)
])
async def assistant(ctx: Context) -> str:
    ...
```

### Form

A hosted HTML form that starts the workflow on submit:

```python
from workflow_builder.triggers.specs import Form, FormField

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

## TriggerDispatcher

The `TriggerDispatcher` routes incoming events to registered workflows:

```python
from workflow_builder.triggers.routing import EventRouter, RoutingEvent

router = EventRouter()

# Register subscriptions
router.subscribe("orders.created", "order_handler")

# Dispatch an event
event = RoutingEvent(name="orders.created", payload={"order_id": "123"})
matched = router.route(event)
```

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
