# Hooks and middleware — design

<!-- docs-illustrative -->

Follows `hooks-middleware.md`, which holds the investigation and the ten
resolved questions this design is built on. Read that first; nothing here
re-argues it.

**Boundary note.** A separate session is working on event-based trigger points.
This design deliberately touches nothing in `triggers/`, `runtime/dispatcher.py`,
or the event machinery in the engine (`send_event`, `take_event`, `_park`). Its
only engine change is two call sites around `_invoke_body`, kept as small as
possible for exactly that reason.

---

## 1. Scope

**In:** named lifecycle points, each dispatching registered middleware, covering
workflow start/end, agent start/end, agent turn start/end, and pre/post for
step, node, tool, agent-call, and child.

**Out, deliberately:**

- *A second enforcement path.* Everything that can refuse is implemented **on
  the existing broker chain**. There will not be a `pre_step` middleware and a
  `TaintBroker` disagreeing about the same call with no defined precedence.
- *Hook versioning.* Resolved in Q10: recorded per run, never in the version.
- *Per-turn agent journaling.* An agent run is one journal entry today
  (`run_agent_durably`: "internal model calls and tool calls are NOT
  individually journaled"). This design does not change that, and depends on it
  — see §2.

---

## 2. The taxonomy

Three families, and the split is derived from where journaling actually sits
rather than chosen for tidiness.

| Family | Fires | Replay | May decide | Implemented in |
|---|---|---|---|---|
| **Effect** | when a durable operation actually executes | never | **yes** | broker chain |
| **Agent** | inside one agent run | never* | yes | `agents/runner.py` |
| **Body** | every body re-entry | **always** | **no** | `runtime/engine.py` |

\* *by containment*: the whole agent run is a single journal entry, so a
completed one is served from the journal and the runner never re-enters. That
is the fact this family's safety rests on. If per-turn journaling is added
later, turn hooks must move behind that journal lookup or they become body
hooks with a decision power they must not have.

### Effect family

Everything durable already flows through `broker.dispatch`, and the journal
lookup happens *before* it — proven in the investigation notes. So this family
is replay-free by construction and needs no flag, no discipline, and no
documentation to be correct.

| Event | `EffectCall.kind` | Target |
|---|---|---|
| `step` | `step` | step name |
| `node` | `step` | `node:<node_id>` |
| `tool` | `tool` | `<toolset>.<operation>` |
| `agent` | `agent` | agent name |
| `child` | `child` | workflow name |
| `artifact`, `event` | as named | — |

Note `node` is not a distinct kind on the wire: `ctx.node` builds a
`DurableCall(kind=EntryKind.STEP, name=f"node:{node_id}")`. Rather than change
the journal format for existing runs, routing derives it from the name prefix
and exposes it as a first-class event. The journal is unchanged; only the
matcher knows.

### Body family

`workflow_start` and `workflow_end`, in `_drive_inner` around `_invoke_body`.
These are the only genuinely new seams, and the only ones exposed to replay.

**They cannot decide.** No `deny`, no `ask`, no return value that changes
control flow. That is not a simplification — it is what makes Q10's answer hold.
A body hook that could refuse would let replay re-derive an outcome from
configuration that is no longer the configuration that produced the run.

They receive `re_entry: bool` (the journal is non-empty) and `attempt: int`, so
a logger can say "resumed" instead of emitting a duplicate "started".

---

## 3. API

### Registration

`Runtime.hooks` is a `HookRegistry`, always present, empty by default, chaining
to a process-global parent exactly as `Runtime.toolsets` and `Runtime.nodes` do.

```python
rt = Runtime(store=SQLiteStore("runs.db"))

@rt.hooks.before_step
async def trace(call: EffectContext) -> None:
    logger.info("step %s starting", call.target)

@rt.hooks.before_tool(effect=EffectClass.DESTRUCTIVE)
async def confirm_deletes(call: EffectContext) -> None:
    call.ask(f"{call.target} deletes data")

@rt.hooks.around_step(target="jira.*")
async def with_cache(call: EffectContext, next: Next) -> Any:
    if (hit := cache.get(call.fingerprint)) is not None:
        return hit
    value = await next()
    cache[call.fingerprint] = value
    return value

@rt.hooks.on_workflow_start
async def note(run: BodyContext) -> None:
    if not run.re_entry:
        metrics.increment("runs.started", workflow=run.workflow)
```

Both forms work — decorator with no arguments, or called with routing
arguments and used as a decorator. `rt.hooks.before_step(fn)` is equivalent for
programmatic registration.

### Three shapes, not one

| Shape | Signature | Use for |
|---|---|---|
| `before_*` | `(ctx) -> None` | inspect, mutate arguments, decide |
| `after_*` | `(ctx) -> None` | observe or rewrite a result |
| `around_*` | `(ctx, next) -> Any` | retry, cache, time, fail over |

**`before`/`after` take no `next`.** This departs from Express and from
pipeshub, and it is deliberate: a sequential middleware that forgets to call
`next()` silently stops the chain, and that bug is invisible until the one
middleware nobody tested doesn't run. Short-circuiting is expressed as
`ctx.deny(...)`, which the dispatcher honours — so the capability survives and
the footgun does not. `around` keeps `next` because re-invoking it *is* the
feature.

Ordering follows LangChain's, which is the one users already expect:
**`before` first-to-last, `around` nested (first registered outermost), `after`
last-to-first.**

### Decisions

```python
ctx.allow()          # default; a no-op, present so intent can be written down
ctx.ask(reason)      # park the run on a human
ctx.deny(reason)     # refuse the call
```

Monotonic on `allow < ask < deny`, with **no public setter for the decision**.
A later, more permissive middleware cannot undo an earlier refusal, so
registration order cannot silently weaken policy. Both pipeshub and Claude Code
reached this independently.

`ask` is where Loom can do something the surveyed systems cannot: it parks the
run rather than blocking a thread. The hook's nested context calls
`ctx.wait_for_approval(...)`, which raises `Suspend`; `DurableCall._resolve`
already re-raises `ControlSignal` untouched, so the run parks. On resume the
body re-enters, the call is still un-journaled, the broker dispatches again, the
hook runs again — and this time `wait_for_approval` finds the journaled answer
and returns it. The stable nested path is what makes that work, and it falls out
of Q6 rather than needing new machinery.

### Durable work inside a hook

A hook that needs to journal — a supervisor, a critique, a verification model
call — receives a context scoped to the call it is hooking:

```python
@rt.hooks.before_agent
async def critique_first(call: EffectContext) -> None:
    verdict = await call.ctx.step(review_plan, call.arguments["prompt"])
    if verdict.risky:
        call.ask(verdict.reason)
```

`call.ctx` is `ctx.nested(f"{call.path}#before")` — a **stable** path derived
from the hooked call, not from a sequence counter. Its `ctx.step(...)` calls
journal and replay correctly, so the model is paid for once. This is the same
mechanism nodes already use to journal beneath their own path.

`EffectCall` gains one field to carry it:

```python
context: Any = field(default=None, compare=False, repr=False)
```

Alongside `perform`, and excluded from equality and repr for the same reason.
`EffectCall.describe()` whitelists its fields explicitly, so the wire projection
a sandboxed child sends is unchanged and no serialisability is lost.

### Routing

Matched cheapest-first, all optional and combinable:

| Argument | Matches on |
|---|---|
| `effect=EffectClass.WRITE` | the declared effect class |
| `target="jira.*"` | glob over the target |
| `node="human.*"` | glob over the node id |
| `where=lambda ctx: ...` | any predicate |

`effect` is listed first because Loom already carries `EffectClass` on every
call and already relies on it for `TaintBroker`. It is a better scoping key than
a path convention, and it is the one pipeshub's notes say they wish they had
reached for sooner (`by_tag("category", "write")` over `/toolsets/*/write/*`).

---

## 4. Failure policy

Per event, not global:

- **`before` and the pre-half of `around` fail closed.** An exception becomes a
  denial naming the middleware. Consistent with what Loom already does
  elsewhere: `CheckPipeline` reports a stage whose tool is missing as *skipped*
  rather than passed, and a guardrail that raises "is treated as a tripwire,
  never an allow — a check that cannot run has found nothing".
- **`after` fails open.** The work already happened; a broken formatter must not
  destroy a valid result. The error is recorded on the journal entry's metadata.

---

## 5. Where it plugs in

Four files, and the engine change is two lines by design.

| File | Change |
|---|---|
| `runtime/hooks.py` *(new)* | `HookRegistry`, `Pipeline`, `Wrap`, contexts, decisions, matchers |
| `runtime/effects.py` | `HookBroker`, composed into the chain; one field on `EffectCall` |
| `runtime/engine.py` | `hooks` on `Runtime`; two dispatches around `_invoke_body` |
| `agents/runner.py` | agent/turn dispatches inside the loop |

`HookBroker` wraps an inner broker and forwards `observe_run`/`forget_run`,
which removes the standing obligation CLAUDE.md currently places on every
author of a wrapping broker. Composition order is
`HookBroker(TaintBroker(GuardedBroker(DirectBroker())))` — hooks outermost, so a
hook sees a call before taint and grants weigh in, and a hook's `deny` costs
nothing downstream.

---

## 6. What this replaces

`Guardrail` becomes an adapter, not a parallel path. Its actions map onto the
decision model with no loss:

| `GuardrailAction` | Becomes |
|---|---|
| `ALLOW` | `allow()` |
| `REJECT` | `deny(message)` — the model is handed the explanation, as now |
| `TRIPWIRE` | raise, aborting the run, as now |
| `REPLACE` | result mutation in an `after` hook |

`Agent(guardrails=[...])` and node `guards=[...]` keep working unchanged; they
register onto the tool pipeline underneath. Nothing in a user's code changes,
and there stops being a second mechanism to learn.

---

## 7. Cost

The default must be free, because an empty pipeline sits on the hot path of
every durable operation in every workflow — the same bar `DirectBroker` meets at
~2µs.

- `Runtime` composes `HookBroker` **only when a hook is registered**. A Runtime
  with no hooks has the exact broker chain it has today, so the cost is
  literally zero rather than nearly zero.
- Once installed, an event with no middleware is one dict lookup and a branch.
- Matchers are evaluated once per dispatch over the registered list, not per
  middleware per call.

A benchmark test pins this: dispatch throughput with hooks unregistered must not
regress against the current chain.

---

## 8. Recording, per Q10

- `ExecutionRecord.metadata["loom.middleware"]` — ordered names active when the
  run opened. Surfaces through `loom show` for free.
- A denial names **which** middleware refused, on the journal entry's existing
  `metadata`.
- Nothing recorded for allows.

### The fidelity fix this depends on

`EffectDenied` currently replays as a generic `StepError`, because the FAILED
branch of `DurableCall._resolve` rebuilds from `recorded.error.message` alone. A
workflow that distinguishes "policy refused this" from "this broke" takes a
different branch on replay:

```
original run : handled as DENIAL          # except EffectDenied
replay       : handled as FAILURE (StepError)
```

That is a bug today, independent of hooks — but hooks turn denial from rare into
routine, so it ships with them: record the error *type* (or a `denied: true`
marker) so the FAILED branch can re-raise faithfully.

---

## 9. Test plan

The properties worth pinning, each of which fails silently if unasserted:

1. **An effect hook does not fire on replay.** The proof the whole taxonomy
   rests on.
2. **A body hook does fire on replay**, and `re_entry` is true the second time.
3. **A body hook has no way to deny** — asserted structurally, not by
   convention.
4. **Decisions are monotonic**: a permissive middleware registered after a
   `deny` cannot lift it, in either registration order.
5. **`before` failing closed, `after` failing open**, each with the error
   recorded.
6. **Ordering**: `before` forward, `after` reverse, `around` nested.
7. **`around` may call `next()` more than once** — the case a single-pass
   `next()` cannot express, and the reason there are two primitives.
8. **A hook's `ctx.step` journals once across a retry**, so a critique is paid
   for once.
9. **`ask` parks the run and resumes with the journaled answer**, without the
   hook re-asking.
10. **A denial replays as a denial**, including its type (§8).
11. **`EffectCall.describe()` is unchanged** by the new field, so the sandbox
    wire shape is untouched.
12. **Zero cost when unregistered**: no `HookBroker` in the chain.
13. **Guardrail adapters preserve existing behaviour** — the current guardrail
    tests pass unmodified.

---

## 10. Phasing

Each phase is independently useful and independently shippable.

| Phase | Delivers |
|---|---|
| **1** ✅ | `runtime/hooks.py` + `HookBroker`; effect family only; `before`/`after`/`around`; decisions; routing. **Shipped** — 51 tests in `tests/test_hooks.py`. |
| **2** ✅ | Body family — `workflow_start`/`workflow_end` in `_invoke_body`; `re_entry`; `status` naming how the body exited. **Shipped**. |
| **3** ✅ | Agent family — `agent_start/end`, `turn_start/end`, `model_start/end` in the runner. **Shipped**. |
| **4** ✅ | `use_guardrail()` adapter; `loom.middleware` recorded on the run; docs. **Shipped** — see the scope change below. |

The `EffectDenied` fidelity fix (§8) **shipped separately**, after the four
phases, for the reason it was held back:
it changes what a replayed run raises, so it is a behaviour change to existing
workflows that catch by type and deserves to land on its own rather than inside
a feature.

Two things phase 1 learned that the design did not anticipate:

- **`ask` works today**, end to end, with no new machinery — park, approve,
  resume, perform exactly once. It was written up as a possibility and turned
  out to be free.
- **`ctx.arguments` reports keyword arguments only.** `_effect_arguments` drops
  positional ones ("a step invoked positionally has no argument names to
  report"). Widening it would change a wire type that the sandbox and taint
  paths both read, so it stays a decision of its own.

Phase 1 alone covers every event the original proposal named except workflow
start/end and agent turns, because the broker already sits at all of them. That
is the measure of how much of this is unification rather than construction.


---

## What the later phases changed about the design

Three departures, each decided while building rather than planned:

**The agent family cannot decide.** The design said it could. It should not:
"may this agent run?" is already an effect hook on `kind="agent"`, and "may this
tool call run?" is already one on `kind="tool"`, because a tool call inside the
loop goes through the broker like everything else. A second way to refuse the
same two things is the exact duplication this design exists to prevent. What is
left — shaping messages before a model call, observing responses, counting turns
— is most of what middleware is actually used for. `AgentHookContext.stop()`
covers the one remaining case honestly: ending the loop is not refusing a call,
and calling both "deny" would describe one of them badly.

**Node `guards` are left alone.** The design said re-home them alongside
`Guardrail`. On reading them, they are not the same mechanism: a guard validates
and transforms a node's *payload* across input and output phases, where a hook
gates *dispatch*. Merging them would be a large refactor of working code to make
two different things share a name.

**`Guardrail` gets an adapter, not a migration.** `use_guardrail()` registers an
existing guardrail as a hook, and `Agent(guardrails=[...])` is untouched — it
has to be, because an agent can run with no Runtime at all and therefore no
registry to fall back on. The gain is being able to apply one check to every
durable call rather than to one agent's tool calls, without writing it twice.

**`turn_end` without re-indenting the loop.** A `try/finally` around the turn
body would be the obvious implementation and would touch 224 lines of working
turn loop. `_Turns` closes the previous turn when the next opens, and the
`execute` wrapper closes the last one — so all three `return`s and any raise
still produce exactly one `turn_end` per `turn_start`, from a three-line diff
inside the loop.
