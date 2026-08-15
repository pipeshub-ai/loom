# Architecture

## Overview

```
User Code          SDK Core               Storage
----------         --------               -------
@workflow  ------> Runtime
@step              |
                   +-> Context ---------> Journal
                   |   (ctx.step,          |
                   |    ctx.sleep,         v
                   |    ctx.agent)     ExecutionStore
                   |                  (Memory|SQLite|
                   +-> Scheduler       Mongo|Postgres)
                       |
                       +-> Triggers
                           (Cron, Webhook,
                            Poll, Event)
```

## Layer Responsibilities

| Layer | Path | Purpose |
|-------|------|---------|
| **Runtime** | `runtime/engine.py` | Re-entry loop, lifecycle (run/resume/retry/replay/cancel), scheduler tick |
| **Context** | `runtime/context.py` | The only legal API from workflow code (`step`, `sleep`, `wait_for_event`, `agent`, `spawn`, `gather`) |
| **Journal** | `runtime/journal.py` | Per-run log of durable operations; provides deterministic replay |
| **Workflow** | `runtime/workflow.py` | `WorkflowDefinition` wrapper and `@workflow` decorator |
| **Steps** | `steps/definition.py` | `@step` decorator with retry, timeout, fallback; step classes (`@pure`, `@effect`, `@node`) |
| **State** | `state/` | Pluggable persistence: `ExecutionStore` protocol, `MemoryStore`, `SQLiteStore`, `MongoStore`, `PostgresStore` |
| **Agents** | `agents/` | `AgentBackend` protocol, `BuiltInBackend`, LangChain/Agno/PydanticAI backends, coding agent |
| **Triggers** | `triggers/` | Entry points: `Webhook`, `Schedule`, `Interval`, `Manual`, `Poll`, `OnEvent`, `Chat`, `Form`, `EmailInbox` |
| **Toolsets** | `toolsets/` | `ToolsetManifest`, `ToolsetCatalog`, `ToolsetRegistry`, Jira/Confluence integrations |
| **Observability** | `observability/` | `Tracer` protocol with `NoopTracer`; plug in OpenTelemetry, Datadog, or Honeycomb |

## Execution Flow

1. `runtime.run(my_workflow, input)` creates an `ExecutionRecord` and `Journal`, saves them to the store
2. The runtime enters the workflow body, providing a `Context` object
3. Each `ctx.step(fn, args)` call checks the journal:
   - **First run:** executes the step, records the result in the journal
   - **Replay:** returns the recorded result without re-executing
4. If the body completes, the run is marked `COMPLETED`
5. If a `Suspend` is raised (from `ctx.sleep()` or `ctx.wait_for_event()`), the run is parked
6. If an exception propagates, the run is marked `FAILED`

## Suspension Model

Workflows park themselves by raising `Suspend`:

```python
from loom import Context, step, workflow


@step
async def send_request(message: str) -> str:
    """Stand-in for the real work."""
    return message


@step
async def notify(message: str) -> str:
    """Stand-in for the real work."""
    return message


@workflow(name="approval_flow")
async def approval_flow(ctx: Context) -> str:
    await ctx.step(send_request, "Please approve")

    # Parks the workflow until "approved" event arrives
    event = await ctx.wait_for_event("approved")

    await ctx.step(notify, f"Approved by {event['user']}")
    return "done"
```

Internally:
- `ctx.sleep(seconds)` raises `Suspend(wake_at=now + timedelta(seconds=seconds))`
- `ctx.wait_for_event(name)` raises `Suspend(awaiting_event=name)`
- The engine persists the suspension to the store
- `runtime.tick()` checks for runs whose `wake_at` has passed and resumes them
- `runtime.resume(run_id, event=...)` resumes a run waiting for an event

## Determinism Rules

Workflow bodies must produce the same sequence of durable operations on every replay. Breaking determinism means the journal cannot be replayed correctly.

**Do not use directly:**

| Forbidden | Use instead |
|-----------|-------------|
| `datetime.now()` | `ctx.now()` |
| `uuid.uuid4()` | `ctx.uuid4()` |
| `random.*` | `ctx.random()` |
| Direct HTTP/DB calls | Wrap in `@step` and call via `ctx.step()` |

All external state must flow through `ctx.step()` so it is journaled. Code that violates these rules raises `NondeterminismError` in strict mode.

## Step Classes

Steps are classified by their side-effect profile:

| Decorator | Class | Re-execution | Use case |
|-----------|-------|-------------|----------|
| `@pure` | Pure computation | Safe to re-run | Data transforms, validation |
| `@effect` | Side-effecting I/O | Journal required | API calls, database writes |
| `@node` | Agent node | Journal + budget | LLM inference |
| `@step` | Auto-classified | Journal by default | General purpose (alias for `@effect`) |

## Public API Surface

The package exports a small set of symbols from `loom.__init__`:

```python
from loom import (
    Context, Runtime, workflow, step,          # Core
    pure, effect, node,                        # Step classes
    ExecutionResult, ExecutionStatus, Failure,  # Results
    Retry, OnError, Usage,                     # Configuration
    StepContext, CachePolicy, StepClass,       # Step details
    DurabilityBackend, EmbeddedBackend,        # Backend ports
    FilterSpec, ToolsetManifest,               # Extensions
    register_toolset, derive_grants, GrantSet, # Registration
    resource, Depends, ResourceScope,          # Resources
    Batch, Page, Result,                       # Types
)
```

All other classes and modules are implementation details.
