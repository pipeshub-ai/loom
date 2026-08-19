# Embedding Loom in your own product

This is the guide for putting Loom *inside* something else — a platform with its
own users, its own database, its own idea of where code lives and who is allowed
to approve what. Not `loom run`, not the HTTP server: your process, your
composition.

Everything here executes in CI, so nothing on this page can quietly stop being
true. The same host is written as a test in `tests/test_host_integration.py`,
which additionally checks that it reaches past no seam.

<!-- docs-preamble -->

```python
import asyncio

from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore


@step
async def fetch_invoice(invoice_id: str) -> dict:
    return {"id": invoice_id, "amount": 4200}


@workflow(name="settle")
async def settle(ctx: Context, payload: dict) -> str:
    invoice = await ctx.step(fetch_invoice, invoice_id=payload["invoice_id"])
    return f"settled {invoice['amount']}"
```

## The one rule

**A workflow module declares steps and workflows and nothing else.** It does not
choose a store, a sandbox, or a channel. Every one of those is a deployment
decision, and a body that made them could not run in your test suite, on a
laptop, and in your production cluster without being edited.

```python
@step
async def fetch_invoice(invoice_id: str) -> dict:
    return {"id": invoice_id, "amount": 4200}


@workflow(name="settle")
async def settle(ctx: Context, payload: dict) -> str:
    invoice = await ctx.step(fetch_invoice, invoice_id=payload["invoice_id"])
    return f"settled {invoice['amount']}"
```

The host composes everything around that:

```python
async def main() -> str:
    runtime = Runtime(store=MemoryStore())
    runtime.register(settle)
    result = await runtime.run(settle, {"invoice_id": "INV-1"})
    return result.output


assert asyncio.run(main()) == "settled 4200"
```

## The seams you will actually need

Each is optional and independently swappable. Supplying none gives you the
embedded default; supplying one never forces the others.

| Seam | Constructor | What your product plugs in |
|---|---|---|
| `ExecutionStore` | `Runtime(store=…)` | where journals live — Postgres, Mongo, your own |
| `ExecutionSandbox` | `Runtime(sandbox=…)` | where a body runs, if not in this process |
| `HumanChannel` | `Runtime(human=…)` | how a person is asked — Slack, your web UI |
| `EffectBroker` | `Runtime(broker=…)` | what every durable call is weighed against |
| `VersionStore` | `Runtime(versions=…)` | where the code that ran is recorded |
| `CredentialResolver` | `ConnectionBroker(resolver=…)` | your credential service |
| `Clock` | `Runtime(clock=…)` | testable time |

`docs/seams/` has a generated page per port, checked against the code in CI.

## Asking a person

Loom parks the run, journals the request, and validates the answer. **Delivering
it is yours** — that is the whole `HumanChannel` protocol, two methods:

```python
from loom.nodes.human.channel import DeliveryReceipt, HumanRequest


class Switchboard:
    """Your notification transport. A real one posts to Slack."""

    name = "switchboard"

    def __init__(self) -> None:
        self.delivered: list[HumanRequest] = []

    async def deliver(self, request: HumanRequest) -> DeliveryReceipt:
        # request.response_schema is the JSON Schema of the accepted answer,
        # so build your UI from the request rather than special-casing node ids.
        self.delivered.append(request)
        return DeliveryReceipt(delivered=True, channel=self.name)

    async def withdraw(self, request_id: str, reason: str) -> None:
        self.delivered = [r for r in self.delivered if r.request_id != request_id]


# Use the human.approval *node*, not ctx.wait_for_approval. Both park the run
# and both are resolved by runtime.approve(), but only the node delivers to your
# channel — and a run parked with nobody notified is indistinguishable from
# patience, so it gets found a day late.
@workflow(name="settle_with_approval")
async def settle_with_approval(ctx: Context, payload: dict) -> str:
    invoice = await ctx.step(fetch_invoice, invoice_id=payload["invoice_id"])
    await ctx.node("human.approval", {"subject": "payment"})
    return f"paid {invoice['amount']}"


async def with_a_person() -> tuple[str, int]:
    switchboard = Switchboard()
    runtime = Runtime(store=MemoryStore(), human=switchboard)
    runtime.register(settle_with_approval)

    parked = await runtime.run(settle_with_approval, {"invoice_id": "INV-2"})
    assert parked.status.value == "suspended"

    # Your API answers, on behalf of whoever it authenticated.
    await runtime.approve(parked.run_id, "payment")
    resumed = await runtime.resume(parked.run_id)
    return resumed.output, len(switchboard.delivered)


output, notifications = asyncio.run(with_a_person())
assert output == "paid 4200"
assert notifications == 1  # journaled, so a restart does not re-notify
```

**No channel configured raises before the run parks.** That is deliberate: it
fails at wiring time rather than becoming a run nobody is waiting on.

## Running code you did not write

If the bodies are generated — by Loom's coding agent or your own — run them
somewhere your credentials are not:

```python
from loom.runtime.sandbox import SandboxPolicy
from loom.runtime.sandboxes import SubprocessSandbox

sandboxed = Runtime(
    store=MemoryStore(),
    sandbox=SubprocessSandbox(),
    sandbox_policy=SandboxPolicy(
        allowed_env=frozenset({"TZ"}),  # an allowlist; everything else is stripped
        max_wall_seconds=60,
    ),
)
assert sandboxed.sandbox.name == "subprocess"
```

The child holds no store, no journal, and no credentials: it decides *what* to
call, and your process decides whether, performs it, and records it. A sandboxed
run produces the same journal as an inline one and passes the same broker chain.

Check what a sandbox actually enforces before you rely on it — the answer is
platform-dependent, and a policy asking for something it cannot apply is
**refused rather than ignored**:

```python
from loom.runtime.sandboxes import SubprocessSandbox

enforced = SubprocessSandbox().enforces
assert "allowed_env" in enforced
assert "max_wall_seconds" in enforced
```

## Delivering events exactly once

Your connectors are at-least-once; give Loom the broker's message id and a
redelivery resumes the run zero more times:

```python
@workflow(name="on_message")
async def on_message(ctx: Context, payload: dict) -> str:
    return await ctx.wait_for_event("payment_received")


async def deliver_twice() -> tuple[bool, bool]:
    runtime = Runtime(store=MemoryStore())
    runtime.register(on_message)
    parked = await runtime.run(on_message, {})

    first = await runtime.send_event(
        parked.run_id, "payment_received", {"ok": True}, dedupe_key="kafka-offset-42"
    )
    second = await runtime.send_event(
        parked.run_id, "payment_received", {"ok": True}, dedupe_key="kafka-offset-42"
    )
    return first.delivered, second.delivered


first, second = asyncio.run(deliver_twice())
assert (first, second) == (True, False)
```

`EventDelivery.reason` is `"duplicate"` on the second, so your consumer can ack
the redelivery instead of retrying it.

## Running things on a schedule

Declare the schedule on the workflow; the host runs a dispatcher. Registration
is idempotent, so calling it on every boot is the intended usage:

```python
from loom.runtime.clock import ManualClock
from loom.runtime.dispatcher import TriggerDispatcher
from loom.triggers.specs import Schedule


@workflow(name="nightly_digest", triggers=[Schedule("0 3 * * *", timezone="Europe/London")])
async def nightly_digest(ctx: Context, _: object = None) -> str:
    return "sent"


async def schedule_it() -> tuple[int, int]:
    # A fixed clock so this example is reproducible; production passes none.
    clock = ManualClock(__import__("datetime").datetime(
        2026, 3, 2, 2, 0, tzinfo=__import__("datetime").UTC
    ))
    runtime = Runtime(store=MemoryStore(), clock=clock)
    dispatcher = TriggerDispatcher(runtime)

    # Three boots, as a redeploy loop would do.
    for _ in range(3):
        await dispatcher.register(nightly_digest)

    triggers = await runtime.store.list_triggers()
    fired = await dispatcher.tick(__import__("datetime").datetime(
        2026, 3, 2, 3, 0, tzinfo=__import__("datetime").UTC
    ))
    return len(triggers), len(fired)


records, fired = asyncio.run(schedule_it())
assert records == 1   # re-registering does not add a second schedule
assert fired == 1     # and the 03:00 occurrence runs once
```

**Cron survives restarts because it lives in the store**, not in the process: a
`TriggerRecord` holds `next_fire_at`, `last_fire_at`, and `run_count` behind the
`TriggerStore` protocol that all four backends implement.

In production you run one loop rather than ticking by hand, and cron shares it
with due timers and orphan recovery:

```python
from loom.runtime.dispatcher import TriggerDispatcher

runtime = Runtime(store=MemoryStore())
dispatcher = TriggerDispatcher(runtime)
# await runtime.start_scheduler(interval=5, dispatcher=dispatcher)
#     ... and pass elector= to run many processes against one store.
assert dispatcher is not None
```

Two properties worth knowing before you run more than one replica:

- **An occurrence is submitted under a deterministic idempotency key**
  (`trigger_id@scheduled_for`), so two dispatchers racing produce one run. You
  do not need to elect a leader for *correctness* — only to avoid duplicate
  work, which `claim_due_triggers` handles: the due set is *taken*, under a
  lease, so a dispatcher that dies mid-tick delays one occurrence rather than
  stranding the trigger.
- **A missed window is a decision.** By default the pending occurrence runs and
  the rest are skipped and logged. `Schedule(..., catch_up=True)` replays them
  instead, bounded by `max_catch_up`, keeping the newest.

`Schedule(..., jitter=60)` spreads a fleet that all fire at midnight. It delays
dispatch only — never the schedule — and the delay is derived from the
occurrence, so every replica agrees on it.

## Knowing which code ran

Publish with the source you deployed, and any finished run can be traced back to
it — including after the workflow has changed:

```python
async def trace() -> str:
    runtime = Runtime(store=MemoryStore())
    runtime.register(settle)
    await runtime.publish(settle, source="# the file you deployed\n")

    result = await runtime.run(settle, {"invoice_id": "INV-3"})
    version = await runtime.version_of(result.run_id)
    return await runtime.versions.source_of(version)


assert asyncio.run(trace()) == "# the file you deployed\n"
```

Identical source returns the existing version rather than appending, so a
retried deploy does not inflate the chain.

## Restricting what generated code may do

Compose the broker chain. `TaintBroker` refuses a write or a delete once the run
has read something nobody reviewed, and a human approval clears it:

```python
from loom.runtime.effects import DirectBroker
from loom.runtime.taint import TaintBroker, TaintPolicy

guarded = Runtime(
    store=MemoryStore(),
    broker=TaintBroker(
        DirectBroker(),
        # Nearly every useful workflow writes after reading; very few need to
        # delete. The two are separate dials for that reason.
        TaintPolicy(block_writes=False, block_destructive=True),
    ),
)
assert guarded.broker.policy.block_destructive
```

For this to see anything, your toolset manifests must declare each operation's
`EffectClass` — that declaration is what tells a read from a write at the call
site. An operation nobody classified defaults to `READ`, which under this broker
means it *taints* rather than being ignored.

**One sharp edge worth knowing before you turn this on.**

The rule keys on an operation being both a *read* and **open-world** — reaching
outside the deployment. Pure computation does not taint: `control.filter`,
`transform.map_fields` and the rest of `control.*`/`transform.*`/`guard.*` are
reads that read nothing, and a workflow that filters a list it was handed and
then writes is not refused. Toolset operations are open-world by default,
because a toolset is a network call; set `open_world=False` on an
`OperationSpec` for one that wraps computation you already own.

**Two narrow dials, off by default.** `block_writes` is the strict one, and
almost every useful workflow writes after reading — so a deployment that finds
it unusable turns it off and then has nothing. These say the useful thing
instead:

```python
from loom.runtime.taint import TaintPolicy

TaintPolicy(
    block_writes=False,          # updating a record after reading is fine
    block_irreversible=True,     # sending the email is not
    block_access_control=True,   # nor is sharing the folder
)
```

`block_irreversible` reads `reversible`, which is what `EffectClass` cannot
express. `gmail_trash_message` is DESTRUCTIVE and reversible for thirty days;
`gmail_send_message` is WRITE and reversible by nothing — so ranked by class the
policy stops the recoverable operation and permits the irreversible one, and
ranked by this dial it does the opposite. `block_access_control` is separate
because sharing exfiltrates without writing anything to the thing shared, and
reads as an ordinary additive write.

Both are off until you have populated `reversible` on your own toolsets —
turning them on before that reads every operation as irreversible. Neither
applies to a read, and both are checked *as well as* the class, so enabling one
can only add refusals.

## Middleware around every operation

`rt.hooks` runs your code around each durable operation — step, node, tool,
agent, child. Empty by default and **free while it stays that way**: the first
registration is what installs the broker, so a Runtime with no hooks runs
exactly the chain it ran before.

```python
from loom.runtime.hooks import HookContext
from loom.toolsets.manifest import EffectClass

audited = Runtime(store=MemoryStore())


@audited.hooks.before_step
async def note(ctx: HookContext) -> None:
    print("about to run", ctx.target)


@audited.hooks.before_any(effect=EffectClass.DESTRUCTIVE)
async def confirm(ctx: HookContext) -> None:
    ctx.deny("deletes are not allowed from this deployment")


assert audited.hooks.names() == ["note", "confirm"]
```

Three shapes. `before` and `after` observe, mutate and decide; `around` receives
the rest of the chain and may call it zero, one, or many times — which is what
retry and caching need and what a single-pass hook cannot express. `before` runs
in registration order and `after` in reverse, because they compile to nested
wrappers and that is what nesting means.

**Decisions escalate and never descend.** A middleware calls `ctx.deny(...)` or
`ctx.ask(...)`; there is no setter. So a permissive middleware registered after
a strict one is a no-op rather than a silent hole, and registration order is not
something you have to keep in your head.

```python
from loom.runtime.hooks import Decision, HookContext
from loom.runtime.effects import EffectCall

ctx = HookContext(EffectCall(kind="step", target="charge"))
ctx.deny("over threshold")
ctx.allow()                       # cannot undo it

assert ctx.decision is Decision.DENY
```

`ctx.ask(reason)` **parks the run** rather than blocking — the answer is
journaled, and the resumed run performs the call exactly once.

### Which hook to reach for

Three families, and picking the wrong one is the mistake worth avoiding:

| You want to | Use | Runs on replay |
|---|---|---|
| gate, cache, retry, or rewrite a call | `before_/after_/around_*` | no |
| know a run started, resumed, or ended | `on_workflow_start/end` | **yes** |
| shape messages, count turns, watch an agent | `on_agent_*`, `on_turn_*`, `on_model_*` | no |

Only the first family can decide. The body family fires on **every** re-entry —
resume, retry, replay — so it is observation only, and `ctx.re_entry` tells
"began" from "resumed". That restriction is what keeps middleware out of the
workflow version: it can change what is *observed* on a replay, never what
happened.

```python
started: list[bool] = []
watched = Runtime(store=MemoryStore())


@watched.hooks.on_workflow_start
async def began(ctx) -> None:
    started.append(ctx.re_entry)


assert watched.hooks.has_body
```

A hook that needs to journal its own work — a supervisor, a critique, a
verification model call — uses `ctx.ctx`, a context scoped beneath the call it
is hooking. Its `ctx.step(...)` calls journal against a **stable** path, so the
model is paid for once rather than on every retry and resume.

**Middleware is recorded on the run, never in the version.**
`ExecutionRecord.metadata["loom.middleware"]` names what was in force, because a
denial has to be explicable months later. It stays out of `content_hash`
deliberately: middleware says what a *deployment* enforces, not what a workflow
*is*, so folding it in would give one commit as many versions as it has
environments.

## Shutting down

Your process will be told to stop, and on a deploy that means SIGTERM. Loom's
recovery story *is* the cleanup, so this is not housekeeping: `shutdown()` stops
the schedulers, and the `finally` around each in-flight run settles the lease
that `reclaim_orphans` later matches on. Killed where it stands, none of it
runs.

Use the Runtime as a context manager, so the shutdown cannot be skipped by the
path that skips things — the exceptional one:

```python
async def serve_one() -> str:
    async with Runtime(store=MemoryStore()) as runtime:
        runtime.register(settle)
        result = await runtime.run(settle, {"invoice_id": "INV-9"})
        return result.status.value


assert asyncio.run(serve_one()) == "completed"
```

`shutdown(drain=...)` is the grace period. It stops the sources of new runs
first — supervised dispatchers and queue consumers, then the scheduler — then
waits for in-flight drives, then cancels. Five seconds by default; pass `0` in
tests.

Route the signals through `guarded`, which cancels your entry coroutine instead
of letting the process die mid-step:

```python
from loom.runtime.shutdown import Interrupted, guarded, run_main


async def forever() -> str:
    async with Runtime(store=MemoryStore()) as runtime:
        runtime.register(settle)
        await runtime.start_scheduler(interval=5)
        return "ready"


assert asyncio.run(guarded(forever())) == "ready"
assert Interrupted(15).exit_code == 143   # SIGTERM, the shell's convention
assert callable(run_main)                 # asyncio.run for a __main__ block
```

Nothing is installed by importing that module — a library that seizes your
process's signal handlers is one you cannot embed. You ask for it, at your
entry point.

**An interrupted run is not a failed run.** It stays unfinished — `RUNNING`, or
`PENDING` if it had not started — with an expired lease, and is picked up by the
next `reclaim_orphans()` on any node, which re-enters the body and serves
everything already journaled. So a rolling deploy does not need to drain to
zero; it needs *some* node still scanning. What it must not do is mark those
runs failed, which is why cancellation is handled separately from exceptions in
the engine, and why your own shutdown path should not "tidy up" an unfinished
record on the way out. The expired lease is what makes it findable; without one
it is invisible to every scan.

## What to check before you ship

- **Does your host reach past a seam?** If it needs a `_private` attribute, the
  port is not finished — say so rather than working around it.
- **Is your channel's delivery idempotent?** Loom journals delivery, so it asks
  once per request; your transport should tolerate being asked again anyway.
- **Do you record source at publish time?** Without it, `version_of` resolves to
  nothing and a run cannot be traced to the code that produced it.
- **Have you checked `sandbox.enforces`?** Believing in a bound you do not have
  is worse than knowing you lack it.
- **Does SIGTERM reach your cleanup?** If the answer is "the container just
  stops", every run in flight at deploy time is one nobody settled.
