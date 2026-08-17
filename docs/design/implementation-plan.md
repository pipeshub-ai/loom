# LOOM implementation plan

<!-- docs-illustrative -->

**What this is:** the work LOOM does to become a substrate a platform can build
on, without becoming that platform.

**Companion:** [PipesHub integration](pipeshub-integration.md) analyses one such
platform. This document is deliberately written without reference to it: every
item below is justified by LOOM's own contract, and would be worth building if
PipesHub did not exist.

---

## 1. What LOOM is

A **library**. `pip install loomflow`, import `workflow` and `step`, run
a durable workflow. Everything else — CLI, MCP server, TUI, HTTP API — is a
convenience surface over the same object, and none of them is required.

That ordering is the constraint the whole plan answers to:

- A feature lands in the **library** first, as a port or a primitive.
- The **surfaces** expose it because they share one facade, not because each was
  taught about it separately.
- **Infrastructure** — a particular database, queue, chat product, or identity
  system — stays outside, behind a port, with one reference adapter that needs
  no infrastructure at all.

## 2. The boundary rule

> LOOM ships the **port** and the **reference adapter that requires nothing**.
> A host ships the adapter that knows about its world.

Applied:

| Concern | LOOM ships | Host ships |
|---|---|---|
| Journal storage | protocol + Memory, SQLite, Postgres, Mongo | Redis, DynamoDB, … |
| Execution location | protocol + in-process, subprocess | container, microVM, remote pool |
| Effect authority | protocol + grant enforcement | policy source, identity |
| Run output | protocol + in-memory observer | chat UI, websocket, log pipeline |
| Workflow source | protocol + filesystem, store-backed | object store, Git host |

The test for whether something belongs in LOOM: **could a second, unrelated host
use it unchanged?** A grant model, yes. A knowledge-base search capability, no —
that is a toolset, and LOOM already has a toolset mechanism for it.

### 2.1 Explicitly out of scope

Named so they stop being re-proposed:

- **Conversation / chat semantics.** LOOM emits run output; what a conversation
  *is* belongs to the host.
- **Agent provisioning** (creating and persisting agent configurations). LOOM
  runs an agent through `AgentBackend`; it does not own an agent registry.
- **Domain capabilities** — knowledge search, CRM lookup, anything with business
  meaning. These are toolsets.
- **Identity, OAuth, token refresh.** LOOM accepts a reference and never mints.
- **A workflow-building UI.** `loom ui` stays an operator's terminal view. A
  studio is a product.

---

## 3. Architecture additions

Five ports and two primitives. Each is small, and each removes a reason someone
would otherwise fork the runtime.

<!-- docs-preamble -->

The protocol sketches below are real: they resolve against these names, which is
how a design stays honest about what it is proposing.

```python
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from loom.runtime.workflow import WorkflowDefinition
from loom.security.grants import GrantSet as Grant


@dataclass(frozen=True)
class Authority:
    """What a run may do. See §3.7."""

    grant: Grant = field(default_factory=Grant)
    dry_run: bool = False


@dataclass(frozen=True)
class ExecutionRequest:
    """Input, authority, and journal handle for one execution."""

    workflow_input: Any = None
    authority: Authority | None = None


@dataclass(frozen=True)
class ExecutionOutcome:
    """Terminal state, or a suspension to be resumed."""

    status: str = "completed"
    output: Any = None


@dataclass(frozen=True)
class EffectCall:
    """One durable operation a workflow body asked for."""

    kind: str
    target: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EffectResult:
    ok: bool = True
    value: Any = None
    error: str | None = None
```

### 3.1 `ExecutionSandbox` — where workflow code runs

```python
@runtime_checkable
class ExecutionSandbox(Protocol):
    """Runs a workflow body, and proxies its durable calls back."""

    name: str
    enforces: frozenset[str]

    async def run(
        self,
        *,
        body: SandboxBody,
        run_id: str,
        input: Any,
        channel: ContextChannel,
        policy: SandboxPolicy,
    ) -> SandboxOutcome: ...
```

- `InlineSandbox` — today's default, in-process, enforces nothing. What tests
  and a laptop should keep using.
- `SubprocessSandbox` — launches a harness, speaks JSON-lines over stdin/stdout,
  applies `setrlimit` where the platform honours it.
- `DockerSandbox` — the same harness and conversation loop inside
  `docker run --rm -i --network none`, with cgroup memory, a read-only root,
  and `--cap-drop ALL`. A host that needs real isolation constructs this
  instead of a subprocess.

Why it is a port rather than a flag: isolation is a spectrum (none → process →
container → microVM → remote), the right point differs per deployment, and the
library ships the first three. "Sandbox" rather than "Backend" — that name
already belongs to `AgentBackend` and `DurabilityBackend`, and this port is an
isolation boundary, not a durability one.

### 3.2 `EffectBroker` — how effects are mediated

Every durable operation already funnels through `Context`. Today it calls
straight through. The broker interposes:

```python
class EffectBroker(Protocol):
    async def dispatch(self, call: EffectCall, authority: Authority) -> EffectResult: ...
```

- `DirectBroker` — default, no checks. Identical behaviour to today.
- `GuardedBroker` — enforces a `Grant` per call, counts against a ceiling,
  refuses writes in a dry run.

This is the seam that makes untrusted code safe *and* the seam a subprocess
speaks across. One mechanism, two payoffs.

### 3.3 `Grant` — authority, per call

`GrantSet` today declares which toolsets, agents, resources, subflows, egress,
and budget a workflow may use, checked when tools are resolved. It grows:

- pins at **operation** granularity, not just toolset
- a **call ceiling** (`max_calls`) so a runaway loop stops
- **dry run**: write-classed effects refused, reads proceed
- checked on **every dispatch**, so a held reference cannot outlive its grant

`GrantSet` stays the ergonomic way to *declare*; `Grant` is what the broker
*enforces*. Deny-by-default: an ungrantable run fails its first effect with a
message naming the missing grant, rather than running unpinned.

### 3.4 `StateStore` — durable workflow-scoped state

`ctx.state` — a KV that survives across runs of the same workflow, keyed by
`(workflow, key)`. Distinct from artifacts, which are immutable and versioned;
this is mutable and current. Reference adapter: the existing `ExecutionStore`.

### 3.5 `RunStream` — where run output goes

A workflow that runs for four minutes should be able to say what it is doing.

```python
class RunStream(Protocol):
    async def report(self, run_id: str, message: str, *, kind: str = "text") -> None: ...
```

Reference adapter buffers in memory and is readable from the facade, so `loom
watch` and the MCP server show progress with no host involvement.

### 3.6 Naming decision: `emit` is currently two things

`ctx.emit(name, payload)` today broadcasts an **event** to waiters. "Emit" is
also the natural word for **streaming output**. Shipping both under one name is
the kind of ambiguity that produces silently wrong behaviour, so:

- events → **`ctx.publish(name, payload)`**; `emit` becomes a deprecated alias,
  removed in the next minor.
- run output → **`ctx.report(message)`**.

Neither name is ever ambiguous, and no host has to guess which one it got.

### 3.7 `Authority` — what a run may do

```python
@dataclass(frozen=True)
class Authority:
    grant: Grant = ...       # which toolsets, agents, subflows may be invoked
    dry_run: bool = False    # reads proceed, writes are refused
```

Deliberately **not** an identity. LOOM does not authenticate, does not know who
is asking, and does not partition — it knows only what the run in front of it
may invoke. An earlier draft called this `Principal` and gave it `partition` and
`subject`; only the permission half was ever enforced, and a name that promises
an identity the library does not have is a name that invites someone to rely on
it. See P1.

---

## 4. Phases

Each ships independently and leaves `main` releasable. Exit criteria are tests.

**Status.** P0, P2, and P4 are implemented, with their exit criteria as tests
in `tests/test_surface_parity.py`, `tests/test_effect_broker.py`, and
`tests/test_state_and_stream.py`. **P1 was built and then removed** — see below.
P3, P5, and P6 are not started. See [grants and progress](../guides/grants.md) for
the shipped surface.

### P0 — One path to the runtime (1 week)

**Done.**

The CLI and MCP server share `RuntimeFacade`. **The HTTP server does not** — it
drives `Runtime` directly, so every capability below would have to be added to
it a second time.

- Rewrite `server/app.py` over `RuntimeFacade`.
- Extend the facade with what REST already exposes but the protocol omits.

**Exit:** `grep -c facade server/app.py` is non-zero; a capability added to the
facade appears in CLI, MCP, and REST without touching three files; existing HTTP
tests unchanged.

*Delivered.* The rewrite surfaced two drifts the duplication had been hiding: a
journal keyed `name` over HTTP and `step_id` everywhere else, now one
`describe_entry`; and `tags`/`metadata` silently dropped on the async start
path. `input_schema` also carried two meanings — `None` for "not derivable" was
being flattened to `{}` by the route — and the wire now keeps them apart.

### P1 — Tenancy — **dropped**

Built, then removed at the maintainer's direction: LOOM is not multi-tenant, and
a partition key threaded through every store query, facade method, and CLI
command is a large permanent tax on a single-tenant library.

What it would have cost is worth recording, because it is the reason not to add
it speculatively. Partitioning is not a filter you can bolt on at the edge:
`list_executions` applies `limit` at the source, so filtering the *result* loses
your own rows to a busier neighbour's, which means every store — four of them —
has to know. Idempotency keys have to be unique per tenant rather than globally,
which meant a table rebuild in SQLite and dropping a constraint in Postgres.
And every write that returns nothing — cancel, event delivery — needs its own
ownership check, because "it returned nothing" is not the same as "it did
nothing."

A host that needs tenancy runs a Runtime and a store per tenant, which needs
nothing from LOOM. If that stops being enough, this is the shape it takes.

**What survived.** `Principal` was carrying two unrelated things: identity
(partition, subject) and permission (grant, dry run). Only the second half was
ever enforced, so it became `Authority` — a name that does not promise an
identity LOOM does not have. P2 uses it.

### P2 — `EffectBroker` and `Grant` (3 weeks)

**Done.**

- `EffectCall` / `EffectResult` / `EffectBroker`; `DirectBroker` default.
- `GuardedBroker`: per-call grant check, `max_calls`, dry-run.
- `Context` routes durable operations through the broker.

**Exit:** a run granted one operation cannot invoke a sibling operation of the
same toolset; the denial names the missing grant. A dry run performs every read
and no write. `max_calls` halts a loop. **`DirectBroker` benchmarks within 5% of
today** — the seam must not tax the common case.

*Delivered.* Dispatch is **once per durable operation**, not once per attempt: a
retry is the retry policy's business, and a denial is not retryable. Resolved
tools are stamped with their toolset, operation, and effect class, which is what
lets a grant name a sibling operation of a tool an agent is already holding.

The benchmark had to change shape. Timing whole runs put a dataclass and an
await underneath a journal write and a store round trip, and reported a 57%
regression and a 6% improvement from identical code on consecutive attempts. It
now measures `dispatch` against calling the same coroutine directly: **~2µs per
call**, against a step doing real I/O — a fraction of one percent, and a bound
that still catches a broker that grew a lock or an allocation.

### P3 — `ExecutionSandbox` (4 weeks)

**Done.** Ships `InlineSandbox` (default), `SubprocessSandbox`, and
`DockerSandbox`. The two isolating adapters share one child harness
(`sandboxes/_harness.py`) and one JSON-lines conversation loop
(`sandboxes/_conversation.py`); a host extends the child's vocabulary with
`ctx_shims=` rather than forking the script. `Runtime(sandbox=...)` is the
constructor argument; `enforces` is honest per platform (macOS refuses
`RLIMIT_AS`, a container does not).

**Exit:** a workflow that reads the filesystem or opens a socket fails under
`DockerSandbox` and succeeds under `InlineSandbox`, with identical journals
otherwise. Kill the child at each step index → resumes with no duplicate
effects. A subprocess is not a container, and `enforces` says so.

### P4 — `StateStore` and `RunStream` (2 weeks)

**Done.**

- `ctx.state` over a `StateStore` port, keyed by `(workflow, key)`;
  store-backed reference adapter.
- `ctx.report` over `RunStream`; in-memory reference adapter surfaced through
  the facade, so `loom watch` and MCP show progress.
- `ctx.publish` rename with a deprecation shim.

**Exit:** state survives across runs of one workflow and is unreadable from
runs of another workflow. `loom watch` shows a long-running workflow's progress
as it happens. `ctx.emit` warns once and still works.

*Delivered.* Neither is journaled, and the consequences are documented rather
than papered over: state reads are live, so a workflow branching on state does
not replay identically; and a replay really does report again, under the
replay's own run id, because a replay is a real execution and a watcher should
see it move. The `emit:` journal prefix is kept under the new name so runs
already in flight stay readable to the code that has to finish them.

### P5 — Source, versions, and the operator surface (3 weeks)

**Not started.**

- `SourceStore` + `VersionStore` ports; filesystem and store-backed adapters.
- Facade: `commit`, `activate`, `versions`, `source`, `dry_run`, trigger CRUD.
- Journal → `TraceView` projection: an ordered, UI-shaped account of a run.
- Source spans (`line_start` / `line_end`) on graph nodes.

**Exit:** a workflow can be committed, activated, rolled back, and dry-run
through CLI, MCP, and REST identically. A run's trace renders without reading
the journal's internal shape. A graph node points at the line that produced it.

### P6 — Hardening (2 weeks)

**Not started.**

- Chaos: kill workers and children at every step index across all backends.
- Soak: sustained throughput with the broker in place, no journal drift.
- Security: sandbox escape attempts; grant denial per capability; dry-run
  refusal; a `max_calls` runaway.
- Docs and examples for every port, executed by the `docs-examples` CI job.

**Exit:** the suite above is green in CI, and `docs/` documents each port with a
runnable example.

---

## 5. Design rules for this work

Carried from what has already bitten this codebase:

1. **A port with one adapter is a guess; ship two.** In-process *and*
   subprocess; memory *and* store-backed. The second one is what proves the
   abstraction.
2. **The default must be the cheap path.** `DirectBroker`, `InProcessBackend`,
   no authority — a `Runtime()` with no arguments behaves exactly as today.
3. **Deny-by-default, and say what is missing.** Every refusal names the grant
   that would have allowed it.
4. **A check that cannot run has found nothing.** Skipped is not passed.
5. **Verify by executing.** A stale API resolves, compiles, and fails only when
   run — which is why the docs job exists.
6. **One capability, one place.** New surface area lands on the facade. If it
   has to be added to CLI, MCP, and REST separately, the boundary is wrong.

---

## 6. Testing

| Level | Coverage |
|---|---|
| Unit | every port and reference adapter in isolation |
| Contract | one suite run against *every* adapter of a port — all four stores, both backends, both brokers |
| Property | replay determinism; grant enforcement (Hypothesis) |
| Chaos | worker and child killed at each step index |
| Security | escape attempts, grant denial, dry-run refusal, call ceiling |
| Surface | the same operation via library, CLI, MCP, and REST returns the same result |
| Docs | every documented example executes (existing CI job) |

The contract suite is the one that matters most: it is what stops "works with
`MemoryStore`, breaks on Postgres" and "works in-process, breaks in a
subprocess."

---

## 7. Sequencing

```
P0 one path ──▶ P2 broker ──▶ P3 backend
                   │
                   └──▶ P4 state + stream
P5 source/versions/trace ── depends on P0 only, can run in parallel
P6 hardening ── last
P1 tenancy ── dropped
```

P2 before P3 is deliberate: the broker is the channel a subprocess speaks
across, so building isolation first would mean building it twice.
