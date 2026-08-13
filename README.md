# workflow-builder

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-1544%20passed-brightgreen.svg)](.github/workflows/ci.yml)

**Library-first durable execution SDK for AI-powered workflows.**

Describe a workflow in English and get runnable Python back — compiled, linted,
type-checked, executed against fakes, and checked for determinism before you see
it. Or write it yourself: either way it survives crashes, resumes where it
stopped, and runs on a laptop or Postgres without a code change.

`pip install` and go — no infrastructure required.

## Quick Start

```python
from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.state.memory import MemoryStore

@step
async def fetch_data(url: str) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        return (await client.get(url)).json()

@step
async def summarize(data: dict) -> str:
    return f"Found {len(data)} keys: {', '.join(list(data.keys())[:5])}"

@workflow(name="research")
async def research(ctx: Context, url: str) -> str:
    data = await ctx.step(fetch_data, url)
    return await ctx.step(summarize, data)

import asyncio

async def main():
    rt = Runtime(store=MemoryStore())
    result = await rt.run(research, "https://jsonplaceholder.typicode.com/todos/1")
    print(result.output)  # "Found 4 keys: userId, id, title, completed"

asyncio.run(main())
```

## Installation

```bash
# Core (MemoryStore + SQLite, zero infra)
pip install workflow-builder

# With storage backends
pip install workflow-builder[mongo]        # MongoDB (motor)
pip install workflow-builder[postgres]     # PostgreSQL (asyncpg)

# With agent framework backends
pip install workflow-builder[langchain]    # LangChain / LangGraph
pip install workflow-builder[agno]         # Agno
pip install workflow-builder[pydantic-ai]  # Pydantic AI

# Google Workspace service-account auth (Gmail/Calendar work without it)
pip install workflow-builder[google]

# With FastAPI webhook server
pip install workflow-builder[api]

# Command line and terminal UI
pip install workflow-builder[cli]          # rich output
pip install workflow-builder[tui]          # loom ui

# MCP server — drive workflows from Claude Code, Claude Desktop, Cursor
pip install workflow-builder[mcp]

# Everything
pip install workflow-builder[all]

# Development
pip install -e ".[dev]"
```

## Key Features

| Feature | Description |
|---------|-------------|
| **Durable execution** | Every side effect is journaled. Crash on step 9? Resume at step 9, not step 1. |
| **Agent-native** | `ctx.agent("prompt")` calls any AI agent. LangChain, Agno, Pydantic AI — swap the backend, keep the code. |
| **Cron triggers** | `@workflow(triggers=[Schedule("0 9 * * *")])` — fires automatically via TriggerDispatcher. |
| **Pluggable storage** | MemoryStore (tests) -> SQLite (dev) -> MongoDB/PostgreSQL (prod). Same code, different backend. |
| **[Coding agent](#workflow-coding-agent)** | Describe a workflow in English, get runnable Python — verified by a seven-stage pipeline before you see it. |
| **Typed toolsets** | Gmail, Google Calendar, Jira, Confluence, web search — Pydantic models, lazy loading, auto-generated docs. |
| **Workflow manager** | Agent-facing tools to list, run, schedule, and cancel workflows via natural language. |
| **[CLI + TUI](#command-line)** | `loom run`, `loom runs`, `loom approve`, `loom ui`. Exit codes distinguish suspended from failed. |
| **[MCP server](#mcp-server)** | `loom mcp` — drive workflows from Claude Code, Claude Desktop, or Cursor. |

## Architecture

```
@workflow + @step          ctx.step() / ctx.agent()        Journal + Store
 (your code)          -->   (durable operations)      -->   (crash-safe replay)
       |                          |                              |
       v                          v                              v
  WorkflowDefinition         Context API                ExecutionStore protocol
  TriggerSpec (cron)     step / sleep / agent / gather   Memory | SQLite | Mongo | Postgres
```

**Core loop**: Load execution record -> re-enter workflow body -> journal short-circuits completed work -> new work is executed and journaled -> if body completes: COMPLETED; if raises Suspend: SUSPENDED (scheduler resumes later).

## Workflow Coding Agent

Describe the workflow in English; get a runnable file back.

```python
from workflow_builder.agents.coding_agent import WorkflowCodingAgent
from workflow_builder.agents.providers import AnthropicProvider

agent = WorkflowCodingAgent(AnthropicProvider())
result = await agent.generate(
    "Fetch a URL, count the words in the response, and report the count."
)

print(result.is_clean)   # True — it validates, runs, and reproduces
print(result.code)       # the complete file, ready to run
```

The interesting part is not that a model writes Python. It is what happens
before you see it: **every generation is verified, and a failure is fed back for
repair.** Seven stages run cheapest-first and stop at the first blocking error.

| Stage | Catches |
|---|---|
| `compile` | Syntax |
| `static` | Missing `@workflow`, I/O in the body, `datetime.now()`, a store chosen in code, an integration you don't have |
| `lint` | Undefined names, unreachable code (ruff, skipped if absent) |
| `types` | A step called with the wrong arity, a wrong return type (mypy, skipped if absent) |
| `smoke` | Actually runs it — against generated fakes and a faked clock, so a four-minute `ctx.sleep` costs nothing and no credentials are needed |
| `replay` | Runs it twice and compares. Non-determinism the static rules cannot see |
| `critique` | A second model reviews durability and spec fidelity (opt-in) |

`result.is_clean` means it compiled, validated, **ran**, and reproduced — not
that it parsed. Anything a stage finds is handed back as a repair instruction
carrying both the error and the spec, and a repair that doesn't reduce the error
count is discarded rather than accepted.

**It resolves what your words refer to before writing code.** You name a person
and a status the way people do; the API matches account ids and its own
per-project vocabulary. A query built from your words returns zero rows *and no
error* — which reads as "nothing to do" when it means "no such name". So the
agent looks entities up while authoring, bakes the resolved id into the code
with the name beside it in a comment, and where a name is genuinely ambiguous
emits a `ctx.agent()` node to decide at run time rather than guessing.

```python
agent = WorkflowCodingAgent(
    AnthropicProvider(),
    tool_registry=rt.toolsets,      # what it may discover and call
    allowed_packages={"httpx"},     # what the target environment has
    supervisor=CodeSupervisor(other_model),   # optional second opinion
)
```

Toolsets load on demand: the prompt carries an index card per integration, and
operations, schemas, and examples are fetched only for the ones a task needs —
so adding integrations does not tax unrelated generations.

> **Maturity.** Generation quality varies with the spec. Simple, well-scoped
> workflows come back clean; an ambiguous entity reference may still take
> several attempts or resolve to the wrong candidate. The verification pipeline
> is what makes that safe to iterate on — you find out before the code runs.

See `examples/cookbook/07_coding_agent.py`, and `09_jira_cli.py --debug` to
watch every tool call it makes while resolving.

## Command Line

```bash
pip install "workflow-builder[cli]"
```

```bash
loom run onboard --input '{"email": "a@b.com"}'   # or -i @payload.json
loom run onboard --follow                          # stream steps as they finish
loom runs --status failed
loom show <run> / loom watch <run>

loom approve <run> refund [--reject]               # unpark a human-gated run
loom send <run> <event> '{"token": "x"}'
loom cancel / retry / replay <run>

loom check flows/order.py       # write order.graph.json + order.description.md
loom serve --port 8000          # HTTP API
loom ui                         # terminal UI, needs [tui]
```

**Exit codes are the contract:** `0` completed, `1` failed, `2` usage,
**`3` suspended**, `4` cancelled. A run parked on a human has neither succeeded
nor failed, and collapsing it into either makes calling scripts do the wrong
thing. `--json` on every command; `--server URL` runs any of them against a
remote LOOM instead of importing locally.

## MCP Server

Drive workflows from Claude Code, Claude Desktop, or Cursor.

```bash
pip install "workflow-builder[mcp]"
claude mcp add loom -- loom mcp --module flows.py
```

Ten tools — list, run, inspect a journal, approve, send an event, cancel, retry,
replay — plus resources and prompts. `--transport http` for networked clients.

Two things it does that a thin wrapper would not: a **suspended** run comes back
with what it is waiting for and the exact call that unparks it, plus a note that
suspended is not failure; and the difference between `retry_run` (from the
failure, current code) and `replay_run` (from the journal, no side effect
repeated) is spelled out, because a model reliably guesses wrong.

See [docs/guides/mcp.md](docs/guides/mcp.md).

## Agent Backends

The workflow code is framework-agnostic. The agent framework is configured on the Runtime:

```python
from workflow_builder.agents.backends.langchain import LangChainBackend
from langchain_anthropic import ChatAnthropic

rt = Runtime(
    store=MemoryStore(),
    agent_backend=LangChainBackend(llm=ChatAnthropic(model="claude-sonnet-4-6")),
)

# Workflow code — no LangChain imports
@workflow(name="research")
async def research(ctx: Context, query: str) -> str:
    result = await ctx.agent(f"Search for articles about {query}")
    return result.output
```

Available backends: `LangChainBackend`, `AgnoBackend`, `PydanticAIBackend`, `BuiltInBackend`, or implement your own `AgentBackend` protocol.

## Storage Backends

```python
# Development (zero infra)
from workflow_builder.state.memory import MemoryStore
rt = Runtime(store=MemoryStore())

# Local persistence
from workflow_builder.state.sqlite import SQLiteStore
rt = Runtime(store=SQLiteStore("workflows.db"))

# Production (MongoDB)
from workflow_builder.state.mongo import MongoStore
store = MongoStore("mongodb://localhost:27017", database="workflows")
await store.ensure_indexes()
rt = Runtime(store=store)

# Production (PostgreSQL)
from workflow_builder.state.postgres import PostgresStore
store = PostgresStore("postgresql://user:pass@localhost/workflows")
await store.connect()
rt = Runtime(store=store)
```

## Trigger System

```python
from workflow_builder.triggers.specs import Schedule
from workflow_builder.runtime.dispatcher import TriggerDispatcher

@workflow(name="daily_report", triggers=[Schedule("0 9 * * 1-5")])
async def daily_report(ctx: Context, _: None = None) -> str:
    result = await ctx.agent("Generate today's status report")
    return result.output

rt = Runtime(store=MemoryStore())
dispatcher = TriggerDispatcher(rt)
await dispatcher.register(daily_report)
await dispatcher.start()  # Fires at 9am weekdays
```

## Cookbook Examples

| # | Example | Pattern |
|---|---------|---------|
| 01 | [Sequential pipeline](examples/cookbook/01_sequential.py) | Step chaining, Retry |
| 02 | [Parallel fan-out](examples/cookbook/02_parallel.py) | ctx.gather() |
| 03 | [Durable sleep](examples/cookbook/03_durable_sleep.py) | ctx.sleep(), scheduler |
| 04 | [Error handling](examples/cookbook/04_error_handling.py) | Retry, OnError.ROUTE, Failure |
| 05 | [Human-in-the-loop](examples/cookbook/05_human_in_the_loop.py) | ctx.wait_for_event(), send_event |
| 06 | [AI agent step](examples/cookbook/06_ai_agent_step.py) | ctx.agent() with Claude |
| 07 | [Coding agent](examples/cookbook/07_coding_agent.py) | NL spec -> generated, verified workflow |
| 08 | [Jira agent](examples/cookbook/08_jira_agent.py) | Jira toolset + coding agent |
| 09 | [Jira CLI](examples/cookbook/09_jira_cli.py) | Coding agent end to end; `--debug` shows every tool call |
| 10 | [LangChain backend](examples/cookbook/10_langchain_react_agent.py) | LangChain ReAct via AgentBackend |
| 11 | [Agno backend](examples/cookbook/11_agno_backend.py) | Agno agent via AgentBackend |
| 12 | [Pydantic AI backend](examples/cookbook/12_pydantic_ai_backend.py) | Pydantic AI via AgentBackend |
| 13 | [Cron trigger](examples/cookbook/13_cron_trigger.py) | Schedule-based dispatch |
| 14 | [Workflow manager](examples/cookbook/14_workflow_manager_cli.py) | Agent that manages workflows |
| 15 | [Queue consumer](examples/cookbook/15_queue_consumer.py) | At-least-once ingress, exactly-once runs |
| 16 | [HTTP server](examples/cookbook/16_http_server.py) | create_app() + LoomClient |
| 17 | [Files and artifacts](examples/cookbook/17_files_and_artifacts.py) | Attachment, blobs, versioned artifacts |
| 18 | [Gmail and Calendar](examples/cookbook/18_gmail_calendar.py) | Google toolsets, approval before send |

## Development

```bash
git clone https://github.com/pipeshub-ai/workflow.git
cd workflow
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check src tests

# Type check
mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License. See [LICENSE](LICENSE) for details.

Built by [PipesHub AI](https://pipeshub.com).
