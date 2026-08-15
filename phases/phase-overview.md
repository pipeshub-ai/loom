# LOOM Implementation Phases — Overview

**How the eleven implementation phases connect, what each delivers, and how an agent should navigate them.**

---

## Reading This Document

This file is the map. Each phase has its own detailed implementation file (`phase-N-*.md`) with HLD, LLD, interfaces, directory structure, data flow diagrams, implementation steps, test plans, and known gaps. Read this overview first, then dive into the phase you're implementing.

**Key principle:** The system design (`system-design.md`) is the _what_. These phase files are the _how_. The system design may have gaps, wrong abstractions, or missing pieces. Each phase file calls out known risks and expected deviations.

---

## Phase Dependency Graph

```
Phase 1 ─── Core Library
   │
   ├──▶ Phase 2 ─── Agent Layer
   │        │
   │        ├──▶ Phase 3 ─── Integrations & Toolsets
   │        │        │
   │        │        ├──▶ Phase 4 ─── Visualization & Explainability
   │        │        │
   │        │        ├──▶ Phase 7 ─── Small Model Compatibility
   │        │        │        │
   │        │        │        └──▶ Phase 8 ─── Reference Workflows (validates all)
   │        │        │
   │        │        └──▶ Phase 9 ─── MCP Server
   │        │
   │        ├──▶ Phase 6 ─── Ecosystem (partial: eval framework, templates)
   │        │
   │        └──▶ Phase 10 ── Agent Framework Integrations
   │
   ├──▶ Phase 5 ─── Production Hardening (can start after Phase 1; full after Phase 3)
   │        │
   │        └──▶ Phase 6 ─── Ecosystem (full: community toolsets, importers)
   │
   └──▶ Phase 11 ── Testing Infrastructure & DX (spans all phases)
```

### Phase Summary

| Phase | Name | Delivers |
|-------|------|----------|
| **1** | Core Library | Durable engine, step classes, journal, Context API, CLI |
| **2** | Agent Layer | AgentExecutor, registry, persistence, hooks, coding agent |
| **3** | Integrations & Toolsets | Three-tier disclosure, generation pipeline, event routing |
| **4** | Visualization | WGIR extraction, narration, canvas, run trace |
| **5** | Production Hardening | PostgreSQL/MongoDB, flow control, HA, OTel, Structural Replay |
| **6** | Ecosystem | n8n importer, templates, community toolsets, eval, VS Code |
| **7** | Small Model Compat | Tiered prompts, scaffolding, schema simplifier, repair pipeline, eval |
| **8** | Reference Workflows | 10 production workflows from n8n/Gumloop, validates SDK end-to-end |
| **9** | MCP Server | MCP tools/resources/prompts for Claude, Cursor, Claude Code |
| **10** | Agent Frameworks | LangGraph, CrewAI, Pydantic AI, OpenAI, Claude, Agno, AutoGen adapters |
| **11** | Testing & DX | Property tests, chaos tests, CI, playground, quickstart, diagnostics |

### Dependency Rules

| Phase | Hard Prerequisites | Soft Prerequisites |
|-------|-------------------|--------------------|
| **Phase 1** | None | — |
| **Phase 2** | Phase 1 complete | — |
| **Phase 3** | Phase 1 complete, Phase 2 partial (tool system) | Phase 2 agent sessions for authoring |
| **Phase 4** | Phase 1 complete (decorators, registry) | Phase 2 (agent nodes), Phase 3 (toolset nodes) |
| **Phase 5** | Phase 1 complete | Phase 2-3 for agent/toolset storage schemas |
| **Phase 6** | Phase 2-3 complete | Phase 4-5 for full feature set |
| **Phase 7** | Phase 2 (agent/coding agent), Phase 3 (toolsets) | Phase 6 eval framework |
| **Phase 8** | Phase 1-3 (core + agents + toolsets) | Phase 7 (small model eval) |
| **Phase 9** | Phase 1-3 (runtime, store, toolsets) | Phase 4 (WGIR resources), Phase 8 (examples) |
| **Phase 10** | Phase 2 (AgentExecutor protocol) | Phase 9 (MCP for Mastra) |
| **Phase 11** | Phase 1 (core test targets) | All phases (comprehensive coverage) |

### Parallelization Opportunities

- **Phase 2 + Phase 5 (early):** PostgreSQL/MongoDB store implementations can start once Phase 1's `ExecutionStore` protocol is stable, even before Phase 2 ships.
- **Phase 3 + Phase 4 (partial):** WGIR extraction (Phase 4) only needs Phase 1's decorators and registry — it can start before Phase 3 toolsets are ready.
- **Phase 5 (flow control) + Phase 4 (canvas):** Independent subsystems with no shared interfaces.
- **Phase 7 + Phase 9:** Small model compat and MCP server have no shared code. Can be built in parallel after Phase 3.
- **Phase 10 + Phase 8:** Framework adapters and reference workflows are independent. Can be built in parallel after Phase 3.
- **Phase 11:** Testing infrastructure can start from Phase 1 and grow with each phase. Property tests and CI pipeline should start early.

---

## Cross-Cutting Concerns (Apply to Every Phase)

### 1. One-Way Doors (Baked into Phase 1, Never Changed)

These decisions from `system-design.md` Chapter 1.5 must be respected in every phase:

| ID | Decision | Consequence |
|----|----------|-------------|
| D1 | Step identity: stable name + `steps.lock` | Every new step type must register with stable id |
| D2 | Determinism contract: strict from day one | Every new `ctx.*` method must be journal-safe |
| D4 | Agent persistence: three classes with journaled ids | Agent storage must carry `agent_id`, `session_id` |
| D5 | Payload addressing: reference-first (>256KB → blob) | Every payload write must check size threshold |
| D6 | Tenancy: `tenant_id` on every row | Every new table/collection gets `tenant_id` |
| D7 | Authorization: gateway-side, tokens never enter worker | Credentials flow through gateway, not step code |
| D12 | Error taxonomy: fixed root hierarchy | New errors must be leaves under existing roots |
| D13 | Idempotency: caller-supplied for writes | Every new write effect needs `idempotency=` lint |

### 2. Testing Strategy (Per Phase)

Each phase adds tests at three layers:

| Layer | What | Speed | Location |
|-------|------|-------|----------|
| **Unit** | Individual functions, pure logic, protocol implementations | ms | `tests/unit/` |
| **Integration** | Store implementations, engine + store, multi-step flows | 1-10s | `tests/integration/` |
| **E2E / Replay** | Full workflow runs, crash recovery, cross-phase scenarios | 2-30s | `tests/e2e/` |
| **Property** | Invariant-based tests via Hypothesis (Phase 11) | 1-30s | `tests/property/` |
| **Chaos** | Fault injection, crash recovery (Phase 11) | 5-60s | `tests/chaos/` |

**Cross-phase test rule:** When Phase N completes, all Phase 1..N-1 tests must still pass. New phases must not break existing behavior.

### 3. Logging Convention

All modules use Python's `logging` with a `workflow.*` namespace:

```python
logger = logging.getLogger("workflow.engine")      # runtime
logger = logging.getLogger("workflow.journal")      # journal
logger = logging.getLogger("workflow.agent")        # agent system
logger = logging.getLogger("workflow.toolset")      # toolsets
logger = logging.getLogger("workflow.graph")        # WGIR extraction
logger = logging.getLogger("workflow.store")        # storage
logger = logging.getLogger("workflow.mcp")          # MCP server (Phase 9)
logger = logging.getLogger("workflow.integration")  # framework integrations (Phase 10)
```

**Levels:**
- `DEBUG`: Journal entries, step scheduling, replay cache hits
- `INFO`: Run lifecycle (started, completed, failed, suspended), agent turns
- `WARNING`: Retry attempts, degraded paths, config overrides
- `ERROR`: Unhandled exceptions, replay divergence, store failures

### 4. Error Handling Pattern

Every phase follows the same escalation path:

```
step retry → region on_error → flow on_error handler → DLQ
```

New error types must be leaves in the hierarchy from `core/exceptions.py`. The root shape (D12) is frozen in Phase 1. Phase 11 adds actionable diagnostics to all error messages.

### 5. Configuration Pattern

All configuration flows through `pydantic-settings`:

```python
from pydantic_settings import BaseSettings

class LoomSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOM_")
    profile: str = "embedded"
    store_url: str = "sqlite:///.loom/journal.db"
    # Phase-specific settings added per phase
```

### 6. Public API Surface Management

The public API (`__init__.py`) grows incrementally per phase:

| Phase | New Public Symbols |
|-------|--------------------|
| 1 | `flow`, `pure`, `effect`, `node`, `ctx`, `Loom`, `Result`, `Batch` |
| 2 | `Agent`, `AgentDefinition`, `Artifact`, `Refusal`, `AgentLimits` |
| 3 | `resource`, `Depends`, `Page`, `ToolsetManifest`, `register_toolset` |
| 4 | (No new public symbols — visualization is a build tool + CLI) |
| 5 | `Blob`, (PostgresStore, MongoStore as optional extras) |
| 6 | (No new public symbols — ecosystem is packages + tools) |
| 7 | (No new public symbols — internal to agent system) |
| 8 | (No new public symbols — reference workflows are examples) |
| 9 | (No new public symbols — MCP server is a separate entry point) |
| 10 | (No new public symbols — adapters are optional extras) |
| 11 | (No new public symbols — testing/DX infrastructure) |

---

## What Exists Today (v0.2.0)

Current codebase has foundational pieces that Phase 1 must refactor:

| Component | Current State | Phase 1 Target |
|-----------|--------------|----------------|
| `@step` decorator | Single class, no `@pure`/`@effect` distinction | Three step classes with `klass` field |
| `@workflow` decorator | Works, but no `@flow` alias | Keep `@workflow`, add `@flow` alias |
| `Context` | Has `step`, `sleep`, `wait_for_event`, `gather`, `spawn`, `call_agent` | Add `map`, `race`, `state`, `store`, `now`, `uuid`, `random`, `progress` |
| `Journal` | In-memory list; `JournalEntry` lacks hashes | Add `contract_hash`, `closure_hash`; `steps.lock` generation |
| `Runtime` | Tightly coupled to `ExecutionStore` | Extract `DurabilityBackend` protocol |
| `ExecutionStore` | Protocol exists with `MemoryStore`, `SQLiteStore` | Expand protocol; ensure implementations match |
| `Exceptions` | Partial hierarchy | Complete to D12 specification |
| `Triggers` | `Webhook`, `Schedule`, `Manual`, `Poll`, `Event`, `Chat`, `Email`, `SubWorkflow` | Normalize all to `TriggerEvent`; add `SubFlow`, `OnComplete` |
| Agent system | `ModelProvider`, `Tool`, guardrails, memory, messages, output, result | Restructure for `AgentExecutor` protocol (Phase 2) |

---

## Shared Abstractions (Used Across Phases)

These are foundational types that Phase 1 establishes and later phases depend on:

### StepKey — Universal Join Key (D1)

```python
@dataclass(frozen=True)
class StepKey:
    step_id: str           # stable identity
    contract_hash: str     # Pydantic schema hash
    closure_hash: str      # transitive body hash
    attempt: int           # retry attempt number
```

Links: code ↔ journal ↔ trace span ↔ canvas node ↔ eval case.

### DurabilityBackend Protocol

```python
class DurabilityBackend(Protocol):
    def capabilities(self) -> Capabilities: ...
    async def step(self, key: StepKey, fn: Callable, *, policy: StepPolicy) -> Any: ...
    async def sleep(self, key: StepKey, until: datetime) -> None: ...
    async def wait(self, key: StepKey, name: str, corr: str, timeout: timedelta | None) -> Any: ...
    async def signal(self, target: RunRef, name: str, payload: Any) -> None: ...
    async def child(self, key: StepKey, flow: FlowRef, inp: Any, *, detached: bool) -> Any: ...
    async def cancel(self, target: RunRef) -> None: ...
    # Tier 2 — optional
    async def history(self, run: RunRef) -> Iterable[JournalEntry]: ...
    async def continue_as_new(self, seed: Any) -> NoReturn: ...
```

### AgentExecutor Protocol (Phase 2 — used by Phases 7, 10)

```python
class AgentExecutor(Protocol):
    async def execute(
        self,
        input: str,
        tools: list[Tool],
        output_type: type | None = None,
        settings: dict | None = None,
        context: Any = None,
    ) -> Any: ...
```

All Phase 10 framework adapters implement this protocol. Phase 7 uses it for model-stratified evaluation.

### ExecutionStore Protocol

Defined in `state/base.py`. Every storage implementation (Memory, SQLite, PostgreSQL, MongoDB) implements this. Phase 1 stabilizes the protocol; later phases add implementations.

### Tracer Protocol

Defined in `observability/tracing.py`. `NoopTracer` ships in Phase 1. `OTelTracer` arrives in Phase 5.

---

## File Layout (Canonical, All Phases)

```
src/loom/
├── core/                   # Phase 1: Foundation
│   ├── models.py           # ExecutionRecord, StepRecord, Usage, Status enums
│   ├── types.py            # Duration, JSONDict, JSONValue, StepKey
│   ├── exceptions.py       # Full error taxonomy (D12)
│   ├── retry.py            # Retry, OnError
│   ├── serde.py            # Encode/decode for journal payloads
│   ├── ids.py              # Fingerprinting, hash computation
│   └── diagnostics.py      # Actionable error messages (Phase 11)
├── runtime/                # Phase 1: Durable execution engine
│   ├── engine.py           # Runtime: run/resume/retry/replay/cancel
│   ├── context.py          # Context[DepsT]: the durable API
│   ├── workflow.py         # WorkflowDefinition + @workflow/@flow decorator
│   ├── journal.py          # JournalEntry, Journal
│   ├── backend.py          # DurabilityBackend protocol (NEW in Phase 1)
│   └── determinism.py      # Determinism lint rules
├── steps/                  # Phase 1: Step definitions
│   ├── definition.py       # StepDefinition + @step/@pure/@effect decorators
│   ├── context.py          # StepContext
│   └── lock.py             # steps.lock generation (NEW in Phase 1)
├── agents/                 # Phase 2+7: Agent abstraction layer
│   ├── executor.py         # AgentExecutor protocol (Phase 2)
│   ├── definition.py       # AgentDefinition registry (Phase 2)
│   ├── runtime.py          # BuiltInAgentRuntime (Phase 2)
│   ├── capability.py       # ModelTier, detect_capabilities (Phase 7)
│   ├── prompts.py          # PromptLibrary, tiered prompts (Phase 7)
│   ├── schema_simplifier.py # Schema simplification for small models (Phase 7)
│   ├── scaffolding.py      # ScaffoldingEngine, templates (Phase 7)
│   ├── repair.py           # CodeValidator, RepairPipeline (Phase 7)
│   ├── examples.py         # Few-shot example bank (Phase 7)
│   ├── models.py           # ModelProvider, ModelRequest, ModelResponse
│   ├── tools.py            # Tool, tool_from_step, tool_from_workflow
│   ├── guardrails.py       # Hook pipeline
│   ├── limits.py           # AgentLimits
│   ├── memory.py           # Session and persistent memory
│   ├── messages.py         # Message types
│   ├── output.py           # Structured output validation
│   └── result.py           # AgentResult, Refusal
├── triggers/               # Phase 1: Trigger types
│   ├── base.py             # TriggerSpec protocol, TriggerEvent
│   ├── specs.py            # Webhook, Schedule, Manual, etc.
│   ├── cron.py             # CronSchedule
│   ├── filter.py           # FilterSpec (Phase 3)
│   └── routing.py          # Event routing / pub-sub (Phase 3)
├── state/                  # Phase 1+5: Pluggable persistence
│   ├── base.py             # ExecutionStore protocol
│   ├── memory.py           # MemoryStore (tests)
│   ├── sqlite.py           # SQLiteStore (embedded)
│   ├── postgres.py         # PostgresStore (Phase 5)
│   └── mongo.py            # MongoStore (Phase 5)
├── toolsets/               # Phase 3: Toolset system
│   ├── catalog.py          # Toolset catalog service
│   ├── manifest.py         # ToolsetManifest
│   ├── gateway.py          # Toolset gateway
│   ├── registry.py         # Toolset registration (register_toolset)
│   ├── generator.py        # OpenAPI/MCP/GraphQL → manifest pipeline
│   └── certify.py          # loom certify (automated certification)
├── graph/                  # Phase 4: WGIR extraction
│   ├── extractor.py        # Three-pass extraction
│   ├── wgir.py             # WGIR data model
│   ├── explainer.py        # Commit-time narration
│   ├── canvas.py           # GraphPatch editing
│   └── export.py           # Mermaid/SVG export
├── integrations/           # Phase 10: Agent framework adapters
│   ├── langgraph_adapter.py
│   ├── pydantic_ai_adapter.py
│   ├── openai_agents_adapter.py
│   ├── claude_adapter.py
│   ├── crewai_adapter.py
│   ├── agno_adapter.py
│   ├── autogen_adapter.py
│   ├── react_adapter.py
│   └── conformance.py      # Adapter conformance test suite
├── mcp_server/             # Phase 9: MCP server
│   ├── __init__.py          # create_server() factory
│   ├── __main__.py          # CLI entry point
│   ├── bridge.py            # RuntimeBridge
│   ├── tools.py             # MCP tool definitions
│   ├── resources.py         # MCP resource definitions
│   └── prompts.py           # MCP prompt templates
├── resources/              # Phase 3: Resource injection
│   ├── base.py             # @resource decorator, Depends
│   └── pool.py             # Connection pooling, health checks
├── observability/          # Phase 1+5: Tracing
│   ├── tracing.py          # Tracer protocol, NoopTracer
│   └── otel.py             # OTelTracer (Phase 5)
├── security/               # Phase 5: Security
│   ├── grants.py           # Grant derivation
│   └── rbac.py             # RBAC
├── eval/                   # Phase 6+7: Evaluation framework
│   ├── runner.py            # EvalRunner
│   ├── model_eval.py        # Model-stratified eval (Phase 7)
│   └── datasets/            # Eval datasets
├── cli/                    # Phase 1+11: CLI
│   ├── __init__.py          # Main CLI entry point
│   ├── playground.py        # Interactive playground (Phase 11)
│   └── init_template.py     # loom init scaffolding (Phase 11)
├── settings.py             # Phase 1: LoomSettings (pydantic-settings)
└── __init__.py             # Public API surface
examples/
├── reference/              # Phase 8: 10 production-quality workflows
│   ├── wf01_lead_outreach.py
│   ├── wf02_content_pipeline.py
│   ├── ...
│   └── wf10_pdf_chatbot.py
├── integrations/           # Phase 10: Framework integration examples
│   ├── langgraph_in_loom.py
│   ├── loom_in_langgraph.py
│   ├── ...
│   └── mastra_via_mcp.md
└── reference_specs/        # Phase 8: NL specs for eval
tests/
├── unit/
├── integration/
├── e2e/
├── property/               # Phase 11: Hypothesis tests
├── chaos/                  # Phase 11: Fault injection tests
├── load/                   # Phase 11: Stress tests
└── eval/                   # Phase 7-8: Eval datasets
```

---

## Implementation Order Within Each Phase

Every phase follows this sequence:

1. **Protocols and types first.** Define the interfaces. Nothing implements them yet.
2. **Core implementation.** Build the simplest working version that satisfies the protocol.
3. **Integration.** Wire into the engine, context, or CLI.
4. **Tests.** Unit tests for the implementation, integration tests for wiring, replay tests for durability.
5. **Lint rules.** If the phase introduces new constraints, add `loom check` diagnostics.
6. **CLI commands.** Surface the feature via `loom` CLI.
7. **Documentation.** Update CLAUDE.md and any relevant docs.

---

## Cross-Phase Integration Tests

After each phase ships, run the combined test suite:

| Test | What It Verifies | Added After |
|------|-----------------|-------------|
| `test_full_workflow_lifecycle` | Create → run → suspend → resume → complete | Phase 1 |
| `test_replay_after_crash` | Kill mid-step → resume → no duplicate effects | Phase 1 |
| `test_agent_in_workflow` | Agent step within a durable workflow | Phase 2 |
| `test_toolset_in_agent` | Agent uses toolset ops as tools | Phase 3 |
| `test_graph_extraction_end_to_end` | Code → WGIR → narration → graph.json | Phase 4 |
| `test_postgres_full_lifecycle` | Same lifecycle tests against PostgreSQL | Phase 5 |
| `test_structural_replay` | Edit code → replay with hashes → green/amber/red | Phase 5 |
| `test_community_toolset_install` | pip install → auto-register → agent discovers | Phase 6 |
| `test_small_model_generates_workflow` | 8B model → valid workflow from NL spec | Phase 7 |
| `test_reference_workflows_all_pass` | All 10 reference workflows run to completion | Phase 8 |
| `test_mcp_round_trip` | list → run → status → complete via MCP | Phase 9 |
| `test_langgraph_in_loom_workflow` | LangGraph agent inside durable LOOM workflow | Phase 10 |
| `test_crash_recovery_no_duplicates` | Property: crash at any point → resume → correct | Phase 11 |

---

## Known Gaps in System Design

The system design (`system-design.md`) is comprehensive but not perfect. Each phase file flags specific gaps. Common themes:

1. **Naming inconsistency:** Design uses `@flow` but existing code uses `@workflow`. Decision: keep `@workflow` as primary, add `@flow` as alias.
2. **DurabilityBackend vs Runtime coupling:** The extraction described in §13.2.3 is correct but the migration path needs careful handling of the existing `Runtime` class.
3. **Agent persistence storage:** The `agent_session` table schema is defined but the in-memory/SQLite representations need design.
4. **Toolset gateway:** Described as a separate service in the HLD but needs to work in embedded mode too (in-process, no gateway).
5. **WGIR data model:** Node and edge kinds are listed but the full Pydantic model isn't specified in the design.
6. **CLI commands:** Many `loom` commands are mentioned (`dev`, `check`, `search`, `show`, `stub`, `pin`, `certify`) but their argument signatures aren't specified.
7. **Event bus abstraction:** The design mentions Kafka/NATS/Redis Streams but doesn't define the abstraction protocol for embedded mode.
8. **Small model support:** System design assumes large model capabilities. Phase 7 addresses this with tiered prompts and scaffolding.
9. **MCP integration:** Not in original system design. Phase 9 adds MCP as a first-class integration point.
10. **Framework adapter protocol:** Phase 2 defines `AgentExecutor` but doesn't specify adapter patterns for all frameworks. Phase 10 provides concrete implementations.

These gaps are expected. The phase files provide concrete implementations where the design leaves room.

---

## How to Use These Files

**If you are implementing Phase N:**
1. Read this overview for context.
2. Read `phase-N-*.md` for the detailed implementation plan.
3. Verify Phase N-1 tests pass before starting.
4. Follow the implementation order within the phase file.
5. Run cross-phase integration tests after completing.

**If you are reviewing code:**
1. Check the phase file for the expected interfaces and test plan.
2. Verify one-way doors (D1-D13) are respected.
3. Verify no Phase N code breaks Phase 1..N-1 tests.

**If the system design and a phase file disagree:**
The phase file wins for implementation details. The system design wins for architectural principles and one-way doors. Flag disagreements for resolution.
