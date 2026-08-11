# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-08-11

### Added
- `TriggerDispatcher` for routing events to workflow triggers
- `MongoStore` — production storage backend using MongoDB (motor)
- `PostgresStore` — production storage backend using PostgreSQL (asyncpg)
- `AgentBackend` protocol — pluggable agent execution layer
- `LangChainBackend` — run agents via LangChain/LangGraph
- `AgnoBackend` — run agents via Agno framework
- `PydanticAIBackend` — run agents via Pydantic AI
- `ToolsetRegistry` with entry-point discovery (`loom_toolset` group)
- Jira toolset (search, create, update, transition, comment, assign)
- Confluence toolset (search, get page, create, update)
- `WorkflowCodingAgent` — ReAct agent that generates workflow code from natural language
- 14 cookbook examples covering sequential, parallel, durable sleep, error handling, human-in-the-loop, AI agents, coding agent, Jira, LangChain, Agno, PydanticAI, cron triggers, and workflow management
- Workflow management tools (`list_runs`, `get_run`, `cancel_run`, `retry_run`)

## [0.10.0]

### Added
- Agent framework integration adapters (LangGraph, CrewAI, Pydantic AI, OpenAI Agents SDK, Claude SDK, Agno, AutoGen)
- Conformance test suite for adapter correctness
- Bi-directional tool conversion (LOOM tools to/from framework-native tools)

## [0.9.0]

### Added
- MCP server with tools, resources, and prompts for Claude Desktop and Cursor
- stdio and SSE transports
- Workflow introspection via MCP resources

## [0.8.0]

### Added
- 10 reference workflows ported from n8n/Gumloop patterns
- Lead outreach, content pipeline, inbox triage, CRM sync, social publisher
- Doc extraction, battle cards, meeting prep, Stripe ETL, PDF chatbot
- Reference specs and reference test suite

## [0.7.0]

### Added
- Small model compatibility layer
- Tiered prompt templates (full, compact, minimal)
- Schema simplification for constrained-context models
- Scaffolding engine for guided code generation
- Code validator and repair pipeline

## [0.6.0]

### Added
- Template system for common workflow patterns
- n8n workflow importer
- Community toolset SDK
- Knowledge, memory, and skill toolsets
- Drift detection for toolset manifests
- Eval framework for generated workflows

## [0.5.0]

### Added
- `PostgresStore` and `MongoStore` (initial implementations)
- Blob service for large payloads
- Flow control: saga/compensation, fan-out/fan-in
- `TemporalBackend` durability port
- HA/leader election for scheduled triggers
- OpenTelemetry tracing integration
- Structural Replay for safe schema migration
- RBAC grant system

## [0.4.0]

### Added
- Graph visualization via WGIR (Workflow Graph Intermediate Representation)
- AST-based skeleton extraction from `@workflow`/`@step` decorators
- Skeleton-first narration: commit-time description generation
- CI golden-set checks for visualization output
- Canvas and run-trace views

## [0.3.0]

### Added
- Three-tier toolset disclosure (index, manifest, full docs)
- Toolset generation pipeline from OpenAPI specs
- `ConnectionBroker` for credential management
- `FilterSpec` for event routing predicates
- Grant system for capability-based access control
- `loom certify` CLI command

## [0.2.0]

### Added
- `AgentExecutor` protocol and `AgentDefinition` registry
- Agent persistence (sessions that survive restarts)
- Hook pipeline (pre/post agent turn)
- Budget enforcement (token and cost limits)
- Coding agent (initial version)
- Mock run system for testing agent workflows

## [0.1.0]

### Added
- Core runtime engine with deterministic re-entry
- Journal-based durable execution
- `@workflow` and `@step` decorators
- `Context` API: `ctx.step()`, `ctx.sleep()`, `ctx.wait_for_event()`, `ctx.spawn()`, `ctx.gather()`
- `MemoryStore` (in-memory, for tests)
- `SQLiteStore` (file-based, for local development)
- Step retry with configurable backoff
- Suspension model (sleep, wait-for-event)
- CLI (`workflow-builder` command)
- `Tracer` protocol with `NoopTracer`
