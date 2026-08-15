# PipesHub Workflow System — Architecture Reference

This document captures the key architecture of PipesHub's workflow system
to guide alignment between the `loomflow` pip package and PipesHub's
production deployment.

## High-Level Design (HLD)

```
┌─────────────────────────────────────────────────────────┐
│                    PipesHub Platform                     │
│                                                         │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  Chat UI │──│ Workflow API │──│  WorkflowService  │  │
│  │ (React)  │  │  (FastAPI)   │  │   (Application)   │  │
│  └──────────┘  └──────────────┘  └────────┬─────────┘  │
│                                           │             │
│                    ┌──────────────────────┤             │
│                    │                      │             │
│            ┌───────▼──────┐  ┌───────────▼──────────┐  │
│            │  TaskEngine  │  │ WorkflowBuilderAgent  │  │
│            │  (Scheduling │  │   (NL → Python code)  │  │
│            │   + CRUD)    │  │                       │  │
│            └──────┬───────┘  └───────────────────────┘  │
│                   │                                     │
│         ┌─────────┴──────────┐                          │
│         │                    │                          │
│  ┌──────▼──────┐  ┌─────────▼──────────┐               │
│  │ Scheduler   │  │ CodeWorkflowRunner  │               │
│  │ Loop        │  │  (Journaled exec)   │               │
│  │ (Triggers)  │  │                     │               │
│  └──────┬──────┘  └─────────┬──────────┘               │
│         │                   │                           │
│  ┌──────▼──────┐  ┌─────────▼──────────┐               │
│  │   Redis     │  │  PlatformBroker    │               │
│  │ (Triggers)  │  │  (Security/Grants) │               │
│  └─────────────┘  └────────────────────┘               │
└─────────────────────────────────────────────────────────┘
```

## Low-Level Design (LLD)

### Component Map

| Layer | Component | File | Purpose |
|-------|-----------|------|---------|
| API | REST endpoints | `api/routes/workflows.py` | CRUD, run, trigger management |
| Application | WorkflowService | `application/workflow_service.py` | Facade over TaskEngine |
| Application | TaskEngine | `tasks/application/engine.py` | Core scheduling + lifecycle |
| Codegen | WorkflowBuilderAgent | `codegen/agent.py` | NL → Python via LLM |
| Codegen | Verifier | `codegen/verifier.py` | Security lint, policy scan |
| Runtime | CodeWorkflowRunner | `runtime/code_runner.py` | Journaled execution |
| Runtime | PlatformBroker | `runtime/broker.py` | Capability gating, grants |
| Runtime | AgentRunner | `runtime/agent_runner.py` | Agent execution in workflow |
| Scheduling | SchedulerLoop | `tasks/runtime/scheduler_loop.py` | Trigger dispatch (Redis) |
| Scheduling | ScheduleCalculator | `tasks/domain/schedule_calculator.py` | DST-aware cron math |
| Domain | TaskDefinition | `tasks/domain/models.py` | Workflow metadata |
| Domain | TaskRun | `tasks/domain/models.py` | Execution record |
| Domain | TaskTrigger | `tasks/domain/models.py` | Trigger state |
| SDK | @workflow, @step | `sdk/decorators.py` | User-facing decorators |
| SDK | Ctx | `sdk/context.py` | Journaled primitives |
| SDK | Triggers | `sdk/triggers.py` | cron(), interval(), on_event() |

### Workflow Lifecycle

```
User describes workflow in chat
        │
        ▼
WorkflowBuilderAgent.generate()
  1. Build prompt (SDK ref + available tools)
  2. LLM generates Python code
  3. Verifier checks (syntax, security, policy)
  4. Repair loop (max 3 attempts)
  5. Extract IR (AST → WorkflowIR graph)
        │
        ▼
WorkflowVersion created (immutable code snapshot)
  - content_hash, tool_pins, agent_pins
  - Stored in ICodeStore (blob storage)
        │
        ▼
TaskEngine.add_trigger()
  - Cron/interval/event/webhook triggers attached
  - ScheduleCalculator computes next_fire_at
  - Stored in ITriggerStore (Redis sorted set)
        │
        ▼
SchedulerLoop.tick() (every 5 seconds)
  1. Reap expired leases
  2. Claim due triggers (atomic, Redis Lua)
  3. Apply fairness caps (max 10/org/tick)
  4. Dispatch → create TaskRun
  5. Reschedule (compute next next_fire_at)
        │
        ▼
CodeWorkflowRunner.execute()
  1. Resolve WorkflowVersion + source bundle
  2. Build Ctx with Journal + Broker
  3. Execute Python code in sandbox
  4. Journal all side-effects
  5. On suspend: park run (awaiting_input/event)
  6. On complete: record result
```

## Data Flow Diagrams

### DFD Level 0: Context

```
  ┌──────┐     NL spec     ┌─────────────┐    run     ┌──────────┐
  │ User │────────────────▶│   PipesHub   │──────────▶│ External │
  │      │◀────────────────│   Workflow   │◀──────────│  Tools   │
  └──────┘   results/UI    │   System    │   results  │(Jira,etc)│
                           └─────────────┘            └──────────┘
```

### DFD Level 1: Major Processes

```
  ┌────────┐       ┌────────────┐       ┌──────────────┐
  │ Create │──────▶│  Generate  │──────▶│   Schedule   │
  │Workflow│       │   Code     │       │  Triggers    │
  └────────┘       └────────────┘       └──────┬───────┘
                                               │
                                        ┌──────▼───────┐
                                        │   Execute    │
                                        │   (Runtime)  │
                                        └──────┬───────┘
                                               │
                                        ┌──────▼───────┐
                                        │   Monitor    │
                                        │   (Traces)   │
                                        └──────────────┘
```

### DFD Level 2: Trigger Dispatch

```
  SchedulerLoop.tick()
        │
        ├─── Reap expired leases (crashed schedulers)
        │
        ├─── claim_due(now, limit=50)  ──── Redis ZADD/ZRANGEBYSCORE
        │         │
        │    ┌────▼────┐
        │    │ Claimed  │
        │    │ Triggers │
        │    └────┬────┘
        │         │
        │    ┌────▼──────────────┐
        │    │ For each trigger: │
        │    │  1. Misfire check │
        │    │  2. Create TaskRun│
        │    │  3. Publish event │
        │    │  4. Reschedule    │
        │    └───────────────────┘
        │
        └─── Release leases
```

## Key Data Models

### TaskDefinition (Workflow)
- `task_id`, `org_id`, `title`, `description`
- `kind`: CODE or AGENT_TASK
- `current_version_id`: pinned WorkflowVersion
- `status`: DRAFT → ACTIVE → PAUSED → DISABLED
- `tool_names`, `connector_ids`, `collection_ids`
- `max_turns`, `timeout_seconds`

### TaskTrigger
- `trigger_id`, `task_id`, `kind` (CRON/INTERVAL/ONE_TIME/EVENT/WEBHOOK)
- `cron_expression`, `interval_seconds`, `fire_at`, `event_filter`
- `next_run_at` (UTC), `last_fire_at`
- `enabled`, `max_runs`, `run_count`
- `misfire_policy` (SKIP/RUN_ONCE/RUN_ALL)
- `lease_owner`, `lease_expires_at` (distributed lock)

### TaskRun
- `run_id`, `task_id`, `trigger_id`, `org_id`
- `status` (PENDING → RUNNING → SUCCEEDED/FAILED/AWAITING_INPUT)
- `attempt`, `completed_steps`, `failed_step_id`
- `trigger_payload`, `output_summary`, `error`
- `suspension_kind`, `awaiting_event_type`, `resume_deadline_at`

### WorkflowVersion (immutable)
- `version_id`, `version_number`, `workflow_id`
- `bundle_ref` (pointer to code in blob store)
- `ir` (WorkflowIR graph)
- `content_hash`, `tool_pins`, `agent_pins`

## SDK Primitives (Ctx)

| Method | Purpose | Journaled? |
|--------|---------|------------|
| `ctx.now()` | Deterministic datetime | Yes |
| `ctx.random()` | Deterministic float | Yes |
| `ctx.uuid()` | Deterministic UUID | Yes |
| `ctx.tool(name, args)` | Call external tool | Yes (broker-gated) |
| `ctx.agent(id)` | Get agent reference | Yes |
| `agent.run(goal)` | Execute agent | Yes |
| `ctx.create_agent(...)` | Create ephemeral agent | Yes |
| `ctx.sleep(seconds)` | Durable pause | Yes |
| `ctx.map(iter, fn)` | Parallel execution | Yes (branched) |
| `ctx.state.get/set` | Persistent KV | Best-effort |
| `ctx.log(...)` | Structured log | No |

## Security: PlatformBroker

- **RunGrant**: computed from TaskDefinition + WorkflowVersion, NOT from sandbox
- **Tool allowlist**: only tools in `version.tool_pins` can be called
- **Agent allowlist**: only agents in `version.agent_pins`
- **Taint tracking**: CLEAN → TAINTED (after external read) → blocks destructive tools
- **Dry-run mode**: WRITE steps simulated, not executed
- **Max calls counter**: prevents runaway loops

## Alignment: pip package vs PipesHub

| Concept | pip package (`loomflow`) | PipesHub |
|---------|--------------------------------|----------|
| Trigger dispatch | `TriggerDispatcher` (in-process) | `SchedulerLoop` (Redis leases) |
| Trigger store | `InMemoryTriggerStore` / `SQLite` / `Mongo` / `Postgres` | Redis sorted sets |
| Execution store | `ExecutionStore` protocol | Graph DB + Redis |
| Code store | `BlobService` (local filesystem) | S3/GCS blob store |
| Workflow CRUD | `Runtime` methods | `WorkflowService` facade |
| Security | None (trusted environment) | `PlatformBroker` (grants, taint) |
| Multi-tenancy | None | `org_id` scoping everywhere |
| Agent execution | `AgentBackend` protocol | `WorkflowAgentRunner` + broker |
| Code generation | `WorkflowCodingAgent` | `WorkflowBuilderAgent` |
| Fairness | None | Max triggers per org per tick |
| Misfire policy | None (fires on next tick) | SKIP/RUN_ONCE/RUN_ALL |

## Extension points for PipesHub

PipesHub should import `loomflow` as a dependency and:

1. Implement `ExecutionStore` with its graph DB adapter
2. Implement `TriggerStore` with its Redis adapter
3. Replace `TriggerDispatcher` with `SchedulerLoop`
4. Wrap `Runtime` in `WorkflowService` for multi-tenant facade
5. Add `PlatformBroker` middleware for security
6. Add `WorkflowAgentRunner` for agent integration
7. Keep the SDK (`@workflow`, `@step`, `Ctx`) unchanged
