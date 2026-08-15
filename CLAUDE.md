# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**workflow-builder** ("LOOM") is a pip-installable, **library-first** durable execution SDK for AI-powered workflows. The primary deliverable is a **Workflow Coding Agent**: an LLM-powered agent that _authors_ workflow code — mixing third-party SDK calls, workflow constructs (`@workflow`, `@step`, `ctx.*`), and raw Python — so that users describe what they want and receive a ready-to-run workflow.

Generated workflows are execution-portable: they run embedded (SQLite store, no infra), in a user-supplied sandbox, or against an external durable backend (Temporal, DBOS, Restate). The SDK never forces a specific runtime.

The SDK's core engine design is **deterministic re-entry**: workflow bodies can be safely re-executed after crashes/deploys because every side effect is journaled and served from the journal on replay. This is analogous to Temporal's event-sourced execution model.

## CLI

Installed as both `loom` and `workflow-builder`.

```bash
# authoring
loom check flows/order.py              # write order.graph.json + order.description.md
loom check flows/order.py --fail-on-change   # CI: fail if the committed graph is stale
loom graph flows/order.py --format react-flow
loom describe flows/order.py
loom init my-project

# running
loom run onboard --input '{"email": "a@b.com"}'   # or -i @payload.json, or a bare string
loom run onboard --follow                          # stream steps as they complete
loom runs --status failed
loom show <run> / loom watch <run>

# acting on a run
loom approve <run> refund [--reject]
loom send <run> <event> '{"token": "x"}'
loom cancel / retry / replay <run>

# serving
loom workflows
loom publish onboard
loom serve --port 8000
loom mcp                               # serve over MCP, needs [mcp]
loom ui                                # terminal UI, needs [tui]
```

**Finding a workflow.** `loom run path.py::name` always works. `loom run name`
resolves against `[tool.loom] modules` in `pyproject.toml`. `--server URL`
imports nothing and asks a running server — every run-side command takes it, and
nothing else about the command changes, because local and remote go through one
`RuntimeFacade`.

**Exit codes are the contract:** `0` completed, `1` failed, `2` usage,
**`3` suspended**, `4` cancelled. The third one matters — a run parked on a human
has neither succeeded nor failed, and collapsing it into either makes calling
scripts do the wrong thing. A suspended run prints the command that unparks it.

**`--json` on every command**, so output pipes into `jq`. Human output uses
`rich` when installed (`[cli]` extra) and strips styling when not a TTY.

**`loom ui`** (`[tui]` extra) is three panes: runs, the selected run's journal,
and a queue of runs parked on a human that you can approve in place. That last
one has no good non-interactive equivalent — otherwise finding those runs means
knowing they exist and querying for them.

`loom check` runs the registry pass (decorated steps: kind, types, retry) and
the AST pass (control flow, `ctx.*` calls), merges them, and narrates the result
— then verifies the narration mentions every node, so a model cannot invent or
hide a step. Both outputs are meant to be committed: the graph makes structural
changes reviewable in a diff, the description makes them readable by someone who
does not read Python.

## MCP Server

`loom mcp` (`[mcp]` extra) serves the same Runtime to Claude Code, Claude
Desktop, and Cursor over the Model Context Protocol, built on the official SDK's
`FastMCP`. `--transport` picks `stdio` (default), `http`, or `sse`. Under stdio,
**stdout is the protocol channel** — anything printed there corrupts the session,
so status goes to stderr.

```bash
claude mcp add loom -- loom mcp --module flows.py
```

**One port, two clients.** `workflow_builder/facade.py` defines `RuntimeFacade`,
the protocol the CLI and the MCP server both depend on, with `LocalFacade`
(in-process Runtime) and `RemoteFacade` (`LoomClient`) implementing it. Add a
capability there and both surfaces get it. `RuntimeBridge` is a deprecated alias
for `LocalFacade`.

**Layering inside `mcp_server/`:** `tools.py`, `resources.py`, and `prompts.py`
are plain coroutines over a facade and import nothing from `mcp`; `server.py` is
the only module that does. That is what makes the capabilities testable without a
protocol in the picture — see the three test levels in `tests/test_mcp_server.py`.

**Write for a model, not a human.** Two states a model reliably misreads, both
handled at the boundary rather than left to the caller: a *suspended* run comes
back with `waiting_for`, the exact `next_action` call that unparks it, and a note
that it is not failure; and `retry_run` (from the failure, current code) vs
`replay_run` (from the journal, no side effect repeated) is spelled out in the
server instructions. Errors return a payload, never a raise — a raise aborts the
model's turn. Inputs are validated against the workflow's advertised
`input_schema()` before a run starts, because a shape mismatch otherwise surfaces
as an `AttributeError` from inside a step, which reads like a broken workflow.

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

**Journal payloads.** Anything served back on replay — step outputs, side-effect
values, event payloads — must be serializable; a value that is not raises
`SerializationError` rather than being replaced by a placeholder that later
replays as if it were real. Step *inputs* are recorded for humans and never
replayed, so those still degrade to `{"__unserializable__": ...}`. Binary values
round-trip as base64; with `Runtime(blobs=BlobService(...))`, payloads over the
service's threshold are stored by content hash and referenced as `blob:<sha256>`.

**Terminal paths.** On failure or cancellation the engine runs the
`ctx.compensate()` stack in LIFO order before the record goes terminal; handlers
that themselves fail are logged and recorded in
`record.metadata["compensation_failures"]` without masking the original error.
`ctx.continue_as_new(seed)` completes the current run and starts a successor with
a clean journal, linked by `root_run_id` and `metadata["continued_as"]`.

### Layer Responsibilities

| Layer | Path | Purpose |
|-------|------|---------|
| **Runtime** | `runtime/engine.py` | Re-entry loop, lifecycle (run/resume/retry/replay/cancel), scheduler tick |
| **Context** | `runtime/context.py` | The only legal API from workflow code to the outside world (`step`, `sleep`, `wait_for_event`, `agent`, `child`, `gather`, `publish`, `report`, `state`) |
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
- **Configuring it:** `WorkflowCodingAgent(instructions=...)` replaces `DEFAULT_SYSTEM_PROMPT` outright; `extra_instructions=` appends house rules; `allowed_packages={...}` states what the target environment has installed, which both goes into the prompt and is enforced by `CodeValidator` against imports. Pass `tool_registry=rt.toolsets` so the agent discovers exactly the toolsets the generated workflow can call.
- **Schema derivation:** Tool schemas are derived from function signatures + docstring `Args:` sections (`agents/tools.py::build_parameter_schema`). Keep docstrings accurate — they are the source of truth for the model.
- **Execution target:** Generated code must be runnable with just `pip install workflow-builder` and `MemoryStore` (no external infra). Sandbox or cloud execution is a deployment detail, not a code change.
- **Agent persistence:** Coding sessions are durable artifacts. The authoring session (spec, decision log, diagnostics) is itself a workflow run that can be resumed — use `session`/`persistent` agent classes, not ephemeral.

### Extension Points

- **Custom persistence:** Implement `state/base.py::ExecutionStore`
- **Custom tracing:** Implement `observability/tracing.py::Tracer`
- **Custom model providers:** Implement `agents/models.py::ModelProvider` (pricing table in `agents/models.py::PRICING`)

### Model Providers

| Provider | Extra | Default model |
|---|---|---|
| `AnthropicProvider` | `[anthropic]` | `claude-sonnet-5` |
| `OpenAIProvider` | `[openai]` | `gpt-5.6-luna` |
| `GeminiProvider` | `[gemini]` | `gemini-2.5-pro` |

```python
from workflow_builder.agents.providers import OpenAIProvider
agent = Agent(name="x", model=OpenAIProvider())          # gpt-5.6-luna
agent = Agent(name="x", model=OpenAIProvider("gpt-4.1")) # or name one
```

All three implement one method, `complete()`, so swapping vendors is a one-line
change at the `Agent`. `workflow_builder.agents.providers` imports them lazily —
the extras are optional and importing the package pulls in no vendor SDK.

`OpenAIProvider` takes `base_url`, so it also serves OpenAI-compatible endpoints
(Azure, Together, Groq, vLLM, Ollama). It routes o-series and gpt-5 models to
`max_completion_tokens` and drops `temperature`/`top_p`, which those models
reject, and sends `reasoning_effort="none"` when a `gpt-5.6` model is given
tools — that combination is otherwise a hard 400. The list is deliberately
narrow: `gpt-5` and `gpt-4.1` reject that value.

`GeminiProvider` absorbs three shape differences: the system prompt is config
rather than a turn, assistant turns use the `model` role, and a function
*response* is keyed by function **name** while LOOM tracks calls by id — so it
indexes ids to names as it converts. It also strips the JSON Schema keywords
Gemini rejects (`$defs`, `$ref`, `additionalProperties`), which Pydantic emits
for any nested model.
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

### Agent Backends

`ctx.agent("prompt")` delegates to an `AgentBackend` configured on the Runtime:
- `BuiltInBackend` — uses LOOM's own agent turn loop + `ModelProvider`
- `LangChainBackend` (`agents/backends/langchain.py`) — wraps a LangGraph ReAct agent
- `AgnoBackend` (`agents/backends/agno.py`) — wraps an Agno agent
- `PydanticAIBackend` (`agents/backends/pydantic_ai.py`) — wraps a Pydantic AI agent

Each backend converts LOOM `Tool` objects to framework-native tools via one adapter function. The workflow code has zero framework imports.

**Agent identity and memory.** `backend.run()` takes `history`, `agent_id`, and
`max_turns`, and declares `supports_history`. `ctx.agent(prompt, session_id=...,
agent_id=...)` loads prior turns from `Runtime.sessions` (a `Session`, defaulting
to `StoreBackedSession(store)`), passes them in, and writes back
`result.messages`. Memory is keyed by `agent_id:session_id`, so two agents sharing
a session id keep separate memories of it.

Only `BuiltInBackend` sets `supports_history = True` today. Passing `session_id`
to a backend that does not raises `ConfigurationError` rather than silently
starting each call from a blank conversation.

Outside a workflow, `agent.session(key=...)` returns an `AgentSession` that does
the same load/append around direct calls. It requires `persistence` to be
`SESSION` or `PERSISTENT`.

### Nodes

A **node** is a typed, versioned, catalogued unit of workflow work: Pydantic in,
Pydantic out. Where a `@step` is your function journaled, a node is a *shareable
contract* — searchable by the coding agent, renderable as copy-pasteable code,
installable from another package.

```python
result = await ctx.node("human.approval", ApprovalIn(subject="refund", timeout=86400))
```

**A node is packaging over the existing engine, not a second durability
mechanism.** Everything durable a node does goes through `Context`; the body's
own calls journal beneath the node's path via `ctx.nested()`, and a node that
parks raises `Suspend` exactly as `ctx.wait_for_approval` does. Deleting
`workflow_builder/nodes/` could not change how an existing workflow replays.

Seven categories, and the split between `control`/`transform` and `agent` is the
"code or judgement" rule made structural: `control.switch` is a rule you can
write today, `agent.classify` is judgement.

| Category | Nodes |
|---|---|
| `human` | approval, choice, form, review_edit, escalate |
| `guard` | schema, policy, pii, budget, content |
| `control` | switch, filter, dedupe, batch, throttle |
| `transform` | map_fields, template, extract, join, redact |
| `io` | http_request, wait_for_webhook |
| `agent` | classify, extract_structured, summarize, judge |
| `custom` | whatever you register |

**Authoring is one file.** `@register_node` derives `input_schema`,
`output_schema`, and `node_class` from the class, so the node is discoverable,
contract-renderable, and validator-known with no second declaration. Distribute
by `loom_node` entry point, the same shape as `loom_toolset`.

`Runtime.nodes` mirrors `Runtime.toolsets`: `NodeRegistry(parent=…)` chains to
the process-global catalog, so `@register_node` and entry points reach every
Runtime while `rt.nodes.register(...)` stays local.

**Imported from `workflow_builder.nodes`, never re-exported at top level.**
`workflow_builder.node` is already the `@node` *step* decorator — a `StepClass`
that behaves exactly like `@effect` and differs only in WGIR colour. Putting
`Node` beside `node` in one namespace would make two unrelated things one
autocomplete apart, so the package boundary is the disambiguator, as it is for
`workflow_builder.toolsets`.

**Replay across a node upgrade is refused, not guessed.** Each call journals
`node_id`, `node_version`, and a hash of the two schemas; a mismatch on replay
raises `ContractChanged` rather than decoding an old payload into a new model.

#### Human-in-the-loop

LOOM owns parking the run, journaling the request, and validating the answer.
**Delivering it to a person is the provider's** — implement `HumanChannel`
(`deliver`/`withdraw`) and pass `Runtime(human=…)`. `HumanRequest` carries the
JSON Schema of the accepted answer, so a channel builds its UI from the request
rather than special-casing node ids.

Three properties, each the fix for a specific failure:

- **Delivery is journaled**, so it happens exactly once per request across
  replays. Otherwise every restart re-notifies and a team learns to ignore the
  channel.
- **No channel configured raises before the run parks.** A run parked with
  nobody listening is indistinguishable from patience, so it is found a day late.
- **`AutoRespondChannel` is not a convenience.** Without it, a generated workflow
  containing an approval hangs in the smoke sandbox, and the cheapest repair a
  model can find is deleting the approval — shipping a workflow that passes every
  check having stripped out the control the spec asked for.

`ctx.wait_for_approval` is unchanged and uses the same `approval:<subject>`
event, so `runtime.approve()` resolves either.

#### Guardrail nodes

`Guardrail`/`GuardrailResult`/`GuardrailAction` are reused, not forked. What is
new is where they attach: standalone (`ctx.guard(...)`), around a node
(`NodeSpec.guards` or `ctx.node(..., guards=[...])`), and where they already ran,
around agent tool calls.

One semantic changes with the wider reach: **outside an agent loop, REJECT
raises.** In an agent loop it hands the model an explanation so it can adapt; in
a workflow body there is nobody to adapt, and a falsy return a caller ignores
would let the guarded work proceed. A guard that *raises* is treated as a
tripwire, never an allow — a check that cannot run has found nothing.

#### What the coding agent sees

The prompt carries **category headers and counts only — O(categories), never
O(nodes)**. Registering 500 custom nodes adds nothing; a test asserts that as
line-count equality, not a tolerance. Detail arrives on demand through three
tools:

| Tool | Returns |
|---|---|
| `search_nodes(query, category=…)` | cards; an empty query with a category lists it |
| `show_node(id)` | schemas, examples, effect, requires |
| `node_contract(id)` | **the code to write** |

That last one is the substantive difference from `ToolsetCatalog.stub()`, which
returns JSON Schema — a *description of* a call. The agent's next action is to
*write* one, so every schema→Python translation is a chance to invent a keyword
argument. `node_contract` returns the import line, the call with annotated
fields, the result type, whether it parks the run, and what the Runtime needs —
all rendered from the node's own models, so it cannot drift.

### Adding a toolset

`docs/guides/toolsets.md` is the end-to-end walkthrough — three files (client,
tools, manifest), credentials, error classification, effect classes,
`resolves`, registration, pagination, fakes, and the contract test. Every
snippet on that page executes in CI. The sections below are the reference
layer; start there if you are writing one.

### Three-Layer Lazy Tool System

Tools are managed via `ToolsetRegistry` on the Runtime:
- **Layer 1 (registration)**: Only `ToolsetManifest` metadata stored. No tool code imported.
- **Layer 2 (discovery)**: Coding agent browses manifests via search/show/stub. Auto-generates docs from schemas.
- **Layer 3 (materialization)**: `Tool` objects created on demand via lazy resolver when `ctx.agent()` is called.

Key classes: `Toolset`, `ToolsetRegistry` in `agents/tool_registry.py`.

**One registry, two jobs.** `ToolsetRegistry` extends `ToolsetCatalog`, so the
same object answers "what integrations exist?" (`search`/`show`/`stub`, used by
the coding agent) and "give me the callable tools" (`resolve_tools`, used by
`ctx.agent()`). Keeping those in separate stores is how a coding agent ends up
generating correct code against a toolset the runtime cannot call.

`Runtime.toolsets` chains to the process-global registry via `parent=`, so
`register_toolset()` and `loom_toolset` entry points reach every Runtime, while
`rt.toolsets.register(...)` stays local to that Runtime.

**Toolset identity.** A manifest carries `kind` (`app`/`mcp`/`knowledge`/`memory`/
`skill`) and `provider`, combining into `qualified_id` = `<kind>:<provider>:<id>`.
Registering two executable toolsets under one `id` with different qualified ids
raises `ConfigurationError` — this is what keeps a first-party Jira toolset and an
MCP-sourced one distinguishable.

**Effect classes.** `from_steps`/`from_callables` guess `EffectClass` from the
operation name; pass `effects={"scrape": EffectClass.WRITE}` when the name lies.
`resolve_tools(effects={EffectClass.READ})` hands an agent a read-only toolset.

**Manifests must say how to import themselves.** Set `tools_module` on the
manifest and `function=` on each `OperationSpec`; `import_line()` composes them
and `registry.describe()` puts the result in front of the coding agent. Without
it the docs list operation ids like `messages.search`, which exist in no
namespace — and a model asked to write code against that invents an import to
match. An operation id names a capability; only a function name is something
anyone can write. `tests/test_manifest_imports.py` executes every declared
import, so the docs cannot promise a symbol that is not there.

### Gmail and Calendar Toolsets

`toolsets/google/` — two separately-grantable toolsets (`gmail`,
`google_calendar`) over one shared OAuth layer, pure httpx, no vendor SDK.

Credentials resolve from the environment in order: `GOOGLE_ACCESS_TOKEN`, then
`GOOGLE_CLIENT_ID`+`GOOGLE_CLIENT_SECRET`+`GOOGLE_REFRESH_TOKEN`, then
`GOOGLE_SERVICE_ACCOUNT_FILE` (the only one needing the `[google]` extra). One
cached token serves both toolsets, refreshed under a lock.

**Errors are classified, not blanket-retried.** Google 4xx (bar 429) raises a
`NonRetryableError` subclass, so a plain `Retry` policy stops on a malformed
query rather than sleeping through three attempts. A 403 splits on `reason`:
quota is retryable, missing scope is not.

**`gmail_send_message`/`gmail_reply_to_message` have retries off.** No
idempotency key exists, so a timeout after delivery is indistinguishable from a
failure and a retry double-sends. Journaling covers replay; this covers the
attempt. Calendar writes retry once — a duplicate event is deletable.

**Defaults that avoid surprises:** `send_updates="none"` so bulk event work does
not email attendees; `singleEvents=True` so recurring series come back as
instances. Gmail messages arrive flattened out of their MIME tree; attachments
come back as LOOM `Attachment`s and offload to blobs.

### Production Layer

All opt-in — constructing a bare `Runtime()` enforces none of it.

| Capability | Wiring |
|---|---|
| **Flow control** | `@workflow(flow_control=FlowControlPolicy(...))` + `Runtime(admission=AdmissionController())`. Evaluated before the record is created, so a rejected trigger leaves no run behind. Raises `AdmissionRejected`; `.retryable` separates "later" from "never". Slots release on terminal transitions. |
| **Effect broker** | `Runtime(broker=GuardedBroker(max_calls=…), authority=Authority(grant=…, dry_run=…))`. Mediates every durable operation. `DirectBroker` is the default and checks nothing (~2µs/dispatch). Grants are checked **per dispatch**, not when tools are resolved — a tool an agent is holding cannot outlive its grant. |
| **RBAC** | `Runtime(role=Role.OPERATOR)`. Checks `flow:run`, `flow:cancel`, `run:view`, `run:replay`. `role=None` enforces nothing. |
| **Leader election** | `await rt.start_scheduler(elector=LeaderElector(lock_provider, node_id))`. Only the lease holder ticks, so many processes can share one store. |
| **Retention** | `await RetentionManager(policy).compact(store)`. Drops journals past the warm cutoff, deletes records past `run_record_days`, never touches suspended runs. |
| **Grants** | `@workflow(grants=GrantSet(toolsets=["jira.issues:read"]))` narrows what `ctx.agent()` resolves. Denied toolset by name → `GrantDenied`. |

### Events, Output, and Workflow State

`ctx.publish(name, payload)` broadcasts an **event**; `ctx.report(message)`
streams a run's **output**. `ctx.emit` was both — the ambiguity that produces
code reading correctly and doing the other thing — and is now a deprecated
alias for `publish` that warns once. Journals keep the `emit:` entry prefix so
runs already in flight stay replayable.

`ctx.state` is a KV space shared by every run of one workflow, over a
`StateStore` port (`runtime/state.py`), backed by the execution store. Mutable
and current, where an artifact is immutable and versioned.

Neither state nor reports are journaled, and both consequences are load-bearing:
a workflow branching on state does **not** replay identically (put the read in a
step when that matters), and a replay reports again under its own run id.

`Runtime(state=…, stream=…)` swaps either. `RunStream`'s reference adapter is a
bounded in-memory ring surfaced through the facade, so `loom watch` and the MCP
`get_run_progress` tool show progress with no host involvement.

### Time

`Runtime(clock=…)` — a `Clock` port with `SystemClock` (default) and
`ManualClock`. Every timestamp and every in-memory wait in the engine, the
context, and the trigger dispatcher reads it, so a four-minute timer or a 9am
cron is testable in milliseconds:

```python
rt = Runtime(store=MemoryStore(), clock=ManualClock(NINE_AM))
parked = await rt.run(reminder)             # parks on a timer
await advance(rt, minutes=5)                # move, tick, and settle
```

`workflow_builder.testing.advance` / `advance_to` do all three steps —
advancing alone leaves a parked run parked, because nothing is driving the
scheduler in a test. The lease heartbeat deliberately stays on the real clock:
a lease is a claim against other processes, and a ManualClock would spin it.

### Pagination

`toolsets/pagination.py`. **The return type is the declaration**: a read that
returns `Results[T]` is paged, one that returns `list[T]` is not. `paginates()`
derives `OperationSpec.pagination` from it, so a toolset author writes it once
and nothing is maintained in parallel — the only version of this that survives
a thousand toolsets.

`page_through(request, style=…, limit=…, page_size=…)` owns the loop; a
`PagingStyle` — `TokenPaging`, `CursorPaging`, `OffsetPaging` — owns the
dialect. A style knows a wire format and nothing about the service; the client
knows the service and nothing about looping. A fourth dialect is a new class,
not an edit to `collect`.
`Results` is a `list` subclass carrying `.complete`, `.total`, `.cursor` and
`.summary()` — and it round-trips through the journal via `SelfEncoding`
(`core/serde.py`), so `.complete` survives being returned from a step. Map rows
with `.mapped()`, never a comprehension: a comprehension yields a plain list and
discards the coverage.

`tests/test_manifest_imports.py` checks three ways — client pages ⟹ tool returns
`Results` ⟹ manifest declares it. The client is ground truth; that check found
six drifts on its first run.

### Failure Taxonomy

`EntryStatus.EXHAUSTED` is a step that spent its own `Retry` budget. It is not
`FAILED`, because a step has said nothing about whether the *run* is finished —
a gateway returning 503 needs the same code run again in five minutes with the
other nine steps still cached, not the journal edited.

The same record answers two questions, read per operation rather than mutated:
`Journal(resume_exhausted=…)` is `True` for retry/resume (re-execute the entry,
serve everything before it from the journal) and `False` for `replay`, which is
a rehearsal of what happened and must reproduce the failure the run saw. The
engine sets it from `record.trigger`.

`retry()` therefore truncates only genuinely `FAILED` entries. An exhausted one
is re-executed by replay on its own, so pruning it would throw away the attempt
history that tells an operator this has failed six times against the same
gateway. Nothing is promoted on the way to terminal — an earlier draft did, and
it put a journal write after the compensation stack had already unwound while
making the distinction unobservable, since every failed run erased it.

### Version Gates

`ctx.patched("use-new-pricing")` lets a branch ship without changing what runs
already in flight are doing. A run reaching the gate for the first time records
that the patch was present and takes the new branch, forever; a run that was
*already past that point* takes the old one, because its journal proves it was
there first (`Journal.has_entries_after`).

The marker is keyed by **name, not position** — deliberately unlike every other
entry — so inserting a durable call before the gate does not change which
decision it finds. That is why a patch id must be unique within a workflow and
must never be reused for a different change.

### Testing With The Journal

`workflow_builder.testing`. The replay engine already prefers a recorded entry
to running anything, so a test can state what happened instead of substituting
a stand-in for the step that would have:

```python
result = await run_with(
    onboard,
    payload,
    given(research, returns={"summary": "canned"}),
    given(send_email, raises=TimeoutError("smtp down")),
)
```

A seeded entry means exactly what a recorded one means, so there is nothing to
keep in sync. Seeds resolve by name and kind, so a mismatch surfaces as the
engine's own divergence error rather than as a fact nobody used.

`assert_replays(workflow, input)` runs a workflow, replays it, and asserts the
output did not change — the `replay` stage from the coding agent's pipeline,
available to anyone writing a workflow by hand.

### Seam Catalog

`python scripts/gen_seam_catalog.py` writes one page per port under
`docs/seams/`, generated from the Protocol's own methods, docstrings,
implementations, and importers. `--check` fails when a page no longer matches
the code, and runs in CI.

Nine seams and one gate, not a doc-sync suite: the value is the alarm, not the
document. Implementations are found structurally as well as by declared base,
because a Protocol is usually satisfied without naming it — listing only
subclasses reported "none found" for exactly the seams whose implementations
are cleanest.

### Grant Validation

`security/grants.py`. An entry that names nothing permits nothing —
`allows_operation` simply never matches it — so `grants=["jira.issues:writ"]`
yields a workflow that reads as restricted to what it lists and is in fact
restricted to nothing. From the outside the two are indistinguishable until an
agent reports, hours later, that it could not find a tool.

`GrantSet.validate_against(toolsets=…, agents=…)` returns `GrantIssue`s rather
than raising, because the right response differs by caller:

| Caller | Behavior |
|---|---|
| `Runtime.register()` | raises `ConfigurationError` — the only moment that knows the effective registry, since `rt.toolsets` chains to the process-global one |
| `loom check` | reports as a problem, beside the narration check |
| `WorkflowCodingAgent` | a `grants` stage (cost 12, non-blocking) feeding repair |

**Grants are only enforced on journaled calls.** `broker.dispatch` runs inside
`DurableCall._resolve`, so `await ctx.step(jira_search_issues, ...)` is weighed
against the `GrantSet` and `await jira_search_issues(...)` — legal, because a
toolset tool is a `@step` and `StepDefinition.__call__` bypasses the journal —
is not. A direct call also skips the tool's declared `Retry` and gives the
enclosing step's granularity to replay. `DEFAULT_SYSTEM_PROMPT` tells generated
code to use `ctx.step`; `TestOnlyJournaledCallsAreGuarded` pins the reason.

Four failure modes, each with near-matches attached: unknown toolset, unknown
group, unknown effect, unknown agent. Two things it deliberately does not do:
an **empty registry checks nothing** (toolsets load lazily and via entry
points, so empty now says nothing about the entries), and validation reads
**manifest metadata only** — Layer 1 stays Layer 1, and no toolset is imported
to check a string.

### Bounded Tool Results

`agents/bounds.py`. A tool that returns four megabytes puts four megabytes into
the next model request, and into every request after it — until the provider
truncates and the model answers confidently about data it never saw. That is
the failure worth naming: not an error, a wrong answer that looks right.

`Agent(bounds=ResultBounds(max_bytes=32_768))` caps what one result contributes
to the *conversation*. The journal still records the value whole — a replay has
to reconstruct the run that happened — and `Runtime(blobs=...)` makes the
original retrievable through `read_spill` / `grep_spill`, which are mounted
before the overflow rather than after it, because the model reads the tool list
first.

Three properties, each one a bug avoided:

- **The cap is on the replacement.** The notice's own byte cost is reserved out
  of the budget, so bounding can never make a result larger. Truncate-then-append
  overshoots by exactly the length of the notice.
- **A paged read keeps its coverage.** `Results` serializes rows first and
  `complete`/`total` last, so a head-and-tail cut is precisely where the
  coverage disappears — reintroducing "one page reported as a total", the thing
  `Results` exists to prevent. `coverage_of()` hoists it into the notice first.
- **Best-effort throughout.** No blob service, or a failed save, degrades to
  truncation with an honest notice. A spill failure never turns a successful
  tool call into an error.

`Runtime(spill=...)` swaps the store; it defaults to `BlobSpillStore` when
blobs are configured, `None` otherwise. `bounds=None` (the default) is exactly
what shipped before. See `examples/cookbook/23_bounded_tool_results.py`.

### Replay Verification

`Journal.lookup()` finds an entry by position and confirms its kind and name.
None of that proves the entry belongs to the call that found it: two calls to
one step at adjacent positions match each other's entries, so swapping the two
lines replays each against the other's recorded output — silently, because
everything compared is still equal.

`VerifyMode` closes that with the fingerprint (name plus arguments) already
recorded on every entry. `WARN` is the default: it serves the value, logs, and
flags the entry `argument_drift` so `loom show` can surface it. `STRICT` raises
`NondeterminismError` naming both fingerprints. `OFF` is the old behaviour.

The default is not `STRICT` because a step whose input derives from `ctx.state`
— deliberately not journaled — legitimately replays with different arguments.
Raising on that would break correct workflows to catch an uncommon one.

Separate from `CompatibilityMode`, which answers a different question: a
*shape* divergence (a different operation at that position) truncates and moves
on; an *argument* divergence warns or raises but never discards a journal the
run may still need.

### Input Validation

`runtime/validation.py`. `Runtime._open_execution` checks a payload against the
workflow's `input_schema()` **before the record is created**, the same position
admission occupies and for the same reason: a run that could never have started
should leave nothing behind. Raises `InputMismatch`, which the CLI renders as
exit code 2 (usage) rather than 1 (failed).

Deliberately shallow — the declared top-level type and an object's required
properties. Anything deeper is the input model's own job and its error is
better. `None` is never rejected: `run()` defaults its input to `None`, so it
is indistinguishable from "not supplied". `Runtime(validate_input=False)` is
the escape hatch for a codebase whose annotations were never meant as contracts.

### Files and Artifacts

| Concept | Use |
|---|---|
| **`Attachment`** | A file's bytes *plus* filename, MIME, and size. `Attachment.from_bytes/from_path/from_text`; `await att.offload(blobs)` moves content to blob storage and keeps the metadata inline. Journals losslessly. |
| **Blobs** | `Runtime(blobs=BlobService(...))` or `$LOOM_BLOBS`. Content-addressed and immutable; oversized journal payloads offload automatically. Backends: `file://`, `s3://`, `az://`, `gs://` via `blob_backend_from_url()`. |
| **Artifacts** | `ctx.put_artifact(name, data)` → `name@1`, `ctx.get_artifact(name, version=None)`, `ctx.artifact_versions(name)`. The mutable-name layer over immutable blobs. |
| **Staging** | `ctx.stage_artifact(name, bytes_or_attachment)` then `ctx.commit_staged(name)`. Per-run; an offloaded Attachment reuses its `ref`. |
| **Signed URLs** | `ctx.artifact_url(name)` mints a short-lived download URL (not journaled — URLs expire). Presigned uploads: `upload_url` then `confirm_upload`. Local HMAC URLs need `LocalBlobBackend(base_url=...)`. |

`$LOOM_BLOBS` is read by `Runtime.from_env()`, the same way `$LOOM_STORE` picks the journal. `file:///var/loom/blobs`, `s3://bucket/prefix`, `az://container/prefix`, `gs://bucket/prefix`. Extras: `[s3]`, `[azure]`, `[gcs]`.

Two properties worth knowing: republishing identical bytes resolves to the
existing version rather than creating a duplicate, so retries and replays do not
inflate the version chain. And `get_artifact` journals the version it resolved,
so a replay reads what the original run read — a replay rehearses what happened,
not what would happen now.

`RetentionManager.compact(store, blobs=...)` deletes orphaned blobs and drops
per-run staging entries; without the `blobs=` argument it reclaims rows and leaks
content.

### Long-Running Runs

Runs take a lease (`Runtime(node_id=..., lease_ttl=...)`), heartbeated at a third
of the TTL. `reclaim_orphans()` resumes runs whose worker died — nothing else
covers them, since a crashed run is `RUNNING`, not waiting on a timer. Wired into
`start_scheduler`, so leader election and orphan recovery run together.

`ctx.continue_as_new(seed)` is what keeps a forever-flow's journal bounded, and
`Runtime(journal_warn_entries=..., journal_max_entries=...)` makes forgetting it
loud instead of slow — a warning once, then `BudgetExceeded`.

### Workflow Catalog

Definitions live in code; the file on disk is the source of truth. The catalog
stores the *entry* — name, version, `code_hash`, source path, triggers, input
schema — so a run can be traced to the code that produced it and a server can
list workflows it did not import.

```python
await rt.publish(my_flow)          # explicit; @workflow never writes to storage
await rt.published()               # every catalogued workflow
await rt.provenance(run_id)        # the entry whose code_hash matches this run
```

`GET /workflows` merges imported and published, with `executable: false` on
anything the serving process cannot actually run.

### Entity Resolution in Generated Code

A spec says "Vishwjeet" and "in progress"; APIs match account ids and their own
configured vocabulary. Nothing joins the two, so a query built from the spec's
words returns **zero rows and no error** — which reads as "nothing to do".

`OperationSpec.resolves` marks an operation as doing the joining
(`resolves="user"`), `ToolsetManifest.resolvers()` reports them, and the
generated docs tell the agent to resolve before filtering. Generic: any toolset
declares its own.

`call_read_operation` lets the coding agent execute a **read-only** operation
while authoring, so a name in the spec is resolved once and baked in rather than
looked up on every run. Writes and destructive operations are refused —
authoring must not change the system it writes code about.

The ladder in `DEFAULT_SYSTEM_PROMPT`: named in the spec → resolve now, bake the
id with the human name in a comment; comes from input → resolve at runtime;
ambiguous → report it for a read, `ctx.wait_for_approval()` for a write; nothing
found → error naming what was tried. `ctx.agent()` is for **judgement**, not
lookup — an agent node answering "who is X" puts a nondeterministic call into
every run to re-answer a question settled once at authoring time.

A **fuzzy text search is not a resolution**. `text ~ "..."`, `contains`, `LIKE`
is the raw-string fallback in disguise, and silently correcting a spelling is a
guess about what someone meant. `ResolutionStage` catches both by flagging a
match operator whose operand came from the spec; an exact comparison is left
alone, because `status = "In Progress"` is a plausible resolved value.

### Code or judgement

Before writing anything the agent classifies each node: *can I write a rule
today that is right for every input the spec allows?* Yes → `@step`. **No, or
unsure → `ctx.agent()`** — when in doubt, the agent. The tell for a rule that
should not be written is an invented constant: a keyword list, a regex over
prose, a threshold nobody supplied. `if "urgent" in subject.lower()` is a guess
wearing the clothes of logic.

The classification comes back on `CodingResult.plan` (`node`, `kind`, `why`)
rather than staying in the prompt, because a rule the model is asked to follow
silently is a rule nobody can check it followed. An empty plan means
*unreported*, not "all deterministic".

**Only registered toolsets exist.** The prompt carries index cards (names +
import line), not every operation, so it grows with the number of integrations
rather than with each one's size; detail comes from `show_toolset` on demand.
`CodeValidator(available_toolsets=…)` rejects an import of anything not
registered, and a refusal returns the agent's explanation as a single
`unsupported` issue rather than "no @workflow found".

### Verification Pipeline

`agents/checks.py` defines `Check` and `CheckPipeline`; `agents/stages.py` holds
the stages. They run cheapest-first and stop at the first blocking error:

| Stage | Cost | Blocking | Notes |
|---|---|---|---|
| `compile` | 0 | yes | `compile()` — everything after assumes it |
| `static` | 10 | yes | the AST rules, toolset availability, store choice |
| `coverage` | 15 | no | the spec asked for *all* and the code caps a fetch |
| `resolution` | 16 | no | a fuzzy match on a word the spec supplied |
| `lint` | 20 | no | ruff `F,E9` only; skips when absent |
| `types` | 30 | no | mypy; warnings, not errors |
| `smoke` | 50 | yes | runs it against fakes, faked clock |
| `replay` | 60 | no | runs twice, compares — determinism observed |
| `critique` | 100 | no | a second model, when configured |

Adding a stage is registration, not surgery — `WorkflowCodingAgent(stages=[...])`
replaces the arrangement. A stage whose tool is missing reports itself
**skipped**: a check that cannot run has found nothing, which is not passing.

Repair consumes the pipeline's issues, so a type error and a traceback reach the
model by one path. Errors about the environment rather than the code never drive
a repair — that is how a workflow came back gutted.

**Fakes** (`agents/fakes.py`) are built from each operation's `output_schema`,
not hand-written, so they cannot drift from the contract. Without them the
sandbox has no credentials and an integration workflow can only reach a 401,
proving nothing. `ToolsetManifest.fakes_module` overrides them where the shape
of an answer is not enough.

`generate()` never raises for an agent-loop failure; the reason comes back as an
`unsupported` issue. Turn budgets are separate: `max_discovery_turns` for
search/inspect/resolve/write, `max_repair_attempts` for repair.

### Generated Code Verification

`WorkflowCodingAgent` validates statically (`CodeValidator`: AST structure,
determinism, import allowlist) and then **runs the code**: `agents/smoke.py`
compiles it, executes it in a subprocess against `MemoryStore` and
`MockModelProvider`, and feeds any traceback back into the repair loop. Disable
with `smoke_test=False`.

`CodeValidator` also resolves imported symbols, so `from workflow_builder import
Retryy` is caught with a suggestion rather than failing on the user's machine.

Optionally add a **supervisor**: `WorkflowCodingAgent(supervisor=CodeSupervisor(model))`
runs a second model over the finished code — durability, determinism, retry
safety, error handling, spec fidelity. Use a different model from the author
where you can; one model reviewing itself mostly agrees with itself.

`CodingResult.is_clean` means the code validates, runs, *and* passes review. Code
that merely parses is a weak claim; so is code that runs but charges twice.

### Queue Ingress

`triggers/queue.py`. Implement `QueueBackend` (`poll`/`ack`/`nack`) over Redis
Streams, SQS, Kafka, or anything else; `InMemoryQueue` is the reference.

`QueueConsumer` acks a message **only after the run is durably recorded**, and
derives the run's idempotency key from the message — at-least-once delivery from
the broker, exactly-once execution in LOOM. A failed submit requeues and then
dead-letters. `batch_size` and `idempotency_field` come from the workflow's own
`OnEvent` trigger unless passed explicitly.

Note the asymmetry: a submit that fails is redelivered; a *workflow* that fails
is a recorded failure and is not.

### HTTP Surface

`workflow_builder.server.create_app(runtime)` (needs the `api` extra) serves
workflows, runs, journals, events, cancel, and replay. `LoomClient` is the async
client. Errors map to status codes: 403 authorization, 404 unknown workflow/run,
429 retryable admission rejection, 409 permanent one.

This is the realistic path to other languages — workflow authoring stays Python
because durability depends on re-entering a Python function body, but starting
runs and delivering approvals are ordinary HTTP requests.

### Trigger Dispatcher (new)

`TriggerDispatcher` (`runtime/dispatcher.py`) fires cron/interval workflows at the scheduled time:
- Scans registered workflows for `Schedule`/`Interval` triggers
- Computes `next_fire_at` via `CronSchedule.next_after()`
- Fires runs via `Runtime.submit()` on each `tick()`
- Uses `TriggerStore` protocol for persistence (in-memory by default)

### Storage Backends

| Store | URL | Driver | Install |
|-------|-----|--------|---------|
| `MemoryStore` | `memory://` | in-process | default |
| `SQLiteStore` | `sqlite:///runs.db` | sqlite3 | default |
| `MongoStore` | `mongodb://…` | motor | `pip install workflow-builder[mongo]` |
| `PostgresStore` | `postgres://…` | asyncpg | `pip install workflow-builder[postgres]` |

All implement: `ExecutionStore + TriggerStore + CacheStore + LockProvider`.

**Workflows do not choose a store.** Where the journal lives is a deployment
decision — tests want memory, a laptop wants SQLite, production wants Postgres,
and the *same workflow code* must run against all three. So a workflow module
declares steps and workflows and nothing else; the host supplies the store:

```python
Runtime(store=PostgresStore(dsn))   # explicit
Runtime.from_env()                  # from $LOOM_STORE, defaults to memory://
from_url("sqlite:///runs.db")       # workflow_builder.state.from_url
```

`CodeValidator` warns when a generated module constructs a store at import time.
Doing it inside `if __name__ == "__main__"` is fine — that block is a script, not
the library.

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
