# Hooks and middleware — investigation notes

Research for a proposed hook system: named lifecycle points (workflow start/end,
agent start/end, agent turn start/end, pre/post tool, pre/post step, pre/post
node) each dispatching a chain of registered middleware, Express-style.

**Status: research complete, design not started.** This file is the evidence the
design should be built on, and the record of which questions are already settled.

---

## Verdict

**Worth building — but as a *unification* of seams Loom already has, not a new
system beside them.**

The idea is right. The use cases named — logging, checks, supervisor, plan,
critique, verification — are real, and pipeshub's agent loop has 26 middleware in
production doing exactly this, which is strong evidence the abstraction pays for
itself rather than being a framework in search of a user.

But Loom already has **five** partially-overlapping extension mechanisms, and two
of them already do most of what a hook system would do. A sixth, parallel
mechanism would be the worst outcome available: a `TaintBroker` and a
`pre_step` middleware that both gate the same call, with no defined precedence,
and two places to look when something is blocked.

The one genuinely new thing is not the events. It is that **Loom is the only
system surveyed where a hook can run twice for one logical occurrence**, because
the workflow body is re-entered on every replay. That constraint has to shape
the design from the first line, and it is what makes this a Loom design problem
rather than a port of pipeshub's.

---

## Part 1 — What Loom already has

Six mechanisms, mapped against what a hook system would provide.

| Mechanism | Where | Composition | Can refuse? | Fires on replay? |
|---|---|---|---|---|
| **`EffectBroker`** (`runtime/effects.py`) | every durable op: step, agent, child, tool, artifact, event | **onion — a broker wraps `call.perform()`** | yes, `EffectResult(ok=False)` | **no** |
| **`Guardrail`** (`agents/guardrails.py`) | agent input, agent output, agent tool calls, node `guards` | plain `for` loop | yes — `REJECT` / `REPLACE` / `TRIPWIRE` | n/a (inside a step) |
| **`Tracer`/`Span`** (`observability/tracing.py`) | every durable call, plus the workflow body | none — one object | no | body span yes, call spans no |
| **`RunObserver`** (`runtime/effects.py`) | re-entry, and mid-body event arrival | n/a | no | **yes, by design** |
| **`CheckPipeline`** (`agents/checks.py`) | authoring-time codegen only | sequential, cost-ordered, first blocking error stops | yes | n/a |
| **`AdmissionController`** (`runtime/flowcontrol.py`) | before the run record exists | one object | yes | n/a |

### The broker is already the middleware chain

This is the central finding. `TaintBroker(GuardedBroker(DirectBroker()))` is
literally Express-style composition, already shipped:

- It sits at **every** durable operation. `ctx.step`, `ctx.node`, `ctx.agent`,
  `ctx.child`, tool calls and artifact writes all construct a `DurableCall`,
  and `DurableCall._resolve` dispatches through `runtime.broker`.
- It is **onion-shaped**, not merely a pre-gate: a broker receives
  `call.perform` and decides whether, when, and how many times to invoke it. So
  post-processing, retry and caching are all already expressible.
- It can **refuse**, with a structured reason (`EffectResult.needs` names the
  grant that would have allowed the call).
- `EffectCall` is **plain serialisable data** — kind, target, arguments, effect
  class, run id, journal path — which is why a sandboxed child can ship one to
  its parent.

What it lacks is not power. It lacks *ergonomics and coverage*:

1. **No registration API.** You write a class with a `dispatch` method. There is
   no `.use(fn)`, no decorator, no way to add one behaviour without wrapping.
2. **A documented footgun**: "Any broker wrapping another must forward both
   [`observe_run` and `forget_run`]" — a manual obligation that a `.use()`-style
   registry would remove entirely.
3. **No routing.** No way to say "only for `jira.*`", "only for writes", "only
   for this node".
4. **No named lifecycle points.** Everything is one `dispatch` with a `kind`
   string; a middleware wanting only post-tool behaviour must branch itself.
5. **Three events have no seam at all**: workflow start, workflow end, and agent
   turn start/end.

### Guardrails are a weaker second mechanism for the same job

`Guardrail` gates agent tool calls with `ALLOW`/`REJECT`/`REPLACE`/`TRIPWIRE`.
It has no `next()`, no composition, no routing, and no monotonicity rule — the
runner iterates a list and breaks. Node `guards` reuse it. This is the piece
most obviously subsumed by a hook system, and the one where a parallel mechanism
would hurt most.

---

## Part 2 — Prior art

Five systems, chosen because each solves a piece Loom needs.

### Temporal — the durable one

| | |
|---|---|
| Composition | Onion only. Each interceptor wraps the whole inner call. |
| Types | Workflow inbound/outbound, Activity inbound/outbound, Client outbound |
| Registration | `Worker(interceptors=[...])`, `Client.connect(interceptors=[...])`, or a Plugin |
| Refusal | Raise |

**The finding that matters most to this design.** Temporal's docs state
plainly:

> "Workflow inbound and outbound interceptor methods also execute during replay.
> Use replay-safe APIs for logging, randomness, and time in these interceptors."

and offer the mitigation:

> "If you want to write generic code shared by all inbound Workflow call handlers
> but want to skip read-only operations, check `workflow.unsafe.is_read_only()`."

So the closest system to Loom hit exactly this problem, and its answer was: be
explicit that workflow-level interceptors run on replay, and hand the author a
flag. Activity interceptors, which run outside the workflow, are unaffected.

That maps cleanly onto Loom: **the broker is Loom's activity interceptor**
(replay-free), and a body-level hook would be Loom's workflow interceptor
(replay-exposed).

### pipeshub (`backend/python/app/agent_loop_lib/hooks/`) — the closest sibling

Agent loop only. **No workflow, step, or node hooks exist** — `hook_dispatch.py`
in `agent/` is the sole dispatcher and `control_plane.py` the sole registrar. So
the agent half of this proposal has a proven reference; the workflow half does
not, and is where the new thinking is needed.

Eleven events: `PRE_TOOL_USE`, `POST_TOOL_USE`, `PRE_AGENT`, `POST_AGENT`,
`PRE_TURN`, `POST_TURN`, `PRE_MODEL`, `PRE_MODEL_CALL`, `POST_MODEL`,
`GUARDRAIL_INPUT`, `GUARDRAIL_OUTPUT`.

Five design decisions worth adopting outright:

1. **Two composition primitives, not one.** `Pipeline` is a single-pass `next()`
   continuation; `Wrapper` composes literal nested closures. The reason is
   stated precisely: a retry policy "needs to call 'the rest of the chain' an
   arbitrary number of times, which a Pipeline's single-pass `next()` can't
   express". `PRE_MODEL_CALL` is the only `Wrapper` event.
2. **Monotonic decisions.** There is no public setter for `decision`; middleware
   calls `ctx.deny()` / `ctx.ask()` / `ctx.block()`, and severity ordering means
   a decision can only get *more* restrictive. This "structurally rules out a
   whole class of bugs where a permissive middleware registered after a strict
   one accidentally overrides it".
3. **Fail policy per event, not global.** `fail_closed=True` for pre-gates —
   a middleware that throws denies. `fail_closed=False` for post-observers —
   "a broken formatter/logger shouldn't destroy a valid result". Errors are
   recorded in `ctx.metadata["middleware_errors"]` either way.
4. **Routing.** `use(mw)` global, `use(pattern, mw)` scoped by glob / tag /
   predicate, `tool(path, *mws)` exact, `mount(prefix, sub)` for plugin bundles.
   Tag matching (`by_tag("category", "write")`) is explicitly preferred over
   path conventions.
5. **Per-instance registries.** Each config builds its own; "nothing here is
   process-global, so tests and concurrent runs never share middleware state".

The 26 builtin middleware are the argument for the whole abstraction:
`supervisor_gate`, `require_critique`, `budget_guard`, `permission`,
`tool_safety`, `retry`, `truncation_recovery`, `stall_detection`,
`auto_compact`, `sliding_window`, `offload`, `skill_learning`,
`tool_preloading`, `logging`, and more.

### LangChain `AgentMiddleware`

Independently converged on the same two primitives:

- Node-style: `before_agent`, `before_model`, `after_model`, `after_agent`
- Wrap-style: `wrap_model_call`, `wrap_tool_call`

Ordering is explicit and worth copying: **`before_*` first-to-last, `wrap_*`
nested, `after_*` last-to-first (reverse)**. Short-circuit via `jump_to`
(`end` / `tools` / `model`), declared up front with
`@hook_config(can_jump_to=[...])`. Conflict rule for non-reducer state:
**outer wins**.

### Claude Code hooks

~30 events, and the operational lessons are in the protocol rather than the list:

- **`permissionDecision`: `deny` | `allow` | `ask`**, with an explicit
  precedence: **`deny` beats `ask` beats `allow`**. Same conclusion as
  pipeshub's severity ordering, reached independently.
- **All matching hooks run in parallel**, which is only safe *because* the
  conflict rule is a total order.
- **Two-level scoping**: a `matcher` on the event (tool name, agent type,
  session-start reason), plus an `if` condition in permission-rule syntax that
  avoids even spawning the handler.
- A timeout on `PreToolUse` **does not block** — fail-open on the gate, which is
  the opposite of pipeshub's choice and worth a deliberate decision rather than
  a default.

### OpenAI Agents SDK

The minimal end of the spectrum: `RunHooks` (whole run) and `AgentHooks` (one
agent), with `on_agent_start/end`, `on_llm_start/end`, `on_tool_start/end`,
`on_handoff`. **Pure observers — no decisions at all.** Useful as a reminder
that the observational tier is worth having on its own, and that scoping to
"the whole run" versus "one agent" is a real distinction users want.

### Convergence

| Property | Temporal | pipeshub | LangChain | Claude Code | OpenAI |
|---|---|---|---|---|---|
| Onion/wrap primitive | ✅ only | ✅ | ✅ | — | — |
| Sequential/reduce primitive | — | ✅ | ✅ | ✅ | ✅ |
| Refusal decisions | raise | ✅ monotonic | `jump_to` | ✅ ordered | — |
| Conflict rule | — | severity | outer wins | deny>ask>allow | — |
| Routing/matchers | — | ✅ glob/tag/pred | — | ✅ matcher+`if` | — |
| `after` in reverse | implicit | implicit | ✅ explicit | — | — |
| Replay-aware | ✅ **explicit** | n/a | n/a | n/a | n/a |

Four independent systems landing on *both* a sequential and a wrapping
primitive is the strongest signal in this table. One primitive is not enough,
and the reason is always the same: retry.

---

## Part 3 — The constraint no one else has

Loom re-enters the workflow body on every replay, resume, retry, and orphan
reclaim. So "before this step runs" is ambiguous in a way it is not anywhere
else, and the two readings need different machinery.

Both halves are verified in this repo, not assumed:

```
# A recording broker wrapped around DirectBroker
first run  dispatches: [('step', 'one'), ('step', 'two')]
replay     dispatches: []                     <- broker never fires on replay
```

```
# A line at the top of the workflow body
after first run : [('body entered', 2)]
after replay    : [('body entered', 2), ('body entered', 3)]   <- fires again
```

The mechanism is in `DurableCall._resolve`: the journal lookup returns the
recorded value **before** `broker.dispatch` is reached. Everything downstream of
that lookup is replay-free by construction; everything upstream runs every time.

**Therefore there are two kinds of hook, and conflating them is the bug this
design most has to avoid:**

| | **Effect hooks** | **Body hooks** |
|---|---|---|
| Fire when | the operation actually executes | every body re-entry |
| Position | after the journal lookup (broker) | before it (engine / context) |
| Replay | never | always |
| May perform I/O | yes | only if idempotent, or guarded by a replay flag |
| May refuse | yes — nothing has happened yet | no — the decision may already be journaled |
| Answers | "should this call proceed?" | "where has this run got to?" |

Concrete failures if they are one type:

- A "log every step" middleware written as a body hook emits N lines for N
  re-entries. Harmless but wrong, and it will be reported as a bug.
- An "approve before any write" middleware written as a body hook re-asks a
  human on every replay. A run that parked, resumed, and was later replayed
  would page someone three times for one write.
- A "count tokens spent" middleware written as an effect hook and expected to
  survive a park sees zero after re-entry — the same failure `RunObserver`
  already exists to fix for `TaintBroker`, and the reason its docstring says a
  broker counting dispatches "would see an empty history after any re-entry
  and, having seen nothing, permit everything".

Temporal's answer is a flag (`is_read_only()`). That is the minimum. The
stronger option available to Loom is to make the two kinds **different types
with different registration**, so the wrong one cannot be reached by accident —
the same reasoning that put `Results` in the return annotation rather than a
`paginates=True` argument.

---

## Part 4 — Open questions, resolved

**Q1. Does this duplicate the `EffectBroker`?**
Yes, if built beside it. The durable-operation events (pre/post step, node,
tool, child, agent-call) are *the broker's job*, and it is already onion-shaped,
already refusal-capable, and already replay-free. They should be implemented by
composing a hook-aware broker into the existing chain, not by adding a second
dispatch path. **Resolved: build on the broker.**

**Q2. Which events are genuinely missing?**
Workflow start, workflow end, and agent turn start/end. Nothing in Loom
observes those today except the tracer's workflow span. Every other event the
proposal names already has a seam. **Resolved: three new seams, the rest are
adapters.**

**Q3. One composition primitive or two?**
Two. Four independent systems agree, and the reason is always retry: a policy
that re-invokes "the rest of the chain" cannot be expressed with a single-pass
`next()`. Loom already needs this — `DurableCall` runs its own retry loop today,
and any middleware wanting to wrap it needs `Wrapper` semantics.
**Resolved: sequential + wrapping.**

**Q4. How do conflicting middleware decisions resolve?**
Monotonic escalation on a total order (`allow < ask < deny`), so registration
order cannot downgrade a refusal. pipeshub and Claude Code reached this
independently. **Resolved: escalate-only, no public setter.**

**Q5. What happens when a middleware throws?**
Per-event policy: pre-gates fail closed (a check that could not run has not
passed — the same rule `CheckPipeline` already applies to a missing linter, and
that `Guardrail` already applies with "a guard that raises is treated as a
tripwire, never an allow"). Post-observers fail open and record the error.
**Resolved, and consistent with existing Loom precedent.**

**Q6. Can a hook journal its own durable work?**
This is the one that decides whether the headline use cases — supervisor, plan,
critique, verification — are usable at all, since all of them call a model.

Yes. `Context.nested(path)` returns a context whose durable calls nest under a
child scope with its own numbering. A hook given `ctx.nested(f"{call.path}#pre")`
gets a **stable** journal path derived from the call being hooked rather than
from a sequence counter, so its own `ctx.step(...)` calls journal correctly and
replay correctly. This is exactly the mechanism nodes already use — "the body's
own calls journal beneath the node's path via `ctx.nested()`".

Without this, a critique hook would re-run its model call on every retry and
resume, silently costing money. **Resolved: hooks that need durability receive a
nested context; hooks that do not are pure.**

**Q7. Process-global or per-runtime registration?**
Per-runtime, following `ToolsetRegistry` and `NodeRegistry`, which chain to a
process-global parent via `parent=`. pipeshub's note applies directly: nothing
process-global, "so tests and concurrent runs never share middleware state".
**Resolved: mirror the existing registry pattern.**

**Q8. What happens to `Guardrail`?**
It becomes a thin adapter registered onto the tool pipeline, keeping the public
API. Its four actions map onto the decision model
(`ALLOW`→allow, `REJECT`→deny-with-message, `TRIPWIRE`→raise, `REPLACE`→result
mutation). Leaving it as a separate mechanism is the outcome to avoid.
**Resolved: subsume, do not duplicate.**

**Q9. Do hooks run inside a sandbox?**
No. `SubprocessSandbox` gives the child no store, no journal and no credentials;
every `ctx.*` call is JSON back to the parent, which turns it into the ordinary
call it would have been. Hooks therefore run **in the parent**, on the same
broker chain, and a sandboxed run and an inline one keep producing identical
journals. **Resolved: parent-side, no sandbox changes.**

**Q10. Are hooks part of the workflow's version/contract?**

**No. Record them on the run, never in the version.**

The premise that made this look hard — "a hook that denies changes what a run
does, so replay depends on the middleware set" — is false, and the code says so
in as many words. `DurableCall._resolve`, on a refusal:

> "Refused. Journal it as a failure so a replay sees what the run saw — a denial
> that left no trace would replay as if the effect had never been attempted, and
> the second run would take a different path."

Verified: deny a step on the first run, then replay the same run through a
Runtime with the middleware **removed entirely**, and the run still takes the
denied branch. The decision is journaled, not re-derived, and replay never
consults the broker at all.

So versioning would buy no reproducibility that the journal does not already
provide. Against it, three arguments in increasing weight:

1. **It cannot be done honestly.** `use(lambda ctx: ...)` has no stable
   identity. A name-based hash would look like a guarantee and not be one —
   the failure this codebase keeps naming, where a check that cannot run reports
   that it passed.
2. **It is the store argument.** "Workflows do not choose a store. Where the
   journal lives is a deployment decision… the same workflow code must run
   against all three." Middleware is *more* deployment-shaped than the store,
   not less: wanting a logger on a laptop and a supervisor in production is the
   point of it, not a misuse.
3. **It would destroy what a version means.** `content_hash` identifies "the
   source a human committed". Fold host configuration into it and one commit has
   as many versions as it has environments, so "show me this version's source"
   stops being answerable — and adding a logging middleware forks the workflow.

The precedent that looks like a counter-example is grants, and it resolves the
same way. `@workflow(grants=GrantSet(...))` *is* declared on the workflow,
because a grant states what **this workflow** needs. Middleware states what
**this deployment** enforces. Anything genuinely intrinsic to the workflow — a
required critique, say — should be declared in the decorator rather than
smuggled into a version hash.

**What to build instead**, since "not versioned" is not the same as "not
recorded":

- **`ExecutionRecord.metadata["loom.middleware"]`** — the ordered names active
  when the run opened. Answers "what policy was in force?" for an operator, at
  the cost of one list per run, and surfaces through `loom show` for free.
- **Attribution on the deciding entry.** `JournalEntry.metadata` already exists
  and is already populated at construction; a denial should name *which*
  middleware refused, not only that something did.
- **Nothing for allows.** A permitted call is an ordinary completed entry.
  Recording "policy X allowed this" on every call would double journal volume to
  answer a question nobody asks.

**This answer depends on the two-family split, and would be wrong without it.**
Only effect hooks can decide, and effect-hook decisions are journaled, so replay
is immune to the middleware set changing. Body hooks run on replay under
whatever is installed *now* — which is harmless precisely because they cannot
decide. Let a body hook deny and the reasoning above collapses: replay would
re-derive an outcome from configuration that is no longer the configuration that
produced the run.

### A pre-existing bug this uncovered

Replay reproduces a denial's *message* but not its *type*. `EffectDenied`
replays as a generic `StepError`, because the FAILED branch of
`DurableCall._resolve` reconstructs from `recorded.error.message`:

```
original run : handled as DENIAL       # except EffectDenied
replay       : handled as FAILURE (StepError)
```

A workflow that distinguishes "policy refused this" from "this broke" takes a
different branch on replay. That is a replay-fidelity gap today, independent of
hooks — but hooks turn denial from rare (only with a `GuardedBroker` wired up)
into routine, so it should be fixed alongside them: journal the error *type*, or
at minimum a `denied: true` marker the FAILED branch can re-raise faithfully.

---

## Part 5 — Implications for the design

Falling out of the above, before any API is drawn:

1. **Two hook families, named differently**, because the replay distinction must
   be unmissable rather than documented.
2. **Effect-family events are implemented on the broker chain.** One dispatch
   path, replay-correctness inherited, `observe_run`/`forget_run` forwarding
   handled once instead of by every author.
3. **Three genuinely new seams** — workflow start, workflow end, agent turn —
   added in `runtime/engine.py` and `agents/runner.py`.
4. **Sequential and wrapping primitives**, with `before` forward, `after`
   reverse, `wrap` nested.
5. **Monotonic decisions** with no public setter.
6. **Per-event fail policy**, closed for gates and open for observers.
7. **Routing by tag and effect class first**, path glob second — Loom already
   has `EffectClass` on every call, which is a better scoping key than a path
   convention and is already load-bearing for `TaintBroker`.
8. **`Guardrail` and node `guards` become adapters**, not a parallel path.
9. **A hook needing durability gets `ctx.nested()`** keyed on the hooked call's
   path.
10. **The default must cost nothing** — the same bar `DirectBroker` already
    meets at ~2µs/dispatch, since an empty pipeline on every durable operation
    is on the hot path of every workflow.

---

## Sources

- [Temporal — Interceptors (Python)](https://docs.temporal.io/develop/python/interceptors)
- [LangChain — Custom middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)
- [LangChain — How middleware lets you customize your agent harness](https://www.langchain.com/blog/how-middleware-lets-you-customize-your-agent-harness)
- [Claude Code — Hooks reference](https://code.claude.com/docs/en/hooks)
- [OpenAI Agents SDK — Lifecycle](https://openai.github.io/openai-agents-python/ref/lifecycle/)
- pipeshub-ai — `backend/python/app/agent_loop_lib/hooks/` (local checkout)
- Loom — `runtime/effects.py`, `runtime/context.py`, `agents/guardrails.py`,
  `agents/checks.py`, `observability/tracing.py`
