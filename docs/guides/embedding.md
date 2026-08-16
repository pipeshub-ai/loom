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
site.

## What to check before you ship

- **Does your host reach past a seam?** If it needs a `_private` attribute, the
  port is not finished — say so rather than working around it.
- **Is your channel's delivery idempotent?** Loom journals delivery, so it asks
  once per request; your transport should tolerate being asked again anyway.
- **Do you record source at publish time?** Without it, `version_of` resolves to
  nothing and a run cannot be traced to the code that produced it.
- **Have you checked `sandbox.enforces`?** Believing in a bound you do not have
  is worse than knowing you lack it.
