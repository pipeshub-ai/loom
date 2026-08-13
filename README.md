# workflow-builder

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-750%20passed-brightgreen.svg)](.github/workflows/ci.yml)

**Library-first durable execution SDK for AI-powered workflows.**

Build workflows that survive crashes, schedule on cron, call any AI agent framework, and manage everything through natural language. Just `pip install` and go — no infrastructure required.

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
| **Coding agent** | Describe a workflow in English, get runnable Python. ReAct loop with tool discovery and validation. |
| **Typed toolsets** | Gmail, Google Calendar, Jira, Confluence, web search — Pydantic models, lazy loading, auto-generated docs. |
| **Workflow manager** | Agent-facing tools to list, run, schedule, and cancel workflows via natural language. |

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
| 07 | [Coding agent](examples/cookbook/07_coding_agent.py) | NL spec -> generated workflow |
| 08 | [Jira agent](examples/cookbook/08_jira_agent.py) | Jira toolset + coding agent |
| 09 | [Jira CLI](examples/cookbook/09_jira_cli.py) | Interactive Jira management |
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
