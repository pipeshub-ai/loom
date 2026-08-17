# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Loom** — `pip install loomflow`, `import loom` — is a **library-first** durable execution SDK for AI-powered workflows. The primary deliverable is a **Workflow Coding Agent**: an LLM-powered agent that _authors_ workflow code — mixing third-party SDK calls, workflow constructs (`@workflow`, `@step`, `ctx.*`), and raw Python — so that users describe what they want and receive a ready-to-run workflow.

Generated workflows are execution-portable: they run embedded (SQLite store, no infra), in a user-supplied sandbox, or against an external durable backend (Temporal, DBOS, Restate). The SDK never forces a specific runtime.

The SDK's core engine design is **deterministic re-entry**: workflow bodies can be safely re-executed after crashes/deploys because every side effect is journaled and served from the journal on replay. This is analogous to Temporal's event-sourced execution model.

## CLI

Installed as both `loom` and `loomflow`.

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

# credentials
loom connect gmail                     # OAuth a credential a workflow can read
loom whoami                            # what is stored, and whether it is ok/due/expired
loom refresh [--all] [--force]         # renew what is near expiry; exit 1 if any failed

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
**`3` suspended**, `4` cancelled, and `128 + signum` interrupted (`130` Ctrl+C,
`143` SIGTERM). The third one matters — a run parked on a human has neither
succeeded nor failed, and collapsing it into either makes calling scripts do the
wrong thing. A suspended run prints the command that unparks it.

**A signal is not an error.** `loom.runtime.shutdown` routes SIGINT and SIGTERM
to one place: cancel the work, let it unwind, report it as what it is. That
matters more than the tidy output, because LOOM's recovery story *is* the
cleanup — the `finally` that settles the lease `reclaim_orphans` later matches
on. SIGTERM used to run none of it, so `docker stop` stranded whatever was
mid-step. An interrupted command says which runs survived and how to find them;
a second signal restores the default disposition and re-raises, because a
cleanup path that cannot itself be interrupted is a hang with extra steps.

`guarded()` covers a command's event loop and `terminate_on()` the windows
outside one — argument parsing, and the commands that never open a loop. The
servers keep their own handling: uvicorn and FastMCP install their own handlers,
and `serve`/`mcp` exit `0` on either signal, because a server asked to stop and
which stopped has succeeded. Nothing is installed by importing the module — a
library that seizes the process's signal handlers is one you cannot embed.

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

**One port, two clients.** `loom/facade.py` defines `RuntimeFacade`,
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

# Type checking — in a `[dev]` environment, deliberately not `[all]`
mypy
```

**mypy runs against `[dev]`, not `[all]`, and that is a decision rather than an
accident.** `python_version = "3.11"` is the floor `requires-python` and the
classifiers promise, and checking against the oldest supported version is what
catches a 3.12-only stdlib call. The optional integration SDKs pull numpy
transitively, and numpy's stubs use PEP 695 `type` statements — a *syntax* error
below 3.12 — so mypy could not parse them and stopped with "errors prevented
further checking", never type-checking LOOM at all. No per-module setting avoids
that: `follow_imports` and `ignore_errors` are both ignored for stub files, and
the failure happens while parsing regardless.

None of those SDKs is needed to check LOOM — every one is imported lazily inside
a function and declared `ignore_missing_imports` in `pyproject.toml` — so `[dev]`
is the environment this check is *for*. The full suite still needs `[all]`; that
is a separate environment. If you type-check in a `[all]` venv you will get one
numpy syntax error and nothing else, which means the gate is not running.

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
| **Stores** | `stores/` | Pluggable persistence: `ExecutionStore` protocol, `MemoryStore`, `SQLiteStore`, `MongoStore`, `PostgresStore` |
| **Agents** | `agents/` | `ModelProvider` protocol, `Tool` abstraction (steps/workflows-as-tools), guardrails, memory |
| **Triggers** | `triggers/` | Entry points: `Webhook`, `Schedule`, `Manual`, `Poll`, `Event`, `Chat`, `Email`, `SubWorkflow` |
| **Observability** | `observability/tracing.py` | `Tracer` protocol + `NoopTracer`; plug in OTel/Datadog/Honeycomb |

### Where things live

Two renames worth knowing, both because the old names read as the same thing:

| Was | Is | Holds |
|---|---|---|
| `state/` | `stores/` | the journal — `ExecutionStore` and its backends |
| `storage/` | `blobs/` | bytes — blobs, artifacts, attachments, staging, retention |

`state/` also collided with `runtime/state.py`, which is `ctx.state` — a
different thing entirely. `nodes/stdlib/` is gone: a node category is a module
(`nodes/control.py`) or a package (`nodes/human/`) depending on its size, and
burying four of them one level deeper only hid them.

### Suspension Model

Workflows park themselves by raising `Suspend(wake_at=datetime)` or `Suspend(awaiting_event="name")`. The engine persists the suspension, and `runtime.tick()` / `runtime.resume(run_id)` re-enters the workflow at the next opportunity. This is how `ctx.sleep()` and `ctx.wait_for_event()` work internally.

### Public API Surface

`src/loom/__init__.py` re-exports ~10 symbols that form the user-facing API:
`Context`, `ExecutionResult`, `ExecutionStatus`, `Failure`, `OnError`, `Retry`, `Usage`, `Runtime`, `StepContext`, `step`, `workflow`.

All internal modules (~200+ classes) are implementation details.

### Workflow Coding Agent

The agent is the primary user-facing feature. It takes a natural-language description and produces a valid, runnable workflow file. Key design constraints for the agent and the code it generates:

- **Code style:** Generated workflows use `@workflow` + `@step` + `ctx.*` for all durable operations. Raw Python and third-party SDK calls belong inside `@step` bodies, never directly in the workflow body.
- **Tools available to the coding agent:** Any `@step` or `WorkflowDefinition` can be surfaced as a `Tool` via `tool_from_step()` / `tool_from_workflow()` / `coerce_tool()` in `agents/tools.py`. The agent's toolset is therefore the SDK itself — it can call steps and sub-workflows as tools to introspect capabilities.
- **Configuring it:** `WorkflowCodingAgent(instructions=...)` replaces `DEFAULT_SYSTEM_PROMPT` outright; `extra_instructions=` appends house rules; `allowed_packages={...}` states what the target environment has installed, which both goes into the prompt and is enforced by `CodeValidator` against imports. Pass `tool_registry=rt.toolsets` so the agent discovers exactly the toolsets the generated workflow can call.
- **Schema derivation:** Tool schemas are derived from function signatures + docstring `Args:` sections (`agents/tools.py::build_parameter_schema`). Keep docstrings accurate — they are the source of truth for the model.
- **Execution target:** Generated code must be runnable with just `pip install loomflow` and `MemoryStore` (no external infra). Sandbox or cloud execution is a deployment detail, not a code change.
- **Agent persistence:** Coding sessions are durable artifacts. The authoring session (spec, decision log, diagnostics) is itself a workflow run that can be resumed — use `session`/`persistent` agent classes, not ephemeral.

### Extension Points

- **Custom persistence:** Implement `stores/base.py::ExecutionStore`
- **Custom tracing:** Implement `observability/tracing.py::Tracer`
- **Custom model providers:** Implement `agents/models.py::ModelProvider` (pricing table in `agents/models.py::PRICING`)

### Model Providers

| Provider | Extra | Default model |
|---|---|---|
| `AnthropicProvider` | `[anthropic]` | `claude-sonnet-5` |
| `OpenAIProvider` | `[openai]` | `gpt-5.6-luna` |
| `GeminiProvider` | `[gemini]` | `gemini-2.5-pro` |

```python
from loom.agents.providers import OpenAIProvider
agent = Agent(name="x", model=OpenAIProvider())          # gpt-5.6-luna
agent = Agent(name="x", model=OpenAIProvider("gpt-4.1")) # or name one
```

All three implement one method, `complete()`, so swapping vendors is a one-line
change at the `Agent`. `loom.agents.providers` imports them lazily —
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
`loom/nodes/` could not change how an existing workflow replays.

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

**Imported from `loom.nodes`, never re-exported at top level.**
`loom.node` is already the `@node` *step* decorator — a `StepClass`
that behaves exactly like `@effect` and differs only in WGIR colour. Putting
`Node` beside `node` in one namespace would make two unrelated things one
autocomplete apart, so the package boundary is the disambiguator, as it is for
`loom.toolsets`.

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

### GitHub and GitLab Toolsets

`toolsets/github/` (15 operations) and `toolsets/gitlab/` (14). Research and
schema in `docs/design/github-gitlab-toolsets.md`.

**Both signal pagination in response headers**, which no earlier dialect could
read — by the time `page_through` hands a style the response, headers are gone.
So these clients return `{"items": rows, "headers": {…}}` and `HeaderPaging`
reads both halves: GitHub's `Link: …; rel="next"` (absence of `rel="next"` is
the documented end) and GitLab's `x-next-page` (empty means the same as
absent). Plain data deliberately — an httpx object in a paging style would make
the style untestable without a transport.

**GitHub's issue listings contain pull requests.** Its own model: *every pull
request is an issue, but not every issue is a pull request*, told apart by a
`pull_request` key. `github_list_issues` filters them out by default, because
"how many open issues" answered over an unfiltered listing is wrong with
nothing to notice. `GitHubIssue.is_pull_request` keeps it visible, and the
filtered `Results` drops its `total` rather than reporting one that counted PRs.

**Three GitHub signals mean "partial", none of them an error**: search caps at
1,000 results however large `total_count` is, search is limited to 30
requests/minute, and `incomplete_results` means the query timed out server-side.
All three surface through `.complete`.

**GitLab's `iid` is not its `id`.** The number in a URL is the per-project
`iid`; the global `id` is a different number most endpoints reject. Both are
carried under GitLab's own names. Two more traps encoded: `state="opened"`, not
`"open"` (an unknown state is ignored and returns everything), and closing takes
`state_event="close"`, not a state. A `group/project` path is URL-encoded for
you — an unencoded slash is a different route and 404s.

**Both return 404 for a resource the token cannot see**, so `GitHubNotFound` and
`GitLabNotFound` say so in the message; otherwise a permissions problem is
debugged as a typo. GitHub's 403 splits on `x-ratelimit-remaining`: zero is
"wait", anything else is "never".

### Salesforce and HubSpot Toolsets

`toolsets/salesforce/` (11 operations) and `toolsets/hubspot/` (15) — the two
CRMs, built from vendor docs read during the work rather than recalled. The
research, schema, and phasing are in
`docs/design/salesforce-hubspot-toolsets.md`.

**Salesforce has no constant base URL.** Every org answers on its own host,
returned by the OAuth exchange as `instance_url`; `login.salesforce.com`
authenticates and does not serve data. The client therefore refuses to
construct without either an explicit instance URL or the refresh credentials
that produce one — a wrong base URL otherwise surfaces as 404s that look like
missing records. Sandboxes need `SALESFORCE_LOGIN_URL=https://test.salesforce.com`;
a sandbox token against the production host fails as `invalid_grant`, which
reads like a bad token rather than a wrong host.

**It also refreshes.** Access tokens expire mid-workflow, so the client owns
the exchange, under a lock, and retries a 401 exactly once — the same
arrangement `toolsets/google/auth.py` uses. Twice would turn a revoked grant
into a loop against the login host.

**A 403 splits.** `REQUEST_LIMIT_EXCEEDED` is org quota and clears if you wait;
any other 403 is a permission that never will. Same rule the Google toolset
applies to quota versus scope, and the reason `errorCode` is carried on the
exception rather than just the status.

**SOQL literals are escaped.** `O'Brien` is the most predictable surname in any
CRM, and unescaped it terminates the string literal.

**HubSpot's two caps both truncate silently.** Search returns at most 10,000
results — paging past it is a 400 — so the client stops there and reports
`complete=False` rather than turning a large query into an error at the end.
And properties are opt-in: a response carries only what `properties=` asked
for, so a default field list is declared per object type. Omitting it returns
a contact with no company, which reads as missing data rather than as an
under-specified request.

Both APIs are uniform across object types, so both expose **generic CRUD plus
typed finders** rather than five near-identical copies — a custom `Deal__c` or
a custom HubSpot object is reachable without a library change. HubSpot's path
version is a constructor argument, since it has begun publishing dated versions
(`/crm/objects/2026-03/…`) alongside `v3`.

### Listing toolsets from the CLI

`loom toolsets` lists every integration a process can reach; `loom toolset <id>`
shows one, with each operation's effect class, whether it pages, what it
resolves, and the import line generated code needs. Both read manifests only —
Layer 1 — so listing costs no vendor imports and no credentials. Before this,
the only way to answer "is Salesforce wired up here?" was to start an MCP
server and ask it.

### ClickUp and Asana Toolsets

`toolsets/clickup/` and `toolsets/asana/` — 14 operations each, the same three
files every shipped toolset uses, pure httpx, no vendor SDK.

**Auth differs in a way worth knowing.** Asana is a plain bearer token
(`ASANA_ACCESS_TOKEN`). ClickUp has two shapes sent *differently*: a personal
token goes in `Authorization` **raw**, an OAuth token takes the `Bearer` prefix,
and sending a personal token as `Bearer pk_…` returns 401 with no hint why.
`CLICKUP_OAUTH_TOKEN` wins over `CLICKUP_API_TOKEN` when both are set.

**Paging.** ClickUp counts *pages* — `page=0,1,…` with a `last_page` flag — which
is `PageNumberPaging`, a dialect distinct from `OffsetPaging` because sending a
row offset where a page number belongs returns the wrong window and no error.
Asana carries an opaque offset inside `next_page.uri`, which `CursorPaging`
already parses.

**Asana's search neither pages nor ships on every plan.** It returns
`list[AsanaTask]`, not `Results`, and declares `pagination=False` — Asana states
results are unstable across identical queries, so there is no page to follow and
no coverage to report. `AsanaPremiumRequired` is its own error class because a
workflow can act on it: fall back to `asana_list_tasks` on a known project.

**Writes with no idempotency key are not retried** — creating a task, posting a
comment. A timeout after the service accepted it is indistinguishable from a
failure, so a retry files it twice. Updates and deletes retry once, since naming
the same task twice reaches the same end state.

Both mark their people-lookup as `resolves="user"`: every write in either API
takes an id (numeric in ClickUp, a `gid` in Asana), and a name passed where an id
belongs matches nothing and reports **no error**.

### Google Workspace Toolsets

`toolsets/google/` — four separately-grantable toolsets (`gmail`,
`google_calendar`, `google_drive`, `google_meet`) over one shared OAuth layer,
pure httpx, no vendor SDK. Four rather than one because a workflow reading a
calendar has no business holding a mail-send or a Drive-delete scope, and
`GrantSet(toolsets=["google_calendar"])` should mean exactly that.
`GOOGLE_MANIFESTS` registers all four in a line.

Credentials resolve from the environment in order: `GOOGLE_ACCESS_TOKEN`, then
`GOOGLE_CLIENT_ID`+`GOOGLE_CLIENT_SECRET`+`GOOGLE_REFRESH_TOKEN`, then
`GOOGLE_SERVICE_ACCOUNT_FILE` (the only one needing the `[google]` extra). One
cached token serves all four, refreshed under a lock — and a later toolset's
scopes are *merged into* it rather than dropped. Without that the second
toolset used gets a token carrying only the first one's scopes, which under a
service account is a Drive call authenticated for Gmail: a 403 that reads as a
broken credential rather than a shared cache. `python -m loom.toolsets.google.setup
--scopes drive` mints a refresh token; `read`/`write` are composed from the
per-toolset sets, so a scope added to one cannot be missing from the combined one.

**Errors are classified, not blanket-retried.** Google 4xx (bar 429) raises a
`NonRetryableError` subclass, so a plain `Retry` policy stops on a malformed
query rather than sleeping through three attempts. A 403 splits on `reason`:
quota is retryable, missing scope is not.

**Anything that a retry would duplicate has retries off.**
`gmail_send_message`/`gmail_reply_to_message`/`gmail_forward_message`,
`drive_upload_file`, and `meet_create_space`: none of those APIs offers an
idempotency key, so a timeout after the effect is indistinguishable from a
failure. Journaling covers replay; this covers the attempt. Calendar and Drive
metadata writes retry once — a duplicate event or a re-applied rename is
recoverable.

**Defaults that avoid surprises:** `send_updates="none"` and Drive's
`notify=False`, so bulk work does not email hundreds of people as a side effect
of a default; `singleEvents=True` so recurring series come back as instances;
`trashed = false` on every Drive search, so a workflow processing a folder does
not re-process the bin.

**Pagination is per-endpoint, because Google is not consistent with itself.**
Gmail and Calendar read `maxResults`, Drive and Meet read `pageSize`, and each
*ignores* the other rather than rejecting it — so the wrong name is not an
error, it is every request silently asking for the server default. Page
ceilings differ too (Drive files 1000, Drive permissions 100, Meet artifacts
10). `GoogleSession.paginate(size_param=…, page_size=…)` takes both, every
paged read returns `Results`, and `tests/test_manifest_imports.py` checks the
client, the return type, and the manifest agree.

**Every one of the six marks a resolver**, because every one of them accepts an
id where a person says a name: Gmail a *label* (`gmail_modify_labels` takes
`Label_7`, and passing "Urgent" applies nothing and reports success), Calendar a
*calendar* (a secondary calendar's id is an opaque
`...@group.calendar.google.com`), Drive a *folder*, Slack a *channel* and a
*user*, Zoom a *user*. Meet is the exception and needs none — its inputs are
resource names produced by other calls.

A resolver that pages has a **third** answer, and the two that scan a list say
so: `slack_find_channel` and `calendar_find_calendar` raise when the scan ran
out before matching, rather than answering `None`. "Not found" is a fact a
caller acts on — it creates the channel, or reports the gap — and it is only a
fact if the whole list was searched. `None` from a truncated scan silently loses
things that plainly exist, in exactly the workspaces big enough for it to
matter.

**Timeouts are configurable, and split in two.** Every client takes `timeout=`
(30s, an API call) and the four that move bytes also take `transfer_timeout=`
(300s). One budget for both would either fail every large Drive export and Zoom
recording, or leave an ordinary API call hanging for five minutes.

Two seams matter more than the individual tools, because in both cases the
obvious toolset is the wrong one:

- **Scheduling a meeting is a Calendar operation.** The Meet API cannot
  schedule anything — `meet_create_space` makes a room with a link and no time,
  no invitees and no calendar entry, which looks like success until nobody
  joins. `calendar_create_event(..., add_meet=True)` is the real path; it sends
  `conferenceDataVersion=1`, without which Google accepts the request, ignores
  the conference block, and returns an event with no link and no error. The
  `requestId` is *derived* from the event rather than random, so it is both
  deterministic and an idempotency key against a re-driven step.
- **A meeting's recording and transcript live in Drive.** Meet reports the ids;
  `drive_download_file` fetches a recording and `drive_export_file` reads a
  transcript — which is a Google Doc and so has no bytes to download.
  `MeetRecording.is_ready` exists because Meet reports a recording the moment
  it stops and the Drive file appears later.

**Drive's failure modes are silent, so the client closes them.** A missing
`fields` mask returns a file with no timestamps; a missing
`includeItemsFromAllDrives` returns an empty list for a team whose files live
on a shared drive; downloading a Google Doc is a 403 that reads as a
permissions problem. All three answer 200-shaped and make a workflow report
something untrue, so the mask is always sent, both shared-drive flags are
always on, and a Doc download is refused up front naming the export call.
`drive_find_folder` is marked `resolves="folder"` and matches exactly — a
`name contains` query returns "Reports Archive" for "Reports", and writing to
the wrong folder is worse than finding nothing.

**Gmail permanent delete is deliberately not exposed.** `messages.delete` needs
`https://mail.google.com/`, a *restricted* scope granting full mailbox access,
so shipping one unrecoverable operation would widen what every Gmail workflow
is granted. Trash is recoverable for 30 days and `gmail_untrash_message` undoes
it. Threads are the better triage unit — Gmail's UI groups by conversation, so
labelling one message of a thread looks like nothing happened — and
`gmail_list_threads` is one request per page where `gmail_search_messages` is
one per hit. `gmail_create_draft` is the safe half of sending: an agent writes,
`ctx.wait_for_approval()` parks, a human sends.

### Web Search Toolsets

`toolsets/exa/`, `toolsets/tavily/`, `toolsets/duckduckgo/` — three because
they are not interchangeable, and the manifests carry enough for the coding
agent to choose.

| Toolset | Credential | Paginates | Distinctive |
|---|---|---|---|
| `exa` (4 ops) | `EXA_API_KEY` (`x-api-key`) | no, cap 100 | Embeddings search — a *description*, not keywords. Page text, similar pages, cited answers. |
| `tavily` (3 ops) | `TAVILY_API_KEY` (bearer) | no, cap 20 | `include_answer` returns a written answer beside the results. News/finance topics, page extract, site map. |
| `duckduckgo` (3 ops) | none | **yes** | No key. Best-effort — see below. |

**Neither Exa nor Tavily has a cursor of any kind**, so a request above the cap
cannot be made whole. Both clients **refuse** rather than clamp: a caller that
asked for 500, received 100, and reported 100 as the total is the failure
`Results` exists to prevent, one layer earlier. The error is a
`NonRetryableError` naming the ceiling and the alternative, so `Retry` stops
instead of failing the same impossible request three times. Their reads return
plain `list`s with `pagination=False`; `duckduckgo` returns `Results` because
`ddgs` exposes a page number and the client follows it.

**Partial success is carried.** Exa's `/contents` and Tavily's `/extract`
answer 200 for a request in which some URLs failed, so the side array reaches
the caller as `.failed` — a short list with nothing saying it is short is the
same bug as a silent page cap.

**DuckDuckGo is not an official API.** They publish none; their one documented
endpoint returns instant answers and no web results. This rides on the
third-party `ddgs` package, which parses result pages — `pip install
'loomflow[duckduckgo]'`, optional precisely because it is a different
reliability contract from the other two. Two things are engineered around it:
being blocked raises a *retryable* `DuckDuckGoRateLimited` rather than
returning `[]` (which a workflow reads as "nothing matched" and acts on), and
the client drives the paging itself so `.complete` distinguishes "that is
everything" from "it stopped early" — asking `ddgs` for 30 returns whatever it
managed, silently. A *soft* block, no rows and no error, stays
indistinguishable from a genuine miss; that one cannot be fixed here. `ddgs` is
synchronous, so every call goes through `asyncio.to_thread`.

**All ten operations are `READ` and `idempotent`**, which is load-bearing
rather than bookkeeping: web search is the canonical taint source, so under
`TaintBroker` a run that has searched needs a human before it writes. Classified
as writes, no read could taint and the rule would be unreachable.

Tavily's own `timeout` parameter is exposed as `read_timeout`, because
`ctx.step` claims `timeout` and a tool declaring it is unreachable by keyword.

### OneDrive and SharePoint Toolsets

`docs/design/onedrive-sharepoint-toolsets.md` is the research these were built
from — Graph API notes, schemas, and the decision each fact forced, with sources.

`toolsets/microsoft/` — two separately-grantable toolsets (`onedrive`,
`sharepoint`; 18 and 19 operations) over one shared Graph layer, pure httpx, no
vendor SDK. **A SharePoint document library *is* a `drive` and its files *are*
`driveItem`s**, so `models.py` is shared and a file moved between the two keeps
one shape. They stay separate toolsets because the grant boundary is real.

Credentials resolve in order: `MS_TENANT_ID`+`MS_CLIENT_ID`+`MS_CLIENT_SECRET`
+`MS_REFRESH_TOKEN` (delegated — acts as a person), the same three without it
(client credentials — acts as the app), then `MS_GRAPH_ACCESS_TOKEN`.
`AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET` are accepted as
fallbacks, since that is the trio the Azure SDKs already put in an environment.
The durable credential outranks the ready-made one for the same reason it does
in `GoogleAuth`. One cached token serves both toolsets.

**`/me` does not exist under an app-only token.** Client credentials
authenticate the application, so there is no signed-in person and `/me/drive`
fails with a 400 that reads as a broken toolset rather than a missing argument.
The clients refuse **before the request**, naming both fixes: `MS_ONEDRIVE_USER`
/ `MS_ONEDRIVE_DRIVE_ID`, or authenticate as a person.

**Paging reuses `LinkPaging`**, and the reference is why: `@odata.nextLink` is a
complete URL and the docs say *"Don't try to extract the `$skiptoken` […] and
use it in a different request"* — which is what `CursorPaging` does. The
follow-up therefore sends the URL verbatim with no parameters of its own; note
that `httpx` clears a URL's query when handed even an empty `params` dict, which
silently re-fetches page one forever.

**A SharePoint column has two names and the wrong one fails silently.** Item
values are keyed by the *internal* name ("Due Date" is `DueDate` or
`Due_x0020_Date`); a write using display names is accepted and sets nothing, so
the row is created and the value is missing. `sharepoint_list_columns` carries
`resolves="column"` and returns both. Likewise `$expand=fields` is always sent,
because Graph hides item values by default and an unexpanded read looks like an
empty list rather than a missing parameter.

Two smaller traps, both test-pinned: Graph's path escape needs a *second* colon
when anything follows it (`/root:/Reports:/children`), which is why
`addressing.py` exists; and an upload session's fragment `PUT`s must carry **no**
`Authorization` header — the upload URL is pre-authenticated and signing it can
401 — making them the only deliberately unsigned requests in the codebase.
`onedrive_upload_file` refuses over 10 MiB and names
`onedrive_upload_large_file`, whose 5 MiB chunk is a multiple of 320 KiB by
construction, because a violation of that rule fails only after the last
fragment. `onedrive_list_changes` wraps `delta` — Graph names polling as a
leading cause of throttling — and returns the delta link beside the items,
since a caller that drops it re-enumerates the whole drive next time.

### Teams, OneNote, and Outlook Toolsets

`docs/design/teams-onenote-outlook-toolsets.md` is the research these were built
from. Four toolsets — `teams` (16 ops), `onenote` (12), `outlook_mail` (15),
`outlook_calendar` (11) — over the same `toolsets/microsoft/` layer OneDrive and
SharePoint use. **Outlook is two toolsets, not one**, for the reason the Google
package already gives: reading a calendar should not confer sending mail.

**The theme is that Microsoft restricts app-only auth inconsistently**, and the
rule adopted is *refuse what cannot work, document what might not*.
`microsoft/scope.py::user_root` is the shared refusal, now used by five clients:
a `/me` path under client credentials cannot resolve, so it raises before the
request naming `MS_TEAMS_USER`/`MS_ONENOTE_USER`/`MS_OUTLOOK_USER` and
`MS_REFRESH_TOKEN`. A resource addressable without a user — `drive_id`, a
OneNote `site_id`/`group_id` — bypasses it. Two restrictions are *not* refused:
**sending a Teams message is delegated-only** (application permissions cover
only `Teamwork.Migrate.All`, and a migration app is a real caller), and
**OneNote's overview says app-only is unsupported while its per-operation pages
list an application permission** — a contradiction quoted in the manifest rather
than resolved by guessing.

**Teams.** Graph's own docs say polling a resource more than once a day violates
the Microsoft APIs Terms of Use; that is in the manifest because the coding
agent is what would otherwise write the cron. Channel messages support **only**
`$top` and `$expand` — no filter, no sort, silently ignored — so the client
offers neither. `$top` caps at 50. Replies get their own operation because
`$expand=replies` truncates at 200 behind a *nested* `replies@odata.nextLink`,
and `$expand=members` on chats caps at 25 with no marker. Channel ids
(`19:…@thread.tacv2`) carry a colon and an `@` through a path segment.

**OneNote.** A page's content is an **HTML document, not JSON**: reading returns
a string, and creating posts `Content-Type: text/html` whose `<title>` *becomes*
the page title — there is no title field, so posting a bare fragment yields an
untitled page and no error. `create_page` therefore assembles the document from
a title and a body. Updates are `target`/`action`/`content` commands, and
targeting anything but `body`/`title` needs `includeIDs=true` on the read.

**Outlook mail.** Bodies come back as HTML unless `Prefer:
outlook.body-content-type="text"` is sent, so the client sends it by default —
and re-sends it per page, since a next-link carries parameters but not headers.
`$filter` and `$orderby` have an ordering contract (sorted properties must
appear in the filter, in order, first) or Graph answers `InefficientFilter`.
Listings project a field set because a large page of full messages can hit a
504. `sendMail` returns **202 = accepted, not delivered**, and the tool says so.

**Outlook calendar.** `calendarView` versus `events` is the whole design:
`/events` returns series *masters*, so "what is on Tuesday" asked there misses
every recurring meeting and returns a plausible short list. `calendarView`
expands occurrences over a required window — the same call `singleEvents=True`
makes for Google Calendar. Times are UTC unless `Prefer: outlook.timezone` is
set, and that header does *not* reinterpret the window, so the range values need
their own offsets. `add_teams_meeting` sets `isOnlineMeeting` **and**
`onlineMeetingProvider`; setting only the first yields an event that claims to
be online and carries no join link. `cancel` notifies attendees where `delete`
leaves their invitations in place.

### Slack and Zoom Toolsets

`docs/design/slack-zoom-toolsets.md` is the research these were built from —
API notes, schemas, and the decisions each fact forced, with sources.

**Slack's failures are HTTP 200s.** `{"ok": false, "error": "channel_not_found"}`
with a 200 status line. A client written to the shape every other toolset here
uses — raise above 399, else decode — treats every failure as an *empty
success*, so a workflow posting to a channel it was never invited to reports the
message as sent and delivers nothing. `toolsets/slack/errors.py` therefore
classifies on the `error` string, and every response goes through
`raise_for_status`. `missing_scope` gets its own type because the fix is a
different action in kind — a reinstall, by a person — and Slack names the scope
it wanted.

**Slack's cursor is nested, and that needed no new dialect.** It lives at
`response_metadata.next_cursor` and signals exhaustion with an empty string.
Both are already `TokenPaging` behaviours — a tuple `token_field` addresses a
nested position, as HubSpot's `paging.next.after` does — so Slack uses that. A
`NestedTokenPaging` class was written and then deleted: it was the same dialect
twice, which is the second source of truth `pagination.py` exists to prevent.

**Everything in Slack takes an id, never a name.** `#incidents` is what people
type and `C024BE91L` is what Slack accepts, so `slack_find_channel`
(`resolves="channel"`) and `slack_find_user_by_email` (`resolves="user"`) are
the two resolvers, and both match *exactly* — a prefix match would return
`#eng-alerts` for `#eng` and post to the wrong room. `files.upload` stopped
working in March 2025, so `slack_upload_file` is three calls to two hosts behind
one tool, and the bot token is deliberately not sent to the pre-signed storage
URL.

**Zoom has two identifiers and they are not interchangeable.** `meeting.id` is
numeric and names the *series*; `meeting.uuid` names one *occurrence*, and every
past-meeting endpoint takes the second. Worse, a uuid is base64: one beginning
with `/` or containing `//` must be **double** URL-encoded or Zoom answers
`3001 Meeting does not exist` for a meeting that plainly does. `encode_uuid`
applies the rule conditionally — double-encoding one that does not need it
produces the same 3001 from the other direction.

**`meeting.start_url` is a host credential**, carrying an embedded token that
lets anyone who opens it run the meeting. That warning is a pydantic `Field`
description rather than an attribute docstring, deliberately: only the former
reaches `model_json_schema()`, which is what the manifest publishes and what the
coding agent reads. A warning that lives only in the source is one the agent
writing the code never sees.

**A Zoom daily rate limit is non-retryable on purpose.** Both limits arrive as a
429, and only the message text tells them apart — but a per-second limit clears
while a step backs off and a daily one does not clear until midnight UTC, so
retrying against it burns the run to reach the same answer. `ZoomDailyLimitReached`
is a `NonRetryableError`; `ZoomRateLimited` is not.

**Auth differs between them.** Slack is an ordinary bot token — `loom connect
slack` (already a provider) or `$SLACK_BOT_TOKEN`. Zoom's default is
Server-to-Server OAuth, which has **no refresh token**: the client id and secret
*are* the durable credential and an hourly token is minted from them on demand,
so the credential-store refresh machinery does not apply and `toolsets/zoom/auth.py`
caches its own under a lock, as `GoogleAuth` does for a service account.
`loom connect zoom` (a new provider entry) covers the user-delegated case.

Neither posts nor schedules under a retry: `chat.postMessage` and
`meetings.create` have no idempotency key, so a retry after a post-delivery
timeout posts the message twice — visibly, to everyone — or puts a second
meeting on the calendar with a different join link.

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

`loom.testing.advance` / `advance_to` do all three steps —
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

### Embedding Loom in a host

`docs/guides/embedding.md` is the end-to-end walkthrough for putting Loom inside
a product with its own users, database, and notification transport — store,
sandbox, human channel, broker, versions, credentials, and event idempotency,
composed by the host. Every snippet on that page executes in CI.

`tests/test_host_integration.py` is the same host as a test, and it is the
phase's acid test rather than a demo: one run goes start → **sandboxed** →
parked on a human → answered over the host's own channel → resumed → traced back
to the version of the code that ran. A final test greps the file for
`runtime._…` and fails on a match, because a host that has to reach past a seam
has found a seam that is not finished — and that creeps back one underscore at a
time.

### Hooks and middleware

`rt.hooks` (`runtime/hooks.py`) runs middleware around every durable operation —
step, node, tool, agent, child, artifact, event. Empty by default, and **free
while it stays that way**: the first registration is what installs a
`HookBroker`, so a Runtime with no hooks runs exactly the chain it ran before.

```python
@rt.hooks.before_tool(effect=EffectClass.DESTRUCTIVE)
async def confirm(ctx): ctx.ask("deletes data")

@rt.hooks.around_step(target="jira.*")
async def cached(ctx, next): ...
```

**One primitive, three shapes.** Everything compiles to `async def (ctx, next)`,
the onion — `before` and `after` are that shape with a fixed structure. Nesting
is what makes `before` run forward and `after` run in reverse, so the ordering
is a consequence rather than something enforced.

Two bugs the compilation makes unavailable. **`before`/`after` never receive
`next`** — a sequential middleware that forgets to continue silently drops
everything behind it, invisibly, so the wrapper calls it instead; refusing is
`ctx.deny(...)`. And **a decision cannot be assigned**, only escalated along
`allow < ask < deny` via `max()`, so a permissive middleware registered after a
strict one is a no-op rather than a silent hole.

**Replay never reaches a hook**, because `DurableCall._resolve` serves a
completed entry from the journal before the broker. That is what makes it safe
for these to perform I/O and to refuse — and it is why hooks that fire on every
body re-entry will be a *different family* with different rules, not more
events on this one.

`ctx.ask()` **parks the run** rather than blocking: the hook's nested context
calls `wait_for_approval`, the run suspends costing nothing, and on resume the
hook asks again and finds the journaled answer. `ctx.ctx` is
`nested(f"{path}#hook")` — a path derived from the hooked call rather than a
sequence counter, so a supervisor or critique that calls a model journals once
instead of being paid for on every retry.

Fail policy differs by shape and both directions are deliberate: `before` fails
**closed** (a gate that could not run has not passed), `after` fails **open**
(the work already happened; a broken formatter must not destroy a valid result).

`HookRegistry` is **per-Runtime and deliberately not chained to a process-global
parent**, unlike `toolsets` and `nodes`. Those answer "what exists?"; this
answers "what does this deployment enforce?", and sharing that means one test's
gate silently applies to another's run.

One inherited sharp edge: `ctx.arguments` reports **keyword arguments only** —
a positionally-invoked step has no argument names to report. Deciding on *what*
is called is unaffected; deciding on *values* needs the call to use keywords.

**Three families, and the split comes from where journaling sits.** The effect
family above is one; the other two exist because a hook can otherwise mean two
different things.

`on_workflow_start` / `on_workflow_end` are the **body family**, in
`_invoke_body`. They fire on *every* body entry, replay included — so
`ctx.re_entry` tells "this run began" from "this run resumed", and `ctx.status`
names how the body exited (`completed`, `suspended`, `cancelled`, `failed`,
`rotated`, `abandoned`; parking is not failure). **They cannot decide**, and that
absence is the design: a body hook that could refuse would let a replay
re-derive an outcome from middleware that has since changed, which is also what
would force middleware into the workflow version.

`on_agent_start/end`, `on_turn_start/end`, `on_model_start/end` are the **agent
family**, in the runner. Replay-free by containment — an agent run is a single
journal entry. Also non-deciding, but for a different reason: "may this agent
run?" and "may this tool call run?" are *already* effect hooks on `kind="agent"`
and `kind="tool"`. What is left is shaping and observing, which is most of what
middleware does. `on_model_start` mutates `ctx.messages` **in place** — that is
the compaction/trimming/redaction point, and rebinding the list does nothing.
`ctx.stop(reason)` ends the loop — the runner raises `AgentStopped` at the top of the next turn, because the loop's only other early exit (the turn budget) raises too, and a partial result handed back quietly would make "a stall detector gave up" indistinguishable from "the agent finished". First stop wins, reason included, matching the escalate-only decision rule.

Both non-deciding families **always fail open**: they cannot change what a run
does, so a broken logger must not become a failed workflow — least of all one
that fails only where that logger is installed.

`rt.hooks.use_guardrail(g)` registers an existing `Guardrail` as a hook. An
**adapter, not a migration**: `Agent(guardrails=[...])` is untouched, because an
agent can run with no Runtime and so no registry. Node `guards` are left alone
too — a guard validates a *payload* across input/output phases, where a hook
gates *dispatch*, and merging them would make two different things share a name.

`ExecutionRecord.metadata["loom.middleware"]` records what was in force when a
run opened. On the **run**, never in the version: middleware states what a
deployment enforces, not what a workflow is, so folding it into `content_hash`
would give one commit as many versions as it has environments.

Design and the research behind it: `docs/design/hooks-middleware.md` and
`docs/design/hooks-middleware-design.md`.

### Replaying a failure

A replay reproduces a recorded failure's **type**, not only its message, for the
failures the engine itself produced. A workflow branches on the type —
`except EffectDenied` is how it tells "policy refused this" from "this broke" —
so rebuilding every recorded failure as a generic `StepError` sent a replay down
a different branch from the run it was rehearsing. Journaling the denial existed
to prevent that divergence; losing the type reintroduced it one layer down.

`DurableCall._recorded_failure` owns the reconstruction, and it is deliberately
narrow: **only failures the engine produced**, because it knows their
constructors. Two rows today:

| Recorded | Replays as | Read from |
|---|---|---|
| `error.type == "EffectDenied"` | `EffectDenied`, `needs` intact | `denied_needs` on the entry |
| `EXHAUSTED` and `attempts > 1` | `RetriesExhausted` | the entry's own attempt count |

`EffectDenied` keeps its `needs` — the actionable half — recorded alongside as
`denied_needs`, because `ErrorInfo` carries a type and a message and nothing
else. `RetriesExhausted` is keyed on **`EntryStatus.EXHAUSTED`** rather than on
the error type, because the journal deliberately records the *original* error
(`ConnectionError` and its text) and the status is what says a retry budget was
spent. Its message is rebuilt rather than read, for the same reason. The
`attempts > 1` condition mirrors the engine's own rule: a step that never
retried raises its original exception, so claiming exhaustion would invent an
attempt that never happened.

The two differ in blast radius, which is why they shipped separately.
`RetriesExhausted` **is** a `StepError`, so widening a replay to it is invisible
to anything already catching the base class. `EffectDenied` is not — but the
first run already raised it, so only the replay ever disagreed.

**A user exception is still widened to `StepError` on replay**, and that gap is
asserted rather than papered over. The journal records `type: "ValueError"`, so
what is missing is not the information but a safe way to use it: rebuilding an
arbitrary exception means guessing at a constructor, and the class may not be
importable in the process doing the replay.

### Read-to-write taint

`Runtime(broker=TaintBroker(GuardedBroker()))`. One rule: **once a run has read
data it did not bring with it, a write or a destructive call needs a human.** A
workflow that searches the web and then deletes tickets has taken instructions
from something nobody reviewed — the property you want when a model wrote the
body. Off unless composed in; taint sits *outside* whatever performs the effect,
because it decides whether to dispatch at all.

`block_writes` and `block_destructive` are separate dials: nearly every useful
workflow writes after reading, and very few need to delete. `ctx.wait_for_approval`
clears the taint, and a read after that approval taints again.

**Taint is derived from the journal, never accumulated in memory** — and the
reason is that memory fails *open*. The engine re-enters a body from the top and
serves every already-answered call from the journal without dispatching it, so a
broker counting dispatches sees an empty history after any park, retry, or
restart and permits everything. `RunObserver` (`runtime/effects.py`) is the hook:
`observe_run(run_id, journal)` at re-entry and again when an event lands
mid-body, `forget_run` at terminal. Any broker wrapping another must forward
both.

That mid-body call is not belt-and-braces. An approval is journaled by
`wait_for_event` and **never dispatched**, so a broker only learns of one by
reading the journal — and at re-entry that entry is still `SUSPENDED`, becoming
the human's "yes" only while the body runs.

**A step's effect class comes from its manifest.** `ToolsetCatalog.effect_of()`
maps a `@step` function name to the `EffectClass` its `OperationSpec` declared,
and `ctx.step` attaches it. Without that every call reached the broker as a
*write* — the default for anything unclassified — so no read could taint and the
rule was unreachable in practice. A plain local `@step` stays unclassified;
inventing a class for it would guess at the declaration a manifest exists to
make.

### Credential resolution

`ConnectionBroker(resolver=...)` takes a `CredentialResolver`
(`toolsets/connections.py`); `EnvCredentialResolver` is the default and is
today's `LOOM_CONN_{ID}_TOKEN` behaviour, unchanged and now named. A host with a
credential service implements two methods instead of monkey-patching a concrete
class.

### Keeping OAuth tokens alive

**Two thresholds, and the gap between them is the whole design.**
`RefreshPolicy.is_due` (default: ten minutes before expiry,
`$LOOM_OAUTH_REFRESH_SKEW`) is when renewal is *attempted*.
`StoredCredential.is_expired` is when failing to renew becomes an *error*.
Waiting for expiry is renewing too late — a token with two seconds left passes
every check and then 401s on the request it was fetched for — and the window
absorbs clock drift against the authorization server, which is otherwise
indistinguishable from a token that died early.

In between, `get()` **fails soft**: no refresher, a network blip, an
authorization-server outage, and it returns the credential that is still valid
and tries again next call. Raising there would take a working token away ten
minutes before it was needed, turning someone else's transient failure into
ours. Past expiry it raises, because there is nothing left to fall back to.

**The window is clamped to half the token's own lifetime** (`max_fraction`,
which is why `StoredCredential` carries `issued_at`). A provider issuing
five-minute tokens under a ten-minute window would otherwise report every token
as due the moment it was minted: every call refreshes, the server sees a storm,
and where refresh tokens rotate, each rotation invalidates the last. The clamp
is also what lets `OAuthClient._peek_if_fresh` judge another process's write by
the same policy — a freshly minted token has its whole lifetime ahead of it, so
two processes cannot bounce one credential between them.

**`store.refresh(name)` renews now; `store.get(name)` renews if due.** Same code
underneath, so they cannot drift on locking, rotation, or write-back — but
`refresh()` always raises on failure, because the caller asked for the renewal
and "the old token still works" answers a different question.

**`CredentialRefreshService` (`connectors/refresh.py`) is the background half**,
and it holds no renewal logic at all — it decides *when* to ask and calls
`store.refresh()`. `start()` sweeps immediately (that is "on restart"), then on
a timer, and registers through `Runtime.supervise()` so `shutdown()` stops it.
`loom serve` and `loom mcp` start one; short-lived commands rely on the skew in
`get()`, and `loom refresh [--all] [--force]` is the explicit hook for a systemd
timer or a login profile — exit **1** when any credential could not be renewed,
so a scheduled run reports a dead refresh token instead of succeeding quietly.
A failed credential backs off exponentially to an hour: retrying a permanently
dead refresh token every sweep is a self-inflicted flood that gets the *working*
credentials rate-limited too.

**Not built on cron, deliberately.** `TriggerDispatcher` fires workflows through
`Runtime.submit()` — an `ExecutionRecord` and a journal per occurrence, thousands
of run records a year to keep one token alive, and a hard dependency on a store
that `loom login` does not require. Cron's exactly-once-per-occurrence guarantee
exists to stop double-firing side effects; a refresh is idempotent and
self-healing, so a missed one should be retried, never `catch_up`-backfilled.
What is reused is the machinery around it: `supervise()`, the `Clock` port, and
`LockProvider` for cross-process single-flight.

**The CLI's store is now the Runtime's ambient store.** `loom connect gmail`
previously stored a credential no workflow could read — the CLI could
authenticate something it then could not use, indistinguishable from the connect
having failed. `targets.resolve()` attaches it as `Runtime(credentials=…)`,
which `_credentials_for` treats as *ambient*: a per-run `credentials=` still
wins, and a name the run declared is never satisfied from here, so this widens
what an unspecified run can reach without ever swapping a caller's identity. It
is also built against the Runtime's store as a `LockProvider`, which closes a
real gap — two `loom` processes could previously refresh the same credential at
once and, on a rotating server, invalidate each other's refresh token.

### Sandboxed execution

`Runtime(sandbox=...)` decides **where the workflow body is invoked**. The
default `InlineSandbox` runs it in this process — what every Runtime already
did, and what a developer wants. A host executing code a model wrote, against
credentials the host holds, passes `SubprocessSandbox`:

```python
from loom.runtime.sandbox import SandboxPolicy
from loom.runtime.sandboxes import SubprocessSandbox

rt = Runtime(store=…, sandbox=SubprocessSandbox(),
             sandbox_policy=SandboxPolicy(allowed_env=frozenset({"TZ"}),
                                          max_wall_seconds=60))
```

**Not a `DurabilityBackend`.** That port answers where durability *lives*
(embedded, Temporal, DBOS); isolation is orthogonal — you want a sandbox on the
embedded backend, and Temporal has its own workers. Conflating them means a host
cannot have both.

The child holds **no store, no journal, and no credentials**. Every `ctx.*` call
is a line of JSON back to the parent, which turns it into the ordinary
`ctx.step(...)` it would have been — so a sandboxed run and an inline one
produce an *identical journal* and pass the same broker chain, grants, budgets,
dry-run, and taint. Untrusted orchestration over trusted effects: the body
decides what to call, the parent decides whether, performs it, and records it.
`tests/test_sandbox.py` asserts the journals match entry for entry.

Three things worth knowing:

- **The steps map is the allowlist.** A sandboxed body can only reach `@step`s
  defined in its own workflow's module. A process-wide registry would hand every
  sandboxed workflow every step any import had ever defined.
- **A limit that cannot be applied is refused, not ignored.** `enforces` reports
  what this platform actually honours — macOS has `RLIMIT_AS` and rejects every
  finite value, including a lower one — and `run` refuses a policy asking for
  anything outside it. A host told "not here" is better off than one that
  believes untrusted code is bounded when nothing bounds it.
- **Parking is not an outcome.** `Suspend` and `WorkflowCancelled` propagate out
  of a sandbox untouched. A subprocess body that parks dies with its child and
  re-executes from the top on re-entry, with every earlier call served from the
  parent's journal — the deterministic re-entry the engine already relies on.

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

`loom.testing`. The replay engine already prefers a recorded entry
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
inflate the version chain. And a replay of `get_artifact` reads what the
original run read — a replay rehearses what happened, not what would happen now.
It buys that by journaling the **content**, not the version: with
`Runtime(blobs=...)` the payload goes back out over the offload threshold and
dedupes against the artifact's own bytes, so the cost is a reference; without
one it is inline, and `journal_max_payload_bytes` is what stops a 500 MB read
from becoming a 667 MB journal row. Prefer `artifact_url` for large artifacts —
it is deliberately not journaled.

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

**An interrupt must be no worse than a crash.** `_drive` settles its lease by
reading the record rather than by being told: every normal exit has already
moved it off `PENDING`/`RUNNING` (terminal via `_finish_*`, `SUSPENDED` via
`_park`), so one still unfinished means the drive was cancelled — Ctrl+C, a
SIGTERM handler, `shutdown()`. That run gets `lease_owner` as a breadcrumb and
its lease expired *now*, so the next `reclaim_orphans()` takes it immediately.

Clearing it instead is the bug this replaced, and the shape is worth
remembering: `reclaim_orphans` matches on `lease_expires_at`, so a nulled one is
unmatchable and the run stays unfinished with no timer covering it and nothing
able to find it — a Ctrl+C stranded a run that a `kill -9` recovered. The engine
names `asyncio.CancelledError` for the same reason: it is a `BaseException`, and
letting it reach the `except Exception` arm would record a healthy run `FAILED`
on a keystroke and unwind compensations for work about to be resumed.

**`PENDING` is scanned as well as `RUNNING`**, for the drive cancelled in the
one store round-trip between creating the record and taking the lease. What
keeps that safe is that the *expired lease* is the signal, never the status: a
record `submit()` has created and not yet driven carries no lease at all, so it
is never matched however long it queues. Only a drive that reached its `finally`
leaves one — exactly the runs somebody has stopped working on.

`shutdown(drain=5.0)` stops the *sources* of new runs first — supervised
services, then the scheduler — then lets in-flight drives finish, then cancels.
`TriggerDispatcher` and `QueueConsumer` register themselves via
`Runtime.supervise()` from their own `start()`, so a host does not have to know
which it wired up. Whatever the deadline cuts off is exactly the interrupted-run
case above and recovers the same way, which is what lets the deadline be short.

**`async with Runtime(...) as rt:`** is how a host should say it — a trailing
`await rt.shutdown()` only runs when nothing goes wrong, which is the case where
it matters least. `loom.runtime.shutdown.run_main(main())` is the matching
`__main__` block: `asyncio.run` plus signals and an exit code. Every cookbook
example uses both, so the pattern a reader copies is the one that cleans up.

### Store parity

Every store claims `ExecutionStore + CacheStore + LockProvider + TriggerStore`
— the last now including `claim_due_triggers` — while Redis deliberately claims
only `CacheStore + LockProvider`. `tests/conformance/` runs **one behavioural
suite against all four backends**, with Mongo and Postgres reached through real
servers in CI.

That harness is not ceremony. Before it, the suite covered `memory` and
`sqlite` and the other two were covered by `hasattr` — and its first run
against real servers found five divergences, including a `sparse=True` unique
index that let `MongoStore` hold exactly **one** run without an idempotency
key, and a `update_after_fire` statement Postgres refused to prepare at all.

```bash
pytest tests/conformance tests/test_store_conformance.py -rs   # -rs prints skip reasons
LOOM_TEST_STORES=memory,sqlite pytest ...                      # fast local loop
```

**A backend that cannot be reached is SKIPPED and named, never dropped.** A
suite that quietly shrinks when a service is down reports green for coverage it
did not have. `tests/conformance/test_harness.py` drives deliberately-broken
stores through the suite to prove it still catches each defect class.

### Workflow versions

`WorkflowRecord` says a workflow exists; `WorkflowVersion` says what its code
*was*. Opt-in and off by default:

```python
await rt.publish(flow, source=src, pins=Pins(toolsets={"jira": "1.2"}))
version = await rt.version_of(run_id)      # the code that ran, not the latest
source = await rt.versions.source_of(version)
```

`VersionStore` is a port — `StoreBackedVersionStore` is the default, and a host
storing versions in its own database implements six methods and passes
`Runtime(versions=…)`.

Three properties worth knowing. **Content goes to blobs**, not into the record,
so a 200KB workflow is not a document-store problem and the same code works on
every store. **Identical source returns the existing version** rather than
appending, so a retried publish does not inflate the chain. And a version
carries *two* hashes: `content_hash` identifies the source a human committed,
`code_hash` is what a finished run records — recording only one leaves either
"show me this version's source" or "which version produced this run"
unanswerable.

Commits are serialised per workflow through `LockProvider`, which every store
implements, rather than a store-specific atomic — eight concurrent commits lost
seven of each other before that, on every backend including Memory.

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

`CodeValidator` also resolves imported symbols, so `from loom import
Retryy` is caught with a suggestion rather than failing on the user's machine.

Optionally add a **supervisor**: `WorkflowCodingAgent(supervisor=CodeSupervisor(model))`
runs a second model over the finished code — durability, determinism, retry
safety, error handling, spec fidelity. Use a different model from the author
where you can; one model reviewing itself mostly agrees with itself.

`CodingResult.is_clean` means the code validates, runs, *and* passes review. Code
that merely parses is a weak claim; so is code that runs but charges twice.

### Event backbone

`loom/events/`, designed in `docs/design/event-backbone.md`. **A log, not a
bus.** A bus makes *delivery* durable, so a subscriber that was down missed
those events permanently and one added tomorrow sees nothing from today. A log
makes *the record* durable and delivery a resumable read — the only shape where
many workflows independently consume one event and every one of them survives
being killed.

**The package ships no broker.** Two Protocols (`EventLog`, `Checkpoints`), one
reference implementation over capabilities every store already has
(`StoreBackedEventLog` on `CacheStore` + `LockProvider`), and conformance kits
(`loom.testing.conformance.verify_event_log` / `verify_checkpoints`) so a host
proves its own Kafka, Redis Streams, or Postgres adapter correct. Sixteen
integration matrices is not a maintainable position in year three.

```python
@workflow(triggers=[OnAppEvent("app.slack.message",
                               where=FilterSpec(conditions={"channel": "C_TECH"}))])
async def triage(ctx, message: dict) -> str: ...

rt = Runtime(store=store, events=StoreBackedEventLog(store))
dispatcher = EventDispatcher(rt)
await dispatcher.register(triage)      # async: it pins where LATEST starts
await dispatcher.start()               # supervised, so shutdown() stops it
```

**One rule, everywhere: do the durable idempotent thing first, advance the
marker last.** `drain()` reads a batch, filters, submits under
`{event_id}#{subscriber}`, and commits the checkpoint *after*. Committing early
loses events permanently — the marker says "handled", nothing ran, and no
provider resends. Committing late costs a re-read the dispatch key absorbs. A
deferral stops the batch rather than skipping ahead, so the checkpoint sits just
before the event that failed.

Four decisions worth knowing:

- **Identity is a stable name and never the filter.** Hashing a filter in makes
  every filter edit a new subscriber, which changes every historical event's
  dispatch key, deduplicates nothing, and re-runs everything. With a stable name
  widening a filter forward is an edit, and widening it retroactively is a
  replay that is safe *by construction* — already-handled events re-derive their
  original key and dedupe away.
- **`LATEST` is pinned at `subscribe()`, not at the first poll.** Hence the
  `async`. Resolving it lazily looks equivalent and silently drops everything
  that arrived in the gap.
- **`EARLIEST` is reachable programmatically and refused in a declaration.**
  Backfilling a week of Slack into a workflow that replies means a week of
  replies at once, and the dispatch key does *not* protect a genuinely new
  subscriber. Backfill is an operational act with bounds, not a line in a
  workflow file.
- **Bounded attempts, then a real dead-letter topic** (`<topic>.dead`). Retrying
  forever stalls a subscriber behind one bad event; skipping silently loses it.
  `CHAIN_DEPTH_CAP = 5` stops a workflow that publishes an event that
  re-triggers it.

**A dead letter is about delivery, not execution.** An event nobody could
dispatch — an unknown workflow, a permanent admission rejection — is
dead-lettered and stepped over. A workflow that *was* dispatched and then raised
is an ordinary **failed run**, with a journal and `retry`/`replay` semantics a
dead-letter would throw away; `loom runs --status failed` is where it lives. The
same asymmetry `QueueConsumer` already has, and getting it backwards means
either losing the journal or stalling the stream.

An event both *starts* runs and *resumes* those parked on
`ctx.wait_for_event` — a trigger creates a run, a wait continues one, and one
event can legitimately do both.

### Event sources and ingress

`docs/guides/event-sources.md` is the end-to-end walkthrough, and every snippet
on it executes in CI. `EventSource` is four small methods — `verify`,
`challenge`, `delivery_id`, `expand` — rather than one `handle()`, so a provider
with no handshake writes `return None` instead of re-implementing a dispatch
loop. **Adding a provider costs a verifier and a normaliser**; if it ever costs
more, the seam is in the wrong place.

Registered by name, discovered through the `loom_event_source` entry point, and
`rt.sources` chains to the process-global registry exactly as `toolsets` and
`nodes` do. Shipped: `slack`, `jira`, `gmail`.

**`verify` is handed the raw body**, because every scheme in use signs bytes. A
source that parses first and verifies the re-serialised form accepts anything —
and passes every hand-written test, since a JSON round trip is lossless in the
happy case. `loom.testing.conformance.verify_event_source` is the kit that
catches it, along with an unstable `delivery_id` and an un-namespaced event
type.

**Verify, then answer a handshake — never the other way round.** A challenge
answered before verification is an oracle: anyone who guesses the URL gets a
signed-looking reply and can complete somebody else's endpoint registration.

**The ingress owns identity, not the source.** `{topic}/{source}:{delivery_id}`,
and the topic is not decoration — Slack's `app_mention` is also a `message`, so
an id without it would make those one event and whichever landed second would
deduplicate away. A provider publishing no delivery id (Jira) falls back to a
body hash, which deduplicates an identical redelivery and honestly cannot tell
two identical events apart.

**Two HTTP routes, because two contracts exist.** `/hooks/{source}` is the
provider-typed one; `/webhook{path}` is what `Webhook.describe()` has been
publishing all along, and an advertised URL is a promise. `/webhook-test` is
registered *first*, because `{path:path}` is greedy and would otherwise swallow
it. Both are thirty lines over `WebhookIngress.receive`, which is transport-free
— a Lambda or a Django view gets identical behaviour.

**Shape B — a payload that is a position.** Gmail posts a `historyId`, not an
email. `expand` returns one *pointer* event; a `Reconciler` driven by
`PointerReconciler` asks the provider what changed and appends the data events
back. Downstream reads `app.gmail.message` and never learns Gmail is different.
Nothing is fetched inside `expand` — an API round trip inside Pub/Sub's
three-second budget makes a slow mailbox produce duplicate pushes *as well as* a
slow response. The provider cursor lives in `SourceState`, not in a checkpoint:
the checkpoint says how far the reconciler read *our* log, the cursor how far it
read *theirs*.

**A dead cursor is an event, not an exception.** Gmail keeps about a week of
history, Salesforce 72 hours; past that the provider cannot say what was missed.
Jumping silently to now is the failure where "nothing arrived today" and "we lost
a day" are indistinguishable — so a `*.gap` event is appended and the cursor
resets forward, and a workflow can subscribe to lost visibility.

**A lapsing subscription is the highest-severity silent failure.** Gmail's watch
dies in 7 days, Graph's in ~3; nothing errors, and the inbox looks quiet.
`WatchRenewer` re-registers at a *fraction* of the lifetime (declared via
`lifetime_hint`, since assuming a week renews a three-day watch after it died),
sweeps immediately on start, and appends `*.watch_lapsed` **once** a watch is
actually dead — not on each failed renewal, because alerting on survivable
failures trains people to ignore the one that matters. `stop()` deliberately does
not tear the subscription down: a rolling restart must not deafen the mailbox.

### Operating the backbone

`SubscriptionManager` holds the durable registry and answers "who is reading, and
how far behind". Separate from the dispatcher because that is meant to be a hot
loop with no storage of its own. Lag is **counted by reading**, never subtracted
— `Position` is opaque, and subtracting works here and is wrong on every
partitioned backend.

A subscriber whose checkpoint has not moved past `subscriber_ttl` is
**quarantined, not retired**: retention proceeds past it, its position is kept,
and resuming it raises `GapDetected` naming what it missed. Reading health has no
side effects — quarantining is an operator's act, and a status command that
changed what it reports by reporting it is useless.

```bash
loom events topics                 # what exists, and its head
loom events tail <topic>           # is the webhook even reaching us?
loom events subscriptions          # who is reading, and their lag
loom events status                 # exit 1 when anything is unhealthy
loom events dead [topic]           # what could not be processed
loom events replay --subscriber s --topic t --since 7d --max-events 1000 [--yes]
```

`status` exits non-zero on purpose: a status command that always succeeds can
only be read by a person, and the failure this subsystem exists to catch is the
one nobody is looking at.

`replay` is what `start_at=EARLIEST` is refused in favour of. It prints the plan
and needs `--yes`, the ceiling keeps the **newest** (as `max_catch_up` does for a
missed cron, so the plan and the rewind agree), and it does not dispatch — it
rewinds a checkpoint and lets the ordinary loop do the work, so a backfill goes
through the same admission, grants and dead-lettering as everything else. It is
safe by construction rather than by care: each re-read event re-derives its
original `{event_id}#{subscriber}` key, so everything already handled
deduplicates away and only newly-matching events start runs.

There is deliberately **no** `loom events install`. Registering a webhook with a
provider means owning N provider admin APIs forever for a once-per-deployment
act; provider registration stays in the host's deployment.

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

`loom.server.create_app(runtime)` (needs the `api` extra) serves
workflows, runs, journals, events, cancel, and replay. `LoomClient` is the async
client. Errors map to status codes: 403 authorization, 404 unknown workflow/run,
429 retryable admission rejection, 409 permanent one.

This is the realistic path to other languages — workflow authoring stays Python
because durability depends on re-entering a Python function body, but starting
runs and delivering approvals are ordinary HTTP requests.

### Scheduling and cron

`CronSchedule` (`triggers/cron.py`) is ~200 lines of stdlib — no croniter, no
APScheduler. It computes in the declared timezone and returns UTC, so DST is
handled where it has to be: the lost hour fires nothing and the repeated hour
fires **once** (`tests/test_cron_dst.py` pins one-per-local-hour-label, 23 on
the short day and 24 on the long one).

`TriggerDispatcher` (`runtime/dispatcher.py`) scans `Schedule`/`Interval`
triggers, persists a `TriggerRecord` through `TriggerStore`, and fires due ones
via `Runtime.submit()`. Cron therefore survives restarts by living in the store,
not in the process.

**Two identities, and everything here follows from them.**

`Fire.key` is `trigger_id@scheduled_for` — the *occurrence*, not the attempt —
and is passed as `submit(idempotency_key=…)`. `idempotency_key` is UNIQUE in
every persistent store, so two dispatchers racing, or one retrying after dying
between the submit and the advance, resolve to one run. A caller that loses that
race gets the winner's run back rather than an exception, so a correctly
deduplicated fire does not surface as a dispatcher error.

`_trigger_id` is a hash of the workflow plus **only the fields that decide when
it fires** (`kind`, `cron`, `timezone`, `seconds`). Registration runs on every
boot, so a fresh id per boot added a record per deploy — three deploys, three
runs per occurrence, from three ids no occurrence key could collapse. The
allowlist matters twice over: `describe()` also carries `next_fire`, a
*timestamp*, so hashing the whole description made the id depend on what time
the process booted. Policy (`catch_up`, `max_catch_up`, `jitter`) is excluded
deliberately — changing it updates the stored spec in place instead of orphaning
the trigger's `last_fire_at` and `run_count`.

Registration is therefore idempotent: a known trigger keeps its schedule state
(recomputing `next_fire_at` on boot would let a pod that restarts more often
than its schedule fires never fire at all), and triggers the workflow no longer
declares are retired, so changing a cron does not leave the old one running.

**Missed fires are a policy.** `catch_up=False` (default) fires the pending
occurrence and skips the rest, now counted and logged rather than silent.
`catch_up=True` replays each missed occurrence oldest-first under its own key —
so an interrupted backfill resumes without repeating — bounded by
`max_catch_up` (default 10), which keeps the newest and reports the drop. The
walk stops one past the ceiling, so a per-minute cron down for a week is not
enumerated in full just to discard almost all of it.

**Jitter is applied to dispatch, never to the schedule**, and is *derived* from
the occurrence key rather than sampled. A random draw would make two dispatchers
disagree about when an occurrence becomes eligible; deriving it means every
process computes the same delay with no coordination, and `Fire.key` stays
exactly the schedule's moment. Jitter reaching the key would silently disable
the exactly-once guarantee — an option for smoothing load undoing the
correctness property.

**Claiming is the other half, and it is about work, not correctness.**
`TriggerStore.claim_due_triggers(now, owner=…, lease_seconds=…)` takes the due
set rather than reading it, so two dispatchers stop building, submitting, and
advancing the same occurrence. Every backend implements it with its own
primitive — a mutex, `BEGIN IMMEDIATE`, `UPDATE … RETURNING … FOR UPDATE SKIP
LOCKED`, `find_one_and_update` — and `tests/conformance/test_trigger_claim.py`
is what says the four mean the same thing.

It is a **lease, not a lock**: a dispatcher that dies mid-tick delays one
occurrence instead of stranding the trigger forever. And `update_after_fire`
*releases* the claim as it advances — leaving that to expiry would make the
lease duration a silent lower bound on the schedule, so a per-minute cron under
a 60-second lease would fire once and then idle. Claim state lives inside
`TriggerRecord`, which every store already persists whole, so none of this
needed a migration on a deployed table.

`await rt.start_scheduler(dispatcher=…, elector=…)` puts cron on the same loop
and the same lease as due timers and orphan recovery. Leaving it outside was a
trap rather than an omission: a host that passed an elector reasonably believed
its scheduling was single-leader, and its timers were while its crons were not.

A store that predates `claim_due_triggers` falls back to `due_triggers` with a
debug line — a host with its own `TriggerStore` is not broken by a capability it
has not implemented, and without the claim the behaviour is exactly what shipped
before.

### Storage Backends

| Store | URL | Driver | Install |
|-------|-----|--------|---------|
| `MemoryStore` | `memory://` | in-process | default |
| `SQLiteStore` | `sqlite:///runs.db` | sqlite3 | default |
| `MongoStore` | `mongodb://…` | motor | `pip install loomflow[mongo]` |
| `PostgresStore` | `postgres://…` | asyncpg | `pip install loomflow[postgres]` |

All implement: `ExecutionStore + TriggerStore + CacheStore + LockProvider`.

**Workflows do not choose a store.** Where the journal lives is a deployment
decision — tests want memory, a laptop wants SQLite, production wants Postgres,
and the *same workflow code* must run against all three. So a workflow module
declares steps and workflows and nothing else; the host supplies the store:

```python
Runtime(store=PostgresStore(dsn))   # explicit
Runtime.from_env()                  # from $LOOM_STORE, defaults to memory://
from_url("sqlite:///runs.db")       # loom.stores.from_url
```

`CodeValidator` warns when a generated module constructs a store at import time.
Doing it inside `if __name__ == "__main__"` is fine — that block is a script, not
the library.

### Workflow Management Tools (new)

`agents/workflow_tools.py` provides 7 agent-facing tools: `list_workflows`, `get_workflow_info`, `run_workflow`, `schedule_workflow`, `list_runs`, `get_run_status`, `cancel_run`. These let a ReAct agent manage workflows via natural language.

### Pip Extras

```bash
pip install loomflow              # core
pip install loomflow[mongo]       # + MongoDB
pip install loomflow[postgres]    # + PostgreSQL
pip install loomflow[langchain]   # + LangChain/LangGraph
pip install loomflow[agno]        # + Agno
pip install loomflow[pydantic-ai] # + Pydantic AI
pip install loomflow[duckduckgo]  # + ddgs, for the duckduckgo toolset only
pip install loomflow[all]         # everything
```
