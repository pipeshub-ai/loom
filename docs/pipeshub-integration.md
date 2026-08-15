# PipesHub Workflow System — architecture, and the road to running on LOOM

**Status:** working document. **Audience:** whoever implements the merge.
**Sources:** `pipeshub-ai@backend/{python,nodejs}`, `pipeshub-ai@frontend`,
`~/opensource/workflow-sdk-design-v1.md`, and the `~/.cursor/plans/*workflow*`
series (≈380 KB across 18 plans).

---

## 0. Why this document exists

PipesHub has a working workflow system: an authoring agent, a durable runtime, a
sandbox, a capability broker, a REST surface, and a React studio. LOOM has a
library that overlaps it substantially and diverges from it in two decisive
places.

The goal is for PipesHub to `pip install workflow-builder` and delete its
parallel implementation. That cannot happen today, and the reason is not a long
tail of small gaps — it is two architectural facts (§8.1). Everything else
is work, not redesign.

This document is the shared map: what PipesHub does, how, and what LOOM must
become. It is meant to be edited as the merge proceeds.

---

## 1. Positioning — the shared bet

From the design draft (§1.2), and PipesHub is a direct implementation of it:

> Keep the operational substrate. Replace the JSON graph with code. Let an agent
> write the code. Project the code back to a graph so nobody loses the picture.

Stated as the pitch: **there is no workflow builder — no agent builder, no
drag-and-drop canvas — anywhere in PipesHub's workflow product.** A user
describes what they want in natural language. PipesHub's Coding Agent writes
it as real Python — `if`/`else`, loops, parallel branches, multiple
sub-agents, all of it — with the full catalog of toolsets, knowledge, skills,
and the agent builder itself exposed to it as one live, versioned capability
schema (an OpenAPI spec of PipesHub's own surface, ingested into LOOM's
toolset registry — see §9). Everything a workflow can call is reachable
directly in the sandbox it runs in; what it may actually *do* is controlled
entirely by the scoped token the run was issued, not by what the sandbox can
technically reach.

**Deterministic and probabilistic are two different node kinds, not two
styles of the same one.** For every node, the Coding Agent asks: can I write
a rule today that is right for every input the spec allows? Yes → a toolset
call and plain Python — journaled, free to re-run, no model in the loop at run
time. No, or unsure → a `ctx.agent()` node, because the alternative is a rule
that guesses (an invented keyword list, a threshold nobody supplied) wearing
the clothes of logic. That classification is reported, not left inside the
prompt (LOOM's `CodingResult.plan`), so it is checkable rather than assumed.
The agent an unclear node delegates to is itself pluggable — LOOM's built-in
loop, or PipesHub's own `agent_loop_lib`, or LangChain/Agno/Pydantic AI via
`AgentBackend` — and the generated workflow code does not change depending on
which one answers it (see the [coding agent guide](guides/coding-agent.md)).

The code is saved because it is the product: generate once, and every
subsequent run is free and deterministic, instead of re-asking a model to
improvise the same task. Debugging is reading a session's execution trace, not
stepping through a black box. And when a user needs to change a workflow, they
are — under the hood — editing the code; PipesHub's own agent turns that code
back into the React visualization the user sees, so there is never a diagram
that drifts from what actually runs (§9.2 below has the reconciliation with
LOOM's deterministic graph extraction, which this depends on).

The consequence that shapes every design decision below: **the code is written
by a model, so it is untrusted, and it must be verified before and while it
runs.** n8n's substrate is safe because a JSON graph cannot do anything the
engine did not intend. Code can.

LOOM's current design assumes the *author* is trusted — the workflow module is
imported into the host process. PipesHub assumes it is not. That single
difference produces most of the gap list.

---

## 2. System architecture

Three tiers, three languages, one workflow.

```
┌─ FRONTEND (Next.js / React) ─────────────────────────────────────────────┐
│  app/(main)/workflows/                                                    │
│    page.tsx · workflow-studio · workflow-detail-view · workflow-graph     │
│    workflow-editor (source) · run-inspector (trace) · triggers-panel      │
│    api.ts  ── typed client ──▶ Node REST                                  │
│  lib/hooks/use-workflow-run-updates.ts  ── live run updates               │
└───────────────────────────────────────────────────────────────────────────┘
                     │ HTTPS, session auth, org scoping
┌─ NODE BACKEND (Express, TypeScript) ─────────────────────────────────────┐
│  modules/workflows/                                                       │
│    routes/workflows.routes.ts          public API (19 endpoints)          │
│    routes/workflows-internal.routes.ts python → node callbacks (4)        │
│    controller/ · container/            auth, scopes, proxying             │
│  Owns: conversations, users, orgs, agents, connectors, sessions           │
└───────────────────────────────────────────────────────────────────────────┘
                     │ internal HTTP, service token
┌─ PYTHON BACKEND (FastAPI) ───────────────────────────────────────────────┐
│  api/routes/workflows.py               /api/v1/workflows/*                │
│  services/workflows/                   ~9.2k LOC, ports and adapters      │
│    application/  workflow_service · version_writer · journal_spill        │
│    runtime/      code_runner · sandbox · broker · agent_runner · replay   │
│    codegen/      agent · verifier · stub_generator · sdk_reference_tool   │
│    sdk/          context(Ctx) · decorators · triggers · _rpc              │
│    ir/           extractor  (code → graph, with source line spans)        │
│    security/     taint · sandbox_policy · scope_enforcer                  │
│    interface/    12 Protocol ports                                        │
│    adapters/     redis · graph(ArangoDB) · artifact · node                │
└───────────────────────────────────────────────────────────────────────────┘
                     │ JSON-lines over stdin/stdout
┌─ SANDBOX (subprocess) ───────────────────────────────────────────────────┐
│  sdk/_rpc.py harness → imports generated workflow → constructs Ctx        │
│  Every effect is an RPC to the host. No credentials in the child.         │
└───────────────────────────────────────────────────────────────────────────┘
```

### 2.1 The three artifacts a workflow has

| Artifact | Lives in | Written by |
|---|---|---|
| **Source** (Python) | `ICodeStore`, content-addressed | codegen agent, or a human edit |
| **Version** (immutable) | `IWorkflowVersionStore` | `commit_version`, activated by `activate_version` |
| **Run** (journal) | `IExecutionJournal` (Redis) + payload spill | the runtime |

Loom collapses the first two into "the file on disk is the source of truth" and
records only a `code_hash` in its catalog. That works for a Git-based developer
workflow and not for a UI where a business user edits and rolls back.

---

## 3. HLD — planes and the request path

### 3.1 Planes

```
AUTHORING                CONTROL                    EXECUTION           STORAGE
─────────                ───────                    ─────────           ───────
codegen agent      ─┐    workflow_service           code_runner         Redis journal
 stub_generator     ├──▶ trigger registry     ──▶   sandbox (subproc)   ArangoDB (graph)
 verifier           │    scheduler                  broker client       artifact store
 sdk_reference_tool │    admission                  agent_runner        trace sink
 IR extractor      ─┘    PlatformBroker  ◀──────────┘                   conversation (Node)
                         scope_enforcer / taint
```

### 3.2 Request path — a triggered run

```
trigger fires (cron | interval | event | webhook | manual)
  → workflow_service.run_now / scheduler
  → resolve TaskDefinition → compute RunGrant  (host-side, never from sandbox)
  → load pinned source from ICodeStore (by active version)
  → provision sandbox session (temp dir, staged source + harness)
  → subprocess: harness imports source, builds Ctx over _RpcJournal/_RpcBroker
  → for each durable operation:
        Ctx._journal_or_replay(step_key)
          ├ journal hit  → return recorded result           (replay)
          └ journal miss → BrokerCall over stdin
                → host: scope_enforcer authorises against RunGrant
                → capability handler executes (tool | agent | search | state | emit)
                → journal.append(entry)  → result back over stdout
  → harness writes {"type":"done"} → host records terminal status
  → trace sink emits; conversation writer appends if conversation-bound
```

The property that matters: **the sandbox holds no credentials and no authority.**
It can ask; the host decides. A generated workflow that tries to reach a tool it
was not granted gets a denial, not a token.

### 3.3 Execution modes

| Mode | Path | Purpose |
|---|---|---|
| `run_now` | full path above | normal execution |
| `dry_run` | `RunPrincipal.is_dry_run=True` | broker refuses every write-tagged tool; reads proceed |
| replay | journal-first | resume after crash; write steps must hit the journal or raise `ReplayDivergence` |
| `answer_run` | resumes a parked run | human-in-the-loop reply |

---

## 4. LLD — the modules that matter

### 4.1 `sdk/context.py` — the `Ctx` surface (564 LOC)

The API generated code is written against. Every method is journaled.

| Method | Journal kind | Notes |
|---|---|---|
| `now()` `random()` `uuid()` | `clock` `random` `uuid` | determinism seams |
| `tool(name, **kwargs)` | `tool` | by **name**, resolved host-side |
| `agent(id)` → `.run(goal=)` | `agent` | invoke a configured agent |
| `create_agent(...)` | `agent` | requires `grant.can_create_agents` |
| `search(...)` | `knowledge` | knowledge-base retrieval |
| `state.get/set(key)` | `state` | **durable across runs of the same workflow** |
| `emit(message, kind=)` | `emit` | streams into the bound conversation |
| `map(fn, items)` | — | bounded fan-out |
| `sleep(s)` `wait_for_event(t)` `request_approval(label)` | `sleep` `wait` `approval` | suspension |
| `log(...)` / `logs` | — | in-run diagnostics |

### 4.2 `interface/` — 12 ports

`IExecutionJournal` · `IJournalPayloadStore` · `ICodeStore` ·
`IWorkflowVersionStore` · `IWorkflowStateStore` · `IConversationWriter` ·
`ISandboxSessionProvisioner` · `IPlatformBroker` + `ICapabilityHandler` ·
`IWorkflowAgentRunner` · `IAgentProvisioning` · `ITraceSink`.

This is a clean hexagonal boundary and is the reason a LOOM substitution is
plausible at all: **LOOM can be introduced as adapters behind these ports**
rather than as a rewrite (§10, P1).

### 4.3 `runtime/broker.py` (639 LOC) + `security/`

`Capability` ∈ {`tool`, `agent.run`, `agent.create`, `knowledge.search`,
`state.get`, `state.set`, `conversation.emit`}.

`RunGrant` is **deny-by-default**:

```python
tool_names: frozenset[str]        # empty = nothing, never "everything"
agent_ids: frozenset[str]
collection_ids: frozenset[str]    # narrows rather than denies
can_create_agents: bool = False
max_calls: int = 200              # runaway-loop ceiling
```

`RunPrincipal` carries `org_id`, `user_id`, `run_id`, `workflow_id`,
`is_dry_run`, `is_service_account`, `conversation_id`, `grant`. Taint tracking
(`security/taint.py`) marks data that came from untrusted sources so it cannot
flow into privileged sinks.

### 4.4 `codegen/` — the authoring pipeline

`agent.py` (ReAct) → `verifier.py` (649 LOC, the checks) → `stub_generator.py`
(typed stubs from toolset manifests) → `sdk_reference_tool.py` (tiered
disclosure of the SDK to the model). `ir/extractor.py` projects code back to a
graph, **carrying `source_start`/`source_end` per node** so the canvas can jump
to the line.

### 4.5 `domain/models.py` — the shared vocabulary

`Workflow` (org, kind, version, triggers, subscriptions, status, scopes,
tool_names, connector_ids, collection_ids, budgets) · `JournalEntry` (12 entry
kinds) · `TraceEntry` (UI-shaped) · `EventSubscription` + `FilterPredicate`
(namespaced events with filters) · `IRNode`/`IRNodeKind` · `ResultRef`
(inline-or-artifact spill).

---

## 5. Data flows

### 5.1 Authoring (chat → live workflow)

```
user describes intent in chat
  → codegen agent: discover toolsets (tiered) → generate → verify → repair
  → commit_version(source)         [ICodeStore + IWorkflowVersionStore]
  → IR extraction → graph for the canvas
  → activate_version               [workflow now runnable]
  → conversation_writer links the workflow to the conversation
```

### 5.2 Execution — see §3.2.

### 5.3 Human-in-the-loop

```
ctx.request_approval("refund")
  → journal entry kind=approval, run parked, worker released
  → UI shows the approval; user answers
  → POST /:workflowId/runs/:runId/answer
  → resume: journal serves every prior step; approval entry now has a value
```

### 5.4 Conversation streaming

```
ctx.emit("working on it…")
  → broker capability conversation.emit
  → POST /internal/conversations/:id/emit   [Node]
  → websocket → chat UI
```
Requires `RunPrincipal.conversation_id`; without it the messages are dropped.

### 5.5 Versioning

`commit_version` → immutable version + source in code store →
`activate_version` flips the pointer → runs pin the version they started with,
so an activation mid-run does not change the code under a running workflow.

---

## 6. REST API reference

### 6.1 Node — public (`/api/v1/workflows`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | list workflows |
| GET | `/:workflowId` | one workflow |
| GET | `/:workflowId/triggers` | triggers |
| GET | `/:workflowId/runs` | run history |
| GET | `/:workflowId/runs/:runId` | one run |
| GET | `/:workflowId/runs/:runId/trace` | trace for the inspector |
| POST | `/:workflowId/runs/:runId/answer` | answer an approval / event |
| GET | `/:workflowId/versions` | version list |
| GET | `/:workflowId/versions/:versionId/source` | source of a version |
| POST | `/:workflowId/versions/commit` | commit a new version |
| POST | `/:workflowId/versions/:versionId/activate` | activate |
| POST | `/:workflowId/run-now` | execute |
| POST | `/:workflowId/dry-run` | execute with writes refused |
| POST | `/:workflowId/pause` · `/resume` · `/cancel` | lifecycle |
| DELETE | `/:workflowId` | delete |
| POST | `/:workflowId/promote-to-agent` | turn a workflow into an agent |
| POST | `/:workflowId/edit` | agent-assisted edit |

### 6.2 Node — internal (python → node)

`POST /internal/conversations/:id/messages` · `POST /internal/conversations/:id/emit` ·
`PATCH /internal/conversations/:id/workflows` · `GET /internal/conversations/:id/workflows`

### 6.3 Python — FastAPI (`/api/v1/workflows`)

Mirrors the public set, guarded by
`require_scopes(OAuthScopes.WORKFLOW_{READ,WRITE,EXECUTE})`.

### 6.4 LOOM today

`workflow_builder.server.create_app()` serves workflows, runs, journals, events,
cancel, retry, replay. **Missing for parity:** versions, source, commit,
activate, dry-run, trace projection, triggers CRUD, promote-to-agent, edit, and
any notion of `org_id`.

---

## 7. What LOOM already has

Not a small list, and it is why this merge is worth doing rather than starting
over.

- Journal-based deterministic replay; `Suspend`; retry/replay distinction
- `@workflow` / `@step` / `@pure` / `@effect` / `@node`; `Retry`, `OnError`
- `Context`: `step` `agent` `sleep` `wait_for_event` `wait_for_approval`
  `gather` `map` `batched` `now` `uuid4` `random` `emit` `signal` `checkpoint`
  `compensate` `continue_as_new` `put_artifact`/`get_artifact` `span` `nested`
- Stores: Memory · SQLite · Mongo · Postgres, all `ExecutionStore + TriggerStore
  + CacheStore + LockProvider`
- Triggers: `Schedule` `Interval` `Webhook` `OnEvent` `Poll` `Manual` `Chat`
  `EmailInbox` `Form` + `TriggerDispatcher`
- Production layer: `AdmissionController` `FlowControlPolicy` `Role` RBAC
  `LeaderElector` `RetentionManager` `GrantSet` leases + orphan recovery
- Toolsets: manifests, three-tier lazy disclosure, `resolves` markers, generated
  fakes, Gmail · Calendar · Jira · Confluence
- Coding agent: ReAct, 7-stage verification pipeline (compile · static · lint ·
  types · smoke · replay · critique), entity resolution, repair loop
- Surfaces: CLI (19 commands, exit-code contract), TUI, MCP server (10 tools),
  HTTP API + client
- Graph extraction and commit-time narration

---

## 8. Gap analysis

Ordered by how much they block the merge.

| # | Gap | PipesHub has | LOOM has | Severity |
|---|---|---|---|---|
| 1 | **Out-of-process execution** | subprocess sandbox + RPC | in-process import | **blocker** |
| 2 | **Capability broker** | 7 capabilities checked *per call*, deny-by-default `RunGrant`, `max_calls`, dry-run, taint | `GrantSet{toolsets, agents, resources, subflows, egress, budget}` checked at resolution | **blocker** |
| 3 | Multi-tenancy | `org_id` on every record and query | none | high |
| 4 | Code + version store | `ICodeStore`, `IWorkflowVersionStore`, commit/activate | `code_hash` in catalog | high |
| 5 | Workflow-scoped state | `ctx.state` durable across runs | artifacts only (immutable, versioned) | high |
| 6 | Conversation streaming | `ctx.emit` → chat | `ctx.emit` = pub/sub broadcast (different semantics) | high |
| 7 | Dry run | broker refuses writes | none | medium |
| 8 | Event subscriptions | namespaced types + `FilterPredicate` | `OnEvent(name)` only | medium |
| 9 | Trace projection | `TraceEntry` shaped for a UI | journal + `Tracer` | medium |
| 10 | Agent provisioning | `create_agent` | none | medium |
| 11 | Knowledge search | `knowledge.search` capability | toolset-level only | medium |
| 12 | Source spans in graph | `IRNode.source_start/end` | node list without spans | low |
| 13 | REST parity | §6.1 | §6.4 | low (mechanical) |

### 8.1 The two blockers, stated precisely

**8.1.1 Execution model.** LOOM's `Runtime.run()` imports the workflow module and
calls it. Generated code therefore runs with the host's credentials, imports,
and network. For PipesHub that is not acceptable: the code was written by a
model from a business user's sentence.

The fix is not to sandbox LOOM's runtime wholesale — it is to make the boundary
*pluggable*, exactly as the store already is. LOOM needs an `ExecutionBackend`
port with `InProcessBackend` (today's behaviour, right for a developer's laptop
and for tests) and `SubprocessBackend` / `ContainerBackend` beside it. PipesHub's
`ISandboxSessionProvisioner` is the proof this factors cleanly.

**8.1.2 Authority.** `GrantSet` is not empty — it covers toolsets, agents,
resources, subflows, egress, and budget. The differences are *where* and *when*:

- **Granularity.** `GrantSet` names a toolset; `RunGrant` pins individual tool
  names, agent ids, and collection ids.
- **Enforcement point.** `GrantSet` is consulted when tools are *resolved* for
  `ctx.agent()`. The broker is consulted on *every call*, so code that obtained
  a reference cannot reuse it past its grant.
- **Missing entirely:** `max_calls`, dry-run refusal, and taint propagation.

LOOM needs the capability layer to sit *under* `ctx.*` rather than beside it, so
a call cannot bypass it — and `GrantSet` should become the ergonomic way to
*declare* a grant that the broker then enforces per call.

These two are one design: **effects go through a broker; the broker is where the
process boundary and the authority check both live.**

---

## 9. Who builds what

The earlier draft of this section had LOOM absorbing conversation streaming,
knowledge search, and agent provisioning. That was wrong: those are PipesHub's
domain, and pulling them into a general-purpose library would make it worse for
every other host.

The split follows the boundary rule in the
[implementation plan](implementation-plan.md) §2 — **LOOM ships the port and a
reference adapter that needs no infrastructure; PipesHub ships the adapter that
knows about PipesHub.**

| PipesHub capability | LOOM provides | PipesHub provides |
|---|---|---|
| Sandbox execution | `ExecutionBackend` + `SubprocessBackend` | container/E2B backend if it wants stronger isolation |
| Capability broker | `EffectBroker` + `GuardedBroker` + `Grant` | the policy that computes a grant from a `TaskDefinition` |
| Capability catalog for the coding agent | toolset registry, resolution, effect classes, `ToolsetGenerator.from_openapi()` (Phase 3) | its own OpenAPI spec covering toolsets, knowledge, skills, and the agent builder |
| `ctx.tool(name)` | toolset registry, resolution, effect classes | its own toolset manifests |
| `ctx.state` | `StateStore` port + reference adapter | Redis adapter |
| `ctx.emit` → chat | `RunStream` port + `ctx.report` | conversation adapter (Node callback) |
| `ctx.search` | nothing — it is a toolset | knowledge toolset |
| `create_agent` | nothing — `AgentBackend` runs agents | agent registry and provisioning |
| Tenancy | opaque `partition` filtered everywhere | maps `org_id` → partition |
| Journal | `ExecutionStore` protocol + 4 adapters | Redis adapter |
| Versions / source | `SourceStore`, `VersionStore` ports | ArangoDB + artifact adapters |
| Trace for the UI | `TraceView` projection from the journal, session-shaped | rendering |
| Graph / visualization | deterministic WGIR extraction (source of truth for structure) | agent that renders WGIR + source as a React app; see §9.2 |

**On the capability catalog row:** PipesHub's "everything available to the agent
directly in the sandbox" is the OpenAPI spec, not raw access — the spec is what
gets ingested (via the planned `ToolsetGenerator.from_openapi()`, Phase 3) into
the same manifest-based registry LOOM's coding agent already reads via
three-tier lazy disclosure (index card → op table → full schema on demand).
Two systems, one interchange format: PipesHub authors and publishes the spec,
LOOM turns it into the catalog the model actually sees. What the sandbox can
*technically* reach and what a run's scoped token *authorizes* stay separate
concerns — the former is the catalog above, the latter is the broker row above
it.

### 9.1 Migration order

LOOM's phases are in the implementation plan. PipesHub's side, which can start
as soon as the matching LOOM phase lands:

| When | PipesHub does |
|---|---|
| after LOOM P0–P1 | implement `IExecutionJournal` over `ExecutionStore`; map `org_id` → `partition`. Nothing else changes. |
| after P2 | compute a `Grant` from `TaskDefinition`; delete `scope_enforcer` in favour of `GuardedBroker` |
| after P3 | replace `runtime/sandbox.py` + `sdk/_rpc.py` with `SubprocessBackend` |
| after P4 | `ctx.state` and `ctx.report` adapters; retire PipesHub's `Ctx` |
| after P5 | source/version/trace adapters; studio reads LOOM's IR |
| cutover | delete `services/workflows/{sdk,runtime,codegen,ir}` (~7k LOC) |

Each row leaves PipesHub working. No row requires the next one.

### 9.2 Visualization: agent-rendered, structurally verified

The product requirement is that a user edits a workflow by editing its code,
and the React visualization they see is regenerated straight from the file —
no separate diagram to keep in sync, and editing *is* re-rendering. The
naive version of that — an agent reads the code file and freehand-generates a
React app — throws away the one guarantee [Phase 4](../phases/phase-4-visualization.md)
was built to provide: **the model cannot invent or hide a step**, because
today's canvas is projected from WGIR, a deterministic extraction (registry +
AST + symbolic passes), not from a model's reading of the file.

The two are not actually in tension; they answer different questions:

- **WGIR extraction answers "what does this code do," deterministically.** No
  model in the loop, so it cannot be wrong about the structure — every
  `@step`, `if`, `for`, `ctx.agent()` call is a node because the extractor
  found it in the AST, not because a model decided to mention it.
- **The rendering agent answers "how should this look."** Layout, grouping,
  labeling, which details to surface — this is exactly the kind of judgment
  call a model is good at and a deterministic pass is bad at.

So the pipeline PipesHub runs is: **extract WGIR from the code (as today) →
agent renders WGIR + source into a React component → a verifier diffs the
rendered graph's node/edge set against WGIR and rejects a render that adds,
drops, or mislabels a node.** The user sees the agent's rendering; the
rendering is checked against the same skeleton-integrity guarantee the
narration step already uses (`Explainer._verify_completeness` in Phase 4 is
the existing precedent — the verifier here is the same idea applied to a
render instead of a paragraph). A render that fails verification is a bug to
fix in the rendering prompt, not a diagram a user should ever see.

This is why `phases/phase-4-visualization.md` gets a `GraphPatch`-adjacent
addition rather than a replacement: WGIR stays the structural source of truth
that both the narration and the render are checked against; only the
*rendering surface* moves from a fixed canvas layout to an agent-generated
React app. See that document's HLD for where the render + verify step slots
into the existing pipeline.

## 10. Testing strategy

The merge is a behaviour-preservation exercise, so the tests are the plan.

| Level | What | Where |
|---|---|---|
| **Conformance** | identical journals for identical specs, PipesHub vs LOOM | P0, runs every phase |
| **Unit** | each new port and adapter in isolation | LOOM `tests/` |
| **Property** | replay determinism: run → replay → compare (Hypothesis) | LOOM |
| **Security** | sandbox escape attempts; grant denial for every capability; dry-run refuses every write-tagged tool; taint into a privileged sink | LOOM + PipesHub |
| **Crash** | kill the worker at every step index; assert no duplicate effects | LOOM |
| **Tenancy** | org A cannot see, run, cancel, or replay org B, through API/CLI/MCP | PipesHub |
| **Integration** | Python → Node internal callbacks; conversation emit reaches the socket | PipesHub |
| **E2E** | chat → generate → commit → activate → trigger → run → approve → trace, in the browser | `frontend/tests/e2e/workflows` |
| **Docs** | every example in both repos executes (LOOM's `docs-examples` CI job) | CI |

Two rules learned the hard way in LOOM and worth carrying over:

1. **A check that cannot run has found nothing.** Skipped ≠ passed; report it.
2. **Verify by executing, not by asserting.** A stale API resolves, compiles,
   and fails only when run.

---

## 11. Risks

| Risk | Why it bites | Mitigation |
|---|---|---|
| Two `emit`s with different meanings | silent wrong behaviour after the merge | rename one in P4, before any code depends on the ambiguity |
| Sandbox is not real isolation | PipesHub's own docstring says the subprocess reaches the network and runs as the service user | treat `SubprocessBackend` as trusted-tenant; ship a container backend behind the same port before multi-tenant untrusted code |
| Journal format divergence | replay breaks across the cutover | conformance suite (P0) compares journals, not just outputs |
| Grant computation is host-side | if a pin cannot be computed the run must fail, not run unpinned | PipesHub already does this — deny-by-default; preserve it verbatim |
| Scope creep into OAuth | design draft §2.2 puts token minting out of scope | keep `Connection` a read-only reference |
| Rendering agent drifts from WGIR | a user trusts a diagram that silently hides or invents a step (§9.2) | verifier diffs the rendered graph against WGIR before a render is served; a mismatch blocks the render, not just logs it |
| "Session" means two things | LOOM already uses *session* for agent conversation memory (`agent_id:session_id`); PipesHub's debugging trace is a session per run | name the run-level trace something else (e.g. *run trace*) before both ship — same lesson as the `emit`/`publish`/`report` split in P4 |

---

## 12. Open questions

1. **Does LOOM keep in-process execution as a first-class mode?** Recommended
   yes — it is what makes `pytest` and a laptop pleasant, and it is the mode the
   coding agent's smoke stage uses.
2. **Where does the toolset gateway live** once both projects have one — LOOM's
   registry, PipesHub's broker, or LOOM's broker with PipesHub adapters?
3. ~~**Who owns the canvas?**~~ **Resolved (§9.2):** LOOM emits WGIR as the
   structural contract; PipesHub's rendering agent generates the React app from
   WGIR + source and a verifier checks the render against WGIR before it is
   served. LOOM owns the guarantee, PipesHub owns the rendering and the pixels.
4. **Agent runtime**: PipesHub has `agent_loop_lib`; LOOM has `AgentBackend`.
   One of them should wrap the other rather than both existing.
5. **OpenAPI ingestion fidelity**: `ToolsetGenerator.from_openapi()` (Phase 3,
   not started) is the one path from PipesHub's capability spec to the
   registry the coding agent reads. It needs to round-trip PipesHub's
   `resolves` markers, effect classes (read/write), and pagination shape
   (`Results[T]`) — not just operation names and JSON schemas — or the coding
   agent loses entity resolution and paging safety for anything registered
   this way instead of hand-written.

---

## Appendix — file map

| Concern | PipesHub | LOOM |
|---|---|---|
| Context API | `sdk/context.py` | `runtime/context.py` |
| Decorators | `sdk/decorators.py` | `runtime/workflow.py`, `steps/definition.py` |
| Triggers | `sdk/triggers.py` | `triggers/specs.py` |
| Journal | `interface/journal.py`, `adapters/redis/journal.py` | `runtime/journal.py`, `state/` |
| Sandbox | `runtime/sandbox.py`, `sdk/_rpc.py` | — (P2) |
| Broker | `runtime/broker.py`, `interface/broker.py` | — (P3) |
| Authority | `security/{taint,scope_enforcer,sandbox_policy}.py` | `security/grants.py` |
| Codegen | `codegen/{agent,verifier,stub_generator}.py` | `agents/{coding_agent,stages,checks}.py` |
| Graph IR | `ir/extractor.py` | `graph/` |
| REST | `api/routes/workflows.py` | `server/app.py` |
| Domain | `domain/models.py` | `core/models.py` |
