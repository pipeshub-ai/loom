# FAQ

## How is the Workflow Coding Agent different from a normal coding agent?

A normal coding agent (Cursor, Copilot, Claude Code) writes code once, in an
editor, for a human to review and commit — after that it's out of the
picture. Whatever runs that code afterward — a cron job, a script, a service
someone stands up — is a separate thing someone has to build. The Workflow
Coding Agent writes code that runs *inside* a purpose-built durable runtime,
so a list of things come with the generated file that a normal coding
agent's output does not get automatically:

| | Normal coding agent | Workflow Coding Agent |
|---|---|---|
| **Durable** | Crash mid-script, and you lost the work | Every `ctx.step`/`ctx.agent`/`ctx.sleep` is journaled — a crash loses nothing |
| **Resume, not restart** | A crashed script starts over from line 1 | `runtime.retry(run_id)` re-enters from the journal; completed steps are served from their recorded result, not re-run |
| **Reusable by construction** | Output is a one-off snippet someone wraps by hand | The output *is* a `@workflow` — call it with new inputs forever, no re-authoring |
| **Generate once, run free forever** | Re-running means asking the model again | The LLM call happens once, at authoring time; every run after that executes the saved Python — no inference cost, no risk the model behaves differently on run #500 than run #1 |
| **Triggerable** | No notion of "run this automatically" | `@workflow(triggers=[Schedule("0 9 * * *")])` fires every morning; `Webhook`/`OnEvent`/`EmailInbox` fire off a webhook or a new email, with no extra infrastructure |
| **Verified before it runs** | Type-checks, maybe | A 7-stage pipeline — compile, static, lint, types, smoke, replay, critique — actually **runs** it against fakes and checks it reproduces, before it ever sees production |

One line: **a coding agent writes code for a human to run once; the Workflow
Coding Agent writes code for a runtime to run forever.** See
[Quick Start](../README.md#quick-start) and [What durable
means](../README.md#what-durable-means) in the README for both halves of that
running.

## Why not use a no-code / drag-and-drop workflow builder?

Because the premise that justified one — "business users can't write code, so
give them boxes and arrows instead" — is going stale, and it was never free:
a box-and-arrow canvas can't express a loop cleanly, buries a few nested
conditionals in a maze of boxes, and turns a mildly complex workflow into
something genuinely harder to read than the fifteen lines of Python it stands
in for.

What changed is the excuse, not the workflow. No-code existed because
*people* couldn't reliably write code. Coding agents now can — for
well-scoped tasks, today's frontier models write correct, idiomatic code
reliably, and each generation gets meaningfully better at it. The bottleneck
no-code was built to work around has moved. Someone who can describe what
they want in a sentence no longer needs a palette of pre-validated boxes
standing between them and a `for` loop — they need a system that turns the
sentence into code, and verifies it before it runs, which is the part a
canvas gets for free by being unable to do anything unintended and code has
to earn.

Concretely, what falls out once the workflow is real code instead of a graph
of boxes:

- **Real control flow.** "For each open PR older than 3 days with no
  reviewer, ping the author unless they're on-call" is one `for` and two
  nested `if`s. On most canvases it's a small maze of boxes, because nesting
  conditionals is exactly what a flat 2D canvas is bad at.
- **Branching a palette can't pre-enumerate.** A node that picks a different
  tool depending on data it hasn't seen yet is a runtime `if` in code. On a
  canvas it's a pre-wired branch per case someone anticipated in advance —
  and a case nobody anticipated has nowhere to go.
- **Reuse and composition.** A sub-workflow is a function call
  (`await ctx.child(other_workflow, ...)`). Most no-code tools either
  duplicate the sub-flow's boxes inline or bolt on a special "sub-flow" node
  type with its own rules.
- **Deterministic and probabilistic mixed freely, at the granularity of one
  line.** `if` for the part you can write a rule for, `ctx.agent()` for the
  part you can't, in the same function — see [Code or
  judgement](guides/coding-agent.md#code-or-judgement). No-code tools
  generally have "the AI step" as one opaque box you cannot decompose
  further.

None of this makes the picture go away — it makes the picture *generated
from* the code instead of being the only way to author it (see [Project
Status](../README.md#project-status)). You get the diagram and the `for`
loop, instead of one at the cost of the other.

## Is LOOM ready for production?

Parts of it, yes; not the whole vision yet — and this is written down rather
than glossed over in the README's [Project
Status](../README.md#project-status). Durable execution, the coding agent's
verification pipeline, typed toolsets, and the CLI/MCP/HTTP surfaces are
shipped and tested. What isn't built yet is the part that matters most for
running **untrusted, model-generated code** at scale used to be sandboxed
execution. That port is now `ExecutionSandbox`: `InlineSandbox` (default,
no isolation), `SubprocessSandbox` (rlimits, no network isolation), and
`DockerSandbox` (`--network none`, cgroup memory, read-only root). If your
workflows are authored by people you trust and run in an environment you
control, the default Runtime is enough. If you plan to let arbitrary users
generate and run workflows unsupervised, construct
`Runtime(sandbox=DockerSandbox(image=...))` — see
[Contributing](../CONTRIBUTING.md).

## What stops the coding agent from generating wrong or unsafe code?

The same thing that makes "an LLM writes my workflow" survivable at all: **it
is verified, not trusted.** Every generation runs a 7-stage pipeline —
compile, static analysis, lint, types, a smoke run against generated fakes, a
replay-and-compare for determinism, and an optional second-model critique —
and a failure at any stage feeds back into a repair loop rather than shipping.
`result.is_clean` means it compiled, validated, **ran**, and reproduced, not
that it merely parsed. Separately, the agent classifies every node as a rule
(if it can state one that's right for every input) or a judgement call (if
not) and reports that classification on `CodingResult.plan`, so a reviewer can
check whether a keyword list or a hardcoded threshold snuck in disguised as
logic. See [Workflow Coding Agent](../README.md#workflow-coding-agent).

## Does this lock me into one LLM or one agent framework?

No, on both counts. The model that authors workflows is behind
`ModelProvider` — `AnthropicProvider`, `OpenAIProvider`, `GeminiProvider`
ship built in, and any OpenAI-compatible endpoint (Azure, Together, Groq,
vLLM, Ollama) works through `OpenAIProvider(base_url=...)`. The agent that
`ctx.agent()` delegates to *inside* a generated workflow is a separate,
equally swappable choice: `BuiltInBackend` runs LOOM's own loop, or wrap
LangChain, Agno, or Pydantic AI via `AgentBackend`. Neither choice touches
the generated workflow code — see [Agent Backends](../README.md#agent-backends).

## How is this different from Temporal / Prefect / Airflow?

loomflow is **library-first**: `pip install loomflow` and you have a working runtime with no external services. Temporal requires a cluster. Prefect and Airflow require a server. loomflow runs embedded with SQLite or in-memory, and you can later swap in MongoDB/PostgreSQL for production without changing workflow code.

It is also **agent-native**: `ctx.agent()` is a first-class durable operation. Agent turns are journaled and replayed like any other step. Other orchestrators bolt on LLM support after the fact.

## Can I use my own LLM provider?

Yes. Implement the `ModelProvider` protocol from `loom.agents.models`:

<!-- docs-preamble -->

Every example on this page assumes:

```python
from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore


@step
async def request_approval(item: str) -> str:
    """Stand-in for the real work."""
    return item


@step
async def proceed(item: str) -> str:
    """Stand-in for the real work."""
    return item


@workflow(name="my_workflow")
async def my_workflow(ctx: Context, data: str) -> str:
    return data
```

```python
class MyProvider:
    async def chat(self, messages, tools=None, **kwargs):
        # Call your model, return a ChatResponse
        ...
```

The built-in provider (`AnthropicProvider`) supports Claude models. Pricing is configured in `agents/models.py::PRICING`.

## How does durable replay work?

Every side effect that goes through `ctx.step()`, `ctx.sleep()`, `ctx.wait_for_event()`, or `ctx.agent()` is recorded in a journal. If the process crashes or redeploys, the runtime re-enters the workflow body from the top. The journal short-circuits completed operations by returning the recorded result instead of re-executing. This guarantees exactly-once semantics for side effects.

## Can I run this in production?

Yes. Use `MongoStore` or `PostgresStore` for durable storage:

```python
from loom.stores.mongo import MongoStore

runtime = Runtime(store=MongoStore("mongodb://localhost:27017/workflows"))
```

See the [Deployment Guide](deployment.md) for Docker and environment variable configuration.

## How do I add a custom tool for the agent?

Option 1 -- make a step available as a tool:

```python
from loom import step

@step
async def lookup_user(email: str) -> dict:
    """Look up a user by email.

    Args:
        email: The user's email address.
    """
    # ... your logic
    return {"name": "Alice", "role": "admin"}
```

Steps decorated with `@step` can be passed directly to agent backends as tools.

Option 2 -- register a toolset:

```python
from loom.toolsets.manifest import ToolsetManifest, OperationSpec
from loom.toolsets.registry import register_toolset

manifest = ToolsetManifest(
    id="my_tools",
    version="1.0.0",
    summary="My custom tools",
    groups={"users": [OperationSpec(id="users.lookup", summary="Look up user")]},
)
register_toolset(manifest)
```

## How do I test workflows?

Use `MemoryStore` -- it requires no external services:

```python
import pytest
from loom import Runtime
from loom.stores.memory import MemoryStore

@pytest.mark.asyncio
async def test_my_workflow():
    runtime = Runtime(store=MemoryStore())
    result = await runtime.run(my_workflow, {"input": "test"})
    assert result.status == "COMPLETED"
    assert result.output == "expected"
```

## What happens if my workflow raises an exception?

If an unhandled exception propagates from the workflow body, the run is marked `FAILED`. You can configure retry at the step level:

```python
from loom import Retry

@step(retry=Retry(max_attempts=3, initial_delay=1.0, multiplier=2.0))
async def flaky_api_call(url: str) -> dict:
    ...
```

You can also use `runtime.retry(run_id)` to retry a failed run from the last successful journal entry.

## Can I run workflows on a schedule?

Yes. Use the `Schedule` trigger with cron syntax:

```python
from loom.triggers.specs import Schedule

@workflow(name="daily_report", triggers=[Schedule(cron="0 9 * * *")])
async def daily_report(ctx: Context) -> str:
    ...
```

See the [Triggers Guide](guides/triggers.md) for all trigger types.

## How do I pause a workflow and wait for human input?

Use `ctx.wait_for_event()`:

```python
@workflow(name="approval")
async def approval(ctx: Context) -> str:
    await ctx.step(request_approval, "Please review")
    event = await ctx.wait_for_event("approval_decision")
    if event["approved"]:
        await ctx.step(proceed)
    return "done"
```

Resume it externally:

```python
async def approve(runtime, run_id):
    await runtime.resume(run_id, event={"approved": True})
```
