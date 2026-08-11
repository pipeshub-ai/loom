# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**workflow-builder** ("LOOM") is a pip-installable, **library-first** durable execution SDK for AI-powered workflows. The primary deliverable is a **Workflow Coding Agent**: an LLM-powered agent that _authors_ workflow code — mixing third-party SDK calls, workflow constructs (`@workflow`, `@step`, `ctx.*`), and raw Python — so that users describe what they want and receive a ready-to-run workflow.

Generated workflows are execution-portable: they run embedded (SQLite store, no infra), in a user-supplied sandbox, or against an external durable backend (Temporal, DBOS, Restate). The SDK never forces a specific runtime.

The SDK's core engine design is **deterministic re-entry**: workflow bodies can be safely re-executed after crashes/deploys because every side effect is journaled and served from the journal on replay. This is analogous to Temporal's event-sourced execution model.

## Commands

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Install with FastAPI webhook support
pip install -e ".[dev,api]"

# Run all tests
pytest

# Run a single test file or test
pytest tests/test_runtime.py
pytest tests/test_runtime.py::test_basic_workflow

# Lint
ruff check src tests

# Type checking
mypy
```

## Architecture

### Core Execution Loop

The runtime (`runtime/engine.py`) drives execution:
1. Load `ExecutionRecord` and `Journal` from the store
2. Re-enter the workflow body; the journal short-circuits already-completed work
3. If body completes → `COMPLETED`; if raises `Suspend` → park with wake time or event name; if raises exception → `FAILED`

**Critical invariant:** Every durable operation must go through the `Context` API so results are journaled. Calling external services directly inside a workflow body will cause double-execution on replay.

### Layer Responsibilities

| Layer | Path | Purpose |
|-------|------|---------|
| **Runtime** | `runtime/engine.py` | Re-entry loop, lifecycle (run/resume/retry/replay/cancel), scheduler tick |
| **Context** | `runtime/context.py` | The only legal API from workflow code to the outside world (`step`, `sleep`, `wait_for_event`, `call_agent`, `spawn`, `gather`) |
| **Journal** | `runtime/journal.py` | Per-run log of durable operations; provides deterministic replay |
| **Workflow** | `runtime/workflow.py` | `WorkflowDefinition` wrapper + `@workflow` decorator |
| **Steps** | `steps/definition.py` | `@step` decorator — wraps async functions with retry, timeout, fallback |
| **State** | `state/` | Pluggable persistence: `ExecutionStore` protocol, `MemoryStore`, `SQLiteStore`, `MongoStore`, `PostgresStore` |
| **Agents** | `agents/` | `ModelProvider` protocol, `Tool` abstraction (steps/workflows-as-tools), guardrails, memory |
| **Triggers** | `triggers/` | Entry points: `Webhook`, `Schedule`, `Manual`, `Poll`, `Event`, `Chat`, `Email`, `SubWorkflow` |
| **Observability** | `observability/tracing.py` | `Tracer` protocol + `NoopTracer`; plug in OTel/Datadog/Honeycomb |

### Suspension Model

Workflows park themselves by raising `Suspend(wake_at=datetime)` or `Suspend(awaiting_event="name")`. The engine persists the suspension, and `runtime.tick()` / `runtime.resume(run_id)` re-enters the workflow at the next opportunity. This is how `ctx.sleep()` and `ctx.wait_for_event()` work internally.

### Public API Surface

`src/workflow_builder/__init__.py` re-exports ~10 symbols that form the user-facing API:
`Context`, `ExecutionResult`, `ExecutionStatus`, `Failure`, `OnError`, `Retry`, `Usage`, `Runtime`, `StepContext`, `step`, `workflow`.

All internal modules (~200+ classes) are implementation details.

### Workflow Coding Agent

The agent is the primary user-facing feature. It takes a natural-language description and produces a valid, runnable workflow file. Key design constraints for the agent and the code it generates:

- **Code style:** Generated workflows use `@workflow` + `@step` + `ctx.*` for all durable operations. Raw Python and third-party SDK calls belong inside `@step` bodies, never directly in the workflow body.
- **Tools available to the coding agent:** Any `@step` or `WorkflowDefinition` can be surfaced as a `Tool` via `tool_from_step()` / `tool_from_workflow()` / `coerce_tool()` in `agents/tools.py`. The agent's toolset is therefore the SDK itself — it can call steps and sub-workflows as tools to introspect capabilities.
- **Schema derivation:** Tool schemas are derived from function signatures + docstring `Args:` sections (`agents/tools.py::build_parameter_schema`). Keep docstrings accurate — they are the source of truth for the model.
- **Execution target:** Generated code must be runnable with just `pip install workflow-builder` and `MemoryStore` (no external infra). Sandbox or cloud execution is a deployment detail, not a code change.
- **Agent persistence:** Coding sessions are durable artifacts. The authoring session (spec, decision log, diagnostics) is itself a workflow run that can be resumed — use `session`/`persistent` agent classes, not ephemeral.

### Extension Points

- **Custom persistence:** Implement `state/base.py::ExecutionStore`
- **Custom tracing:** Implement `observability/tracing.py::Tracer`
- **Custom model providers:** Implement `agents/models.py::ModelProvider` (pricing table in `agents/models.py::PRICING`)
- **Custom triggers:** Subclass `TriggerSpec` from `triggers/base.py`

### System Design

The comprehensive system design is in `system-design.md` (15 chapters). Key references:
- **Chapter 3:** Programming model — three step classes, projectable code, SDK surface
- **Chapter 4:** Durable execution engine — durability port, journal, replay, Structural Replay
- **Chapter 7:** Visualization — AST-extracted skeleton, commit-time narration, CI golden-set checks
- **Chapter 9:** Storage — PostgreSQL and MongoDB schemas
- **Chapter 13:** Gap analysis — current code vs design target
- **Chapter 14:** Phasing — 11 implementation phases with exit criteria

### Implementation Phases

Detailed implementation plans are in `phases/`. Each file includes HLD, LLD, interfaces, directory structure, data flow diagrams, test plans, and multi-angle review:

- **`phases/phase-overview.md`** — Phase dependency graph, cross-cutting concerns, shared abstractions, canonical file layout
- **`phases/phase-1-core-library.md`** — Step classes (`@pure`/`@effect`/`@node`), `DurabilityBackend` protocol, journal hashes, `steps.lock`, Context API expansion, determinism lint, CLI
- **`phases/phase-2-agent-layer.md`** — `AgentExecutor` protocol, `AgentDefinition` registry, persistence classes, hook pipeline, budget enforcement, coding agent, mock run system
- **`phases/phase-3-integrations.md`** — Three-tier lazy disclosure, toolset generation pipeline, `ConnectionBroker`, `FilterSpec`, event routing, grant system, `loom certify`
- **`phases/phase-4-visualization.md`** — WGIR extraction (registry/AST/symbolic passes), skeleton-first narration, commit-time pipeline, CI golden set, canvas, run trace, time-travel
- **`phases/phase-5-production.md`** — `PostgresStore`, `MongoStore`, blob service, flow control, saga/compensation, `TemporalBackend`, HA/leader election, OTel, Structural Replay, RBAC
- **`phases/phase-6-ecosystem.md`** — n8n importer, template system, community toolset SDK, knowledge/memory/skill toolsets, drift detection, eval framework, VS Code extension
- **`phases/phase-7-small-model-compat.md`** — Tiered prompts, schema simplification, scaffolding engine, code validator, repair pipeline, model-stratified eval suite
- **`phases/phase-8-reference-workflows.md`** — 10 production workflows from n8n/Gumloop (lead outreach, content pipeline, inbox triage, CRM sync, social publisher, doc extraction, battle cards, meeting prep, Stripe ETL, PDF chatbot)
- **`phases/phase-9-mcp-server.md`** — MCP server with tools/resources/prompts for Claude Desktop, Cursor, Claude Code; stdio and SSE transports
- **`phases/phase-10-agent-framework-integrations.md`** — Bi-directional adapters for LangGraph, CrewAI, Pydantic AI, OpenAI Agents SDK, Claude SDK, Agno, AutoGen; conformance suite
- **`phases/phase-11-testing-dx.md`** — Property-based tests (Hypothesis), chaos tests, CI pipeline, interactive playground, quickstart scaffolding, actionable error diagnostics

### Key Design Principles

- **Determinism is a dial, not a foundation** — `@pure` → `@effect` → `Agent(...)` are dial positions; moving work between them is a code change on that step, not an architectural migration
- **Graph is projected from code** — decorators declare the graph; AST extraction produces WGIR; the model narrates a verified skeleton (cannot invent/hide steps)
- **Generate descriptions at commit, not on demand** — cached per commit; description diff = changelog for non-technical reviewers

### Determinism Rules

Workflow bodies must be deterministic across replays:
- Never call `datetime.now()` directly — use `ctx.now()`
- Never call `uuid.uuid4()` directly — use `ctx.uuid4()`
- Never call `random.*` directly — use `ctx.random()`
- Never access external state without `ctx.step()` — it won't be journaled
- Violating these raises `NondeterminismError` in strict mode

### Agent Backends (new)

`ctx.agent("prompt")` delegates to an `AgentBackend` configured on the Runtime:
- `BuiltInBackend` — uses LOOM's own agent turn loop + `ModelProvider`
- `LangChainBackend` (`agents/backends/langchain.py`) — wraps a LangGraph ReAct agent
- `AgnoBackend` (`agents/backends/agno.py`) — wraps an Agno agent
- `PydanticAIBackend` (`agents/backends/pydantic_ai.py`) — wraps a Pydantic AI agent

Each backend converts LOOM `Tool` objects to framework-native tools via one adapter function. The workflow code has zero framework imports.

### Three-Layer Lazy Tool System (new)

Tools are managed via `ToolsetRegistry` on the Runtime:
- **Layer 1 (registration)**: Only `ToolsetManifest` metadata stored. No tool code imported.
- **Layer 2 (discovery)**: Coding agent browses manifests via search/show/stub. Auto-generates docs from schemas.
- **Layer 3 (materialization)**: `Tool` objects created on demand via lazy resolver when `ctx.agent()` is called.

Key classes: `Toolset`, `ToolsetRegistry` in `agents/tool_registry.py`.

### Trigger Dispatcher (new)

`TriggerDispatcher` (`runtime/dispatcher.py`) fires cron/interval workflows at the scheduled time:
- Scans registered workflows for `Schedule`/`Interval` triggers
- Computes `next_fire_at` via `CronSchedule.next_after()`
- Fires runs via `Runtime.submit()` on each `tick()`
- Uses `TriggerStore` protocol for persistence (in-memory by default)

### Storage Backends (new)

| Store | Driver | Install |
|-------|--------|---------|
| `MemoryStore` | in-process | default |
| `SQLiteStore` | sqlite3 | default |
| `MongoStore` (`state/mongo.py`) | motor | `pip install workflow-builder[mongo]` |
| `PostgresStore` (`state/postgres.py`) | asyncpg | `pip install workflow-builder[postgres]` |

All implement: `ExecutionStore + TriggerStore + CacheStore + LockProvider`.

### Workflow Management Tools (new)

`agents/workflow_tools.py` provides 7 agent-facing tools: `list_workflows`, `get_workflow_info`, `run_workflow`, `schedule_workflow`, `list_runs`, `get_run_status`, `cancel_run`. These let a ReAct agent manage workflows via natural language.

### Pip Extras

```bash
pip install workflow-builder              # core
pip install workflow-builder[mongo]       # + MongoDB
pip install workflow-builder[postgres]    # + PostgreSQL
pip install workflow-builder[langchain]   # + LangChain/LangGraph
pip install workflow-builder[agno]        # + Agno
pip install workflow-builder[pydantic-ai] # + Pydantic AI
pip install workflow-builder[all]         # everything
```
