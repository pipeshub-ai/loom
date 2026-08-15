# Writing a node

A **node** is a typed, reusable unit of workflow work: Pydantic in, Pydantic
out. Where a `@step` is your function journaled, a node is a *shareable
contract* — the coding agent can find it, render the exact call, and check that
call before anyone runs it.

This page builds one end to end. Every snippet on it executes in CI, and
`tests/test_node_guide.py` writes these exact files into an empty directory and
drives the result through every path a real node is used by — so if the guide
stops being true, the build fails rather than the reader.

---

## When to write one

Write a node when the work has a **contract worth naming**: a shape of input, a
shape of output, and a name someone else would search for. Write a plain `@step`
when the work is yours alone — one function, one workflow, no reuse.

The tell is the second caller. A `@step` copied into a second workflow with the
arguments shuffled is a node that has not been written yet.

Before writing anything, check whether one exists:

```
loom nodes --category transform
loom node transform.map_fields
```

---

## Step 1 — the two models

The models *are* the contract. They become the JSON Schema the coding agent
reads, the fake the sandbox runs against, and the hash that makes replay safe
across an upgrade — so the effort spent here is the effort you do not spend
documenting the node.

```python
from pydantic import BaseModel, Field

from loom import Context, Runtime, step, workflow
from loom.nodes import (
    EffectClass,
    Node,
    NodeCategory,
    NodeExample,
    NodeSpec,
    get_node_catalog,
    register_node,
)
from loom.stores import MemoryStore


class ScoreIn(BaseModel):
    text: str = Field(description="The lead blurb to score.")
    threshold: float = Field(default=0.5, description="Score at or above this passes.")


class ScoreOut(BaseModel):
    score: float
    passed: bool
    reason: str = ""
```

Two rules, both because the models are read by a model:

**Describe every input field.** `Field(description=...)` is rendered as the
trailing comment in the generated call, and it is the only place the agent
learns what `threshold` means. A field without one renders as a bare type.

**Report what the caller cannot derive.** `reason` costs one line and turns an
unexplainable score into a reviewable one. The built-in nodes do this
consistently — `control.filter` returns `dropped`, `transform.map_fields`
returns `missing` — because a node that silently discarded everything looks
exactly like one that was given nothing.

---

## Step 2 — the node

<!-- docs-preamble -->
```python
from pydantic import BaseModel, Field

from loom import Context, Runtime, step, workflow
from loom.nodes import (
    EffectClass,
    Node,
    NodeCategory,
    NodeExample,
    NodeSpec,
    get_node_catalog,
    register_node,
)
from loom.stores import MemoryStore


class ScoreIn(BaseModel):
    text: str = Field(description="The lead blurb to score.")
    threshold: float = Field(default=0.5, description="Score at or above this passes.")


class ScoreOut(BaseModel):
    score: float
    passed: bool
    reason: str = ""


@step
async def fetch_signals(text: str) -> int:
    """Real work lives in a step. The node body composes steps."""
    return len(text)


@register_node
class LeadScoreNode(Node[ScoreIn, ScoreOut]):
    """Score a lead from its description text."""

    spec = NodeSpec(
        id="custom.lead_score",
        version="1.0.0",
        category=NodeCategory.TRANSFORM,
        summary="Score a lead from its description text.",
        tags=["lead", "score", "qualify"],
        examples=[
            NodeExample(
                title="Score an inbound blurb",
                payload={"text": "Acme wants a demo for 40 seats", "threshold": 0.4},
            )
        ],
    )
    Input, Output = ScoreIn, ScoreOut

    async def run(self, ctx, payload: ScoreIn) -> ScoreOut:
        signals = await ctx.step(fetch_signals, payload.text)
        score = min(signals / 50, 1.0)
        return ScoreOut(
            score=score,
            passed=score >= payload.threshold,
            reason=f"{signals} signals in the blurb",
        )
```

That is the whole integration. `@register_node` derives `input_schema`,
`output_schema`, and `node_class` from the class, so the node is searchable,
renderable, fake-able, and validator-known **with no second declaration
anywhere**. There is no manifest to keep in step and no docs entry to update.

**Do your I/O through `ctx`.** A node body is workflow code: an HTTP call made
directly rather than inside `ctx.step()` is not journaled and runs again on
every replay. `NodeContext` gives you `step`, `call`, `sleep`,
`wait_for_event`, `agent`, `report`, and the deterministic `now`/`uuid4`/
`random`. It deliberately does **not** give you `continue_as_new`, `child`,
`publish`, or the artifact API — those restructure the run your node is a guest
in.

**`id` must be namespaced.** `custom.lead_score`, not `lead_score`: a flat id
collides the moment two packages pick the same word, and registration refuses
one.

---

## Step 3 — declare what is true about it

Four spec fields change how LOOM treats the node. Each defaults to the safe
answer, and each is worth a moment.

| Field | Set it when | Consequence |
|---|---|---|
| `deterministic` | the body could answer differently on a replay | `False` means the answer is journaled rather than recomputed |
| `suspends` | the node can park the run | the agent needs it *before* writing the call |
| `effect` | the node writes or deletes | `resolve_tools(effects={READ})` and grants filter on it |
| `requires` | the node needs something on the Runtime | checked at resolution, before the body runs |

`deterministic` is the one people get wrong. A node that calls a model, reads a
clock, or consults a policy that can change is **not** deterministic, and saying
it is means a replay re-decides against today's world and disagrees with what
the run actually did.

```python
JUDGEMENT = NodeSpec(
    id="custom.triage",
    category=NodeCategory.AGENT,
    summary="Decide how urgent a ticket is.",
    deterministic=False,      # it asks a model
    effect=EffectClass.READ,
)
```

---

## Step 4 — check what the agent will see

This is the step people skip, and it is the one that decides whether generated
code is right. Look at the rendered contract, because that — not your docstring
— is what the coding agent writes from:

```
loom node custom.lead_score
```

```text
# custom.lead_score  v1.0.0  [transform]   suspends: no   effect: read

from my_nodes import ScoreIn, ScoreOut

result: ScoreOut = await ctx.node(
    'custom.lead_score',
    ScoreIn(
        text='Acme wants a demo for 40 seats',  # str — The lead blurb to score.
        threshold=0.4,                          # float, optional — Score at or above this passes.
    ),
)

# ScoreOut: score: float, passed: bool, reason: str
```

If the field comments are unhelpful, fix the `Field(description=...)`. If the
values look wrong, fix `examples[0]` — that is where they come from. A node with
no example renders type-shaped placeholders, which is a worse starting point
than a real call.

Note the import line: it comes from where the class lives. When your public
path differs from the module — you re-export from a package `__init__` — say so,
because the line is copied verbatim into somebody's workflow and a private path
being importable today is not a promise:

```python
PUBLIC = NodeSpec(
    id="custom.lead_score",
    category=NodeCategory.TRANSFORM,
    summary="Score a lead.",
    import_module="my_package",          # not my_package.nodes.scoring
)
```

---

## Step 5 — call it

```python
@workflow(name="qualify")
async def qualify(ctx: Context, blurb: str) -> str:
    scored = await ctx.node("custom.lead_score", ScoreIn(text=blurb))
    return "qualified" if scored.passed else f"skipped: {scored.reason}"


runtime = Runtime(store=MemoryStore())
```

A node call journals exactly what the equivalent hand-written code would, and
the body's own steps nest beneath it:

```text
0    step   node:custom.lead_score    node_id=custom.lead_score  contract=5060ab70
0.0  step   fetch_signals
```

Retries, timeouts, and guards are per call:

```python
async def guarded(ctx: Context, blurb: str) -> str:
    scored = await ctx.node(
        "custom.lead_score",
        ScoreIn(text=blurb),
        retry=3,
        guards=["guard.pii"],
    )
    return str(scored.passed)
```

---

## Step 6 — ship it

Registration is global by default. Two ways to reach a Runtime:

```python
get_node_catalog().register_node(LeadScoreNode)  # this process
```

or, from an installed package, an entry point — the same shape as
`loom_toolset`, and the one that makes a node installable:

```toml
[project.entry-points.loom_node]
lead_score = "my_package.nodes:LeadScoreNode"
```

Entry points load the first time a `Runtime` builds its node registry, so
importing `loom` does not import every installed node package.

To keep a node local to one Runtime — a project-specific node that should not
leak into another host in the same process:

```python
local = Runtime(store=MemoryStore())
local.nodes.register_node(LeadScoreNode)
```

where `LeadScoreNode` is the class from step 2.

---

## Step 7 — test it

Test the node directly. It is an ordinary object, and its body only needs a
context:

```python
import asyncio


@workflow(name="qualify_check")
async def qualify_check(ctx: Context, blurb: str) -> str:
    scored = await ctx.node("custom.lead_score", ScoreIn(text=blurb))
    return "qualified" if scored.passed else f"skipped: {scored.reason}"


async def check() -> None:
    runtime = Runtime(store=MemoryStore())
    runtime.register(qualify_check)
    result = await runtime.run(qualify_check, "Acme wants a demo for 40 seats")
    assert result.status.value == "completed"
    assert result.output == "qualified"


asyncio.run(check())
```

Then test the two things that are not about your logic:

**Its own example runs.** The built-in suite drives every node from
`spec.examples[0]`, so an untested node is a missing example — visible in the
docs the agent reads rather than only in a coverage report.

**Its contract renders as valid Python.** A field aliased to a keyword (`in`,
`from`, `class`) renders as a call that does not parse. This was a real defect
in `control.filter`, caught by compiling every rendered contract.

---

## Versioning

A node's `version` plus the hash of its two models is journaled with every call.
Change either model and a replay of an older run raises `ContractChanged`
instead of decoding an old payload into a new shape.

That is deliberate: the alternative is an upgrade quietly changing what a
finished run replays to, so the run appears to have done something it never did.
When you change a contract, bump `version` — and to replay old runs, pin the
version they were written against, or `runtime.retry()` them against current
code.

Adding an **optional output field** is still a contract change by this rule. It
is the case people expect to be safe, and it is exactly the one where a replay
would silently gain a field the original run never produced.

---

## Reference

| Concern | Where |
|---|---|
| Categories, the catalog, the prompt budget | `CLAUDE.md`, "Nodes" |
| Human-in-the-loop and `HumanChannel` | `loom.nodes.human` |
| Guardrail nodes and the four verdicts | `loom.nodes.guard` |
| The built-in library | `loom nodes` |
| Design and rationale | `docs/design/nodes-plan.md` |
