# FAQ

## How is this different from Temporal / Prefect / Airflow?

workflow-builder is **library-first**: `pip install workflow-builder` and you have a working runtime with no external services. Temporal requires a cluster. Prefect and Airflow require a server. workflow-builder runs embedded with SQLite or in-memory, and you can later swap in MongoDB/PostgreSQL for production without changing workflow code.

It is also **agent-native**: `ctx.agent()` is a first-class durable operation. Agent turns are journaled and replayed like any other step. Other orchestrators bolt on LLM support after the fact.

## Can I use my own LLM provider?

Yes. Implement the `ModelProvider` protocol from `workflow_builder.agents.models`:

<!-- docs-preamble -->

Every example on this page assumes:

```python
from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.state.memory import MemoryStore


@step
async def request_approval(item: str) -> str:
    """Stand-in for the real work."""
    return item


@step
async def proceed(item: str) -> str:
    """Stand-in for the real work."""
    return item


@workflow(name="my_workflow")
async def my_workflow(ctx: Context, data: str) -> str:
    return data
```

```python
class MyProvider:
    async def chat(self, messages, tools=None, **kwargs):
        # Call your model, return a ChatResponse
        ...
```

The built-in provider (`AnthropicProvider`) supports Claude models. Pricing is configured in `agents/models.py::PRICING`.

## How does durable replay work?

Every side effect that goes through `ctx.step()`, `ctx.sleep()`, `ctx.wait_for_event()`, or `ctx.agent()` is recorded in a journal. If the process crashes or redeploys, the runtime re-enters the workflow body from the top. The journal short-circuits completed operations by returning the recorded result instead of re-executing. This guarantees exactly-once semantics for side effects.

## Can I run this in production?

Yes. Use `MongoStore` or `PostgresStore` for durable storage:

```python
from workflow_builder.state.mongo import MongoStore

runtime = Runtime(store=MongoStore("mongodb://localhost:27017/workflows"))
```

See the [Deployment Guide](deployment.md) for Docker and environment variable configuration.

## How do I add a custom tool for the agent?

Option 1 -- make a step available as a tool:

```python
from workflow_builder import step

@step
async def lookup_user(email: str) -> dict:
    """Look up a user by email.

    Args:
        email: The user's email address.
    """
    # ... your logic
    return {"name": "Alice", "role": "admin"}
```

Steps decorated with `@step` can be passed directly to agent backends as tools.

Option 2 -- register a toolset:

```python
from workflow_builder.toolsets.manifest import ToolsetManifest, OperationSpec
from workflow_builder.toolsets.registry import register_toolset

manifest = ToolsetManifest(
    id="my_tools",
    version="1.0.0",
    summary="My custom tools",
    groups={"users": [OperationSpec(id="users.lookup", summary="Look up user")]},
)
register_toolset(manifest)
```

## How do I test workflows?

Use `MemoryStore` -- it requires no external services:

```python
import pytest
from workflow_builder import Runtime
from workflow_builder.state.memory import MemoryStore

@pytest.mark.asyncio
async def test_my_workflow():
    runtime = Runtime(store=MemoryStore())
    result = await runtime.run(my_workflow, {"input": "test"})
    assert result.status == "COMPLETED"
    assert result.output == "expected"
```

## What happens if my workflow raises an exception?

If an unhandled exception propagates from the workflow body, the run is marked `FAILED`. You can configure retry at the step level:

```python
from workflow_builder import Retry

@step(retry=Retry(max_attempts=3, initial_delay=1.0, multiplier=2.0))
async def flaky_api_call(url: str) -> dict:
    ...
```

You can also use `runtime.retry(run_id)` to retry a failed run from the last successful journal entry.

## Can I run workflows on a schedule?

Yes. Use the `Schedule` trigger with cron syntax:

```python
from workflow_builder.triggers.specs import Schedule

@workflow(name="daily_report", triggers=[Schedule(cron="0 9 * * *")])
async def daily_report(ctx: Context) -> str:
    ...
```

See the [Triggers Guide](guides/triggers.md) for all trigger types.

## How do I pause a workflow and wait for human input?

Use `ctx.wait_for_event()`:

```python
@workflow(name="approval")
async def approval(ctx: Context) -> str:
    await ctx.step(request_approval, "Please review")
    event = await ctx.wait_for_event("approval_decision")
    if event["approved"]:
        await ctx.step(proceed)
    return "done"
```

Resume it externally:

```python
async def approve(runtime, run_id):
    await runtime.resume(run_id, event={"approved": True})
```
