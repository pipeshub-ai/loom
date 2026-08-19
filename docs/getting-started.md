# Getting Started

## Installation

```bash
pip install loomsdk
```

Optional extras for specific backends:

```bash
# MongoDB storage
pip install loomsdk[mongo]

# PostgreSQL storage
pip install loomsdk[postgres]

# LangChain agent backend
pip install loomsdk[langchain]

# Everything
pip install loomsdk[all]
```

## Your First Workflow

Create a file `hello.py`:

<!-- docs-preamble -->

Every example on this page assumes:

```python
from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore
```

```python
import asyncio
from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore


@step
async def greet(name: str) -> str:
    return f"Hello, {name}!"


@step
async def shout(message: str) -> str:
    return message.upper()


@workflow(name="hello_world")
async def hello_world(ctx: Context) -> str:
    greeting = await ctx.step(greet, "World")
    result = await ctx.step(shout, greeting)
    return result


async def main():
    runtime = Runtime(store=MemoryStore())
    result = await runtime.run(hello_world, {})
    print(result.output)  # HELLO, WORLD!


asyncio.run(main())
```

Run it:

```bash
python hello.py
```

## Key Concepts

**Workflows** are async functions decorated with `@workflow`. They describe the sequence of operations. The workflow body must be deterministic -- no direct I/O, no `datetime.now()`, no `random`.

**Steps** are async functions decorated with `@step`. All side effects (API calls, file I/O, database queries) go inside steps. Steps are journaled so they are not re-executed on replay.

**Context** (`ctx`) is the only legal API from workflow code to the outside world. Use `ctx.step()` to call steps, `ctx.sleep()` to pause, `ctx.wait_for_event()` to park until an external signal.

**Runtime** drives execution. Pass it a store (MemoryStore for tests, SQLiteStore for local dev, MongoStore/PostgresStore for production) and call `runtime.run()`.

## Adding Retry

```python
from loom import Retry

@step(retry=Retry(max_attempts=3, initial_delay=1.0, multiplier=2.0))
async def call_api(url: str) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
```

## Using an AI Agent

```python
@workflow(name="ai_workflow")
async def ai_workflow(ctx: Context) -> str:
    result = await ctx.agent("Summarize the latest news about Python")
    return result
```

Configure the agent backend on the runtime:

```python
from loom.agents.backend import BuiltInBackend
from loom.agents.providers.anthropic_provider import AnthropicProvider

runtime = Runtime(
    store=MemoryStore(),
    agent_backend=BuiltInBackend(model=AnthropicProvider()),
)
```

## Next Steps

- Browse [cookbook examples](https://github.com/pipeshub-ai/workflow/tree/main/examples/cookbook) for patterns like parallel execution, error handling, human-in-the-loop, and cron triggers
- Read the [Architecture](architecture.md) guide to understand the runtime, journal, and suspension model
- Set up a production store with [MongoDB or PostgreSQL](guides/storage.md)
- Plug in [LangChain, Agno, or PydanticAI](guides/agent-backends.md) as your agent backend
