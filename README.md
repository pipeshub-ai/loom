# loomsdk

[License: MIT](LICENSE)
[Python 3.11+](https://www.python.org/downloads/)
[Tests](.github/workflows/ci.yml)

**There is no workflow builder. That's the point.**

LOOM is a library-first durable execution SDK for AI-powered workflows. You
describe what you want in a sentence. A coding agent — with your tools,
knowledge, and skills in front of it as one live capability catalog, not a
static doc — writes real Python: `if`/`else`, loops, parallel branches,
sub-agents, all of it. Every generation is compiled, linted, type-checked, run
against fakes, and checked for determinism before you see it.

The code *is* the workflow, and it's the only source of truth: it's saved for
reuse, so you generate once and run for free forever; it survives crashes and
resumes where it stopped, on a laptop or on Postgres, without a code change;
and when you need to change a workflow, you change the code — the diagram you
see is generated from it, not maintained beside it.

Every other workflow tool makes you choose between the drag-and-drop canvas a
non-engineer can use and the code a real system needs. LOOM gives you the
code, and generates the picture from it — not the other way around.

`pip install` and go — no infrastructure required.

> **This is a work in progress, not a finished product.** The core — durable
> execution, the coding agent, toolsets — is real, tested, and usable today.
> Larger parts of the vision (sandboxed execution, versioned source, agent-
> rendered visualization) are designed but not yet built; see
> [Project Status](#project-status) for exactly what that split is. If you
> want to help make this production-ready, that's what we're building it in
> the open for — see [Contributing](CONTRIBUTING.md).

## Quick Start

Describe the workflow in English. Get back Python that has been compiled,
linted, type-checked, executed against fakes, and checked for determinism —
then run it.

```python
import asyncio

from loom import Runtime
from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.providers import AnthropicProvider
from loom.stores.memory import MemoryStore


async def main():
    agent = WorkflowCodingAgent(AnthropicProvider())
    result = await agent.generate(
        "Take a URL string as input, fetch it, and report how many words "
        "the response contains."
    )
    print("verified:", result.is_clean)
    print(result.code)
    workflow = result.load()                   # import what it wrote
    rt = Runtime(store=MemoryStore())
    rt.register(workflow)
    run = await rt.run(workflow.name, "https://example.com")
    print("→", run.status.value, "|", run.output)


asyncio.run(main())
```

```
verified: True
<the generated file — @step for the fetch, @workflow for the body>
→ completed | Word count for https://example.com: 27
```

Needs `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` with `OpenAIProvider`), and takes
about half a minute. `result.is_clean` is the interesting part: it means the
code compiled, passed the static rules, **ran**, and produced the same result
twice — not that it parsed. See [Workflow Coding Agent](#workflow-coding-agent)
for what each stage checks.

## What durable means

No API key needed for this one. A workflow charges a card and books a seat; the
booking fails, and the retry must not charge the card again.

```python
import asyncio

from loom import Context, Runtime, step, workflow
from loom.stores.sqlite import SQLiteStore

attempts = 0


@step
async def charge_card(amount: float) -> str:
    print(f"  charging ${amount:.2f}")      # expensive — must happen once
    return "ch_123"


@step(retry=1)                              # no auto-retry, so the failure shows
async def book_seat(charge_id: str) -> str:
    global attempts
    attempts += 1
    print("  booking seat")
    if attempts == 1:
        raise RuntimeError("seat service timed out")   # a transient outage
    return "seat_4A"


@workflow(name="book_trip")
async def book_trip(ctx: Context, amount: float) -> str:
    charge = await ctx.step(charge_card, amount)
    seat = await ctx.step(book_seat, charge)
    return f"{seat}, paid with {charge}"


async def main():
    rt = Runtime(store=SQLiteStore("trips.db"))
    first = await rt.run(book_trip, 42.0)
    print("→", first.status.value)
    second = await rt.retry(first.run_id)          # resume, do not restart
    print("→", second.status.value, "|", second.output)


asyncio.run(main())
```

Save it as `trip.py` and run `python trip.py`.

```
  charging $42.00
  booking seat
→ failed
  booking seat            ← only this ran again
→ completed | seat_4A, paid with ch_123
```

`charging` **prints once.** The retry resumed from the journal: `charge_card`
had already completed, so its recorded result was served instead of re-running
it. That is the whole idea — the card is not charged twice, and you did not
write any code to make that true.

The journal is in `trips.db`, so this survives the process dying too. Kill it
between the two runs and the second one still picks up where the first stopped.

## Installation

```bash
# Core (MemoryStore + SQLite, zero infra)
pip install loomsdk

# With storage backends
pip install loomsdk[mongo]        # MongoDB (motor)
pip install loomsdk[postgres]     # PostgreSQL (asyncpg)

# With agent framework backends
pip install loomsdk[langchain]    # LangChain / LangGraph
pip install loomsdk[agno]         # Agno
pip install loomsdk[pydantic-ai]  # Pydantic AI

# Google Workspace service-account auth (Gmail/Calendar work without it)
pip install loomsdk[google]

# With FastAPI webhook server
pip install loomsdk[api]

# Command line and terminal UI
pip install loomsdk[cli]          # rich output
pip install loomsdk[tui]          # loom ui

# MCP server — drive workflows from Claude Code, Claude Desktop, Cursor
pip install loomsdk[mcp]

# Everything
pip install loomsdk[all]

# Development
pip install -e ".[dev]"
```



## Key Features


| Feature                                    | Description                                                                                                 |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| **No drag-and-drop, ever**                 | Workflows are generated code, not boxes and arrows. A model writes it; the verification pipeline is what makes that safe. |
| **Durable execution**                      | Every side effect is journaled. Crash on step 9? Resume at step 9, not step 1.                              |
| **Agent-native**                           | `ctx.agent("prompt")` calls any AI agent. LangChain, Agno, Pydantic AI — swap the backend, keep the code.   |
| **Cron triggers**                          | `@workflow(triggers=[Schedule("0 9 * * *")])` — fires automatically via TriggerDispatcher.                  |
| **Pluggable storage**                      | MemoryStore (tests) -> SQLite (dev) -> MongoDB/PostgreSQL (prod). Same code, different backend.             |
| **[Coding agent](#workflow-coding-agent)** | Describe a workflow in English, get runnable Python — verified by a seven-stage pipeline before you see it. |
| **Typed toolsets**                         | Gmail, Google Calendar, Jira, Confluence, web search — Pydantic models, lazy loading, auto-generated docs.  |
| **Workflow manager**                       | Agent-facing tools to list, run, schedule, and cancel workflows via natural language.                       |
| **[CLI + TUI](#command-line)**             | `loom run`, `loom runs`, `loom approve`, `loom ui`. Exit codes distinguish suspended from failed.           |
| **[MCP server](#mcp-server)**              | `loom mcp` — drive workflows from Claude Code, Claude Desktop, or Cursor.                                   |
| **[Paged reads](#paged-reads)**            | Search operations follow the API's pages and tell you whether they saw everything, so a page is never reported as a total. |
| **[Testable time](#testable-time)**        | A virtual clock — a four-minute timer or a 9am cron, tested in milliseconds.                                |
| **[Typed nodes](#typed-nodes)**            | Pydantic in, Pydantic out. Human approvals, guardrails, and a standard library — searchable, versioned, and yours to extend. |




## Architecture

```
@workflow + @step          ctx.step() / ctx.agent()        Journal + Store
 (your code)          -->   (durable operations)      -->   (crash-safe replay)
       |                          |                              |
       v                          v                              v
  WorkflowDefinition         Context API                ExecutionStore protocol
  TriggerSpec (cron)     step / sleep / agent / gather   Memory | SQLite | Mongo | Postgres
```

**Core loop**: Load execution record -> re-enter workflow body -> journal short-circuits completed work -> new work is executed and journaled -> if body completes: COMPLETED; if raises Suspend: SUSPENDED (scheduler resumes later).

## Workflow Coding Agent

The [Quick Start](#quick-start) shows the loop; this is what happens inside it.

The interesting part is not that a model writes Python. It is what happens
before you see it: **every generation is verified, and a failure is fed back for
repair.** Seven stages run cheapest-first and stop at the first blocking error.


| Stage      | Catches                                                                                                                                |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `compile`  | Syntax                                                                                                                                 |
| `static`   | Missing `@workflow`, I/O in the body, `datetime.now()`, a store chosen in code, an integration you don't have                          |
| `lint`     | Undefined names, unreachable code (ruff, skipped if absent)                                                                            |
| `types`    | A step called with the wrong arity, a wrong return type (mypy, skipped if absent)                                                      |
| `smoke`    | Actually runs it — against generated fakes and a faked clock, so a four-minute `ctx.sleep` costs nothing and no credentials are needed |
| `replay`   | Runs it twice and compares. Non-determinism the static rules cannot see                                                                |
| `critique` | A second model reviews durability and spec fidelity (opt-in)                                                                           |


`result.is_clean` means it compiled, validated, **ran**, and reproduced — not
that it parsed. Anything a stage finds is handed back as a repair instruction
carrying both the error and the spec, and a repair that doesn't reduce the error
count is discarded rather than accepted.

**It writes code for what's certain, and delegates what isn't.** Before
generating each node, the agent asks one question: *can I write a rule today
that is right for every input the spec allows?* Yes → plain Python and a
toolset call, deterministic and journaled like any other step. No, or unsure →
a `ctx.agent()` node — an invented constant is the tell that a rule shouldn't
exist: a keyword list, a regex over prose, a threshold nobody supplied.
`if "urgent" in subject.lower()` is a guess wearing the clothes of logic. The
classification comes back on `CodingResult.plan` (node, kind, why) rather than
staying in the prompt, so it's something you can check, not something you have
to trust. And the agent behind `ctx.agent()` isn't locked to LOOM's own loop —
swap in `BuiltInBackend`, or wrap LangChain, Agno, or Pydantic AI via
`AgentBackend` (see [Agent Backends](#agent-backends)) — the generated
workflow code reads identically either way.

**It resolves what your words refer to before writing code.** You name a person
and a status the way people do; the API matches account ids and its own
per-project vocabulary. A query built from your words returns zero rows *and no
error* — which reads as "nothing to do" when it means "no such name". So the
agent looks entities up while authoring, bakes the resolved id into the code
with the name beside it in a comment, and where a name is genuinely ambiguous
emits a `ctx.agent()` node to decide at run time rather than guessing.

```python
from loom import Runtime
from loom.agents.coding_agent import WorkflowCodingAgent
from loom.agents.providers import AnthropicProvider, OpenAIProvider
from loom.agents.supervisor import CodeSupervisor

rt = Runtime()

agent = WorkflowCodingAgent(
    AnthropicProvider(),
    tool_registry=rt.toolsets,      # what it may discover and call
    allowed_packages={"httpx"},     # what the target environment has
    # A different model reviews the result; one model reviewing itself
    # mostly agrees with itself.
    supervisor=CodeSupervisor(OpenAIProvider()),
)

print(agent.build_system_prompt().count("Available toolsets"))   # 1 — toolsets injected
print([stage.name for stage in agent._stages or []] or "default pipeline")
```

Toolsets load on demand: the prompt carries an index card per integration, and
operations, schemas, and examples are fetched only for the ones a task needs —
so adding integrations does not tax unrelated generations.

> **Maturity.** Generation quality varies with the spec. Simple, well-scoped
> workflows come back clean; an ambiguous entity reference may still take
> several attempts or resolve to the wrong candidate. The verification pipeline
> is what makes that safe to iterate on — you find out before the code runs.

See `examples/cookbook/07_coding_agent.py`, and `09_jira_cli.py --debug` to
watch every tool call it makes while resolving.

## Typed nodes

A `@step` is your function, journaled. A **node** is a shareable contract —
Pydantic in, Pydantic out — that the coding agent can find, render the exact
call for, and check before anyone runs it.

```python
import asyncio

from loom import Context, Runtime, workflow
from loom.nodes.human import ApprovalIn, LogChannel
from loom.stores import MemoryStore


@workflow(name="refund")
async def refund(ctx: Context, order: dict) -> str:
    decision = await ctx.node("human.approval", ApprovalIn(
        subject=f"refund-{order['id']}",
        prompt=f"Approve a ${order['amount']} refund?",
        assignees=["finance@acme.com"],
        timeout=86400,
    ))
    return "sent" if decision.approved else "held"


async def main():
    rt = Runtime(store=MemoryStore(), human=LogChannel())
    rt.register(refund)
    parked = await rt.run(refund, {"id": "4821", "amount": 420})
    print(parked.status.value)                      # suspended — costs nothing
    await rt.approve(parked.run_id, "refund-4821")  # a person answers
    done = await rt.resume(parked.run_id)
    print(done.output)                              # sent


asyncio.run(main())
```

Save it as `refund.py` and run `python refund.py`.

```
suspended
sent
```

Twenty-one built in, across seven categories:

| Category | What it covers |
|---|---|
| `human` | approval, choice, form, review_edit, escalate |
| `guard` | schema, policy, pii, budget, content |
| `control` | switch, filter, dedupe, batch, throttle |
| `transform` | map_fields, template, extract, join, redact |
| `io` | http_request, wait_for_webhook |
| `agent` | classify, extract_structured, summarize, judge |
| `custom` | whatever you register |

`control` and `agent` are separate on purpose. `control.switch` is a rule you
can write today; `agent.classify` is judgement. Keeping them apart makes
choosing between them a decision rather than an accident.

**A node adds no durability semantics.** `ctx.node()` journals exactly what the
equivalent hand-written code would, and the body's own steps nest beneath it:

```text
0    step   node:custom.sla_check
0.0  step   sla_window
```

### Human-in-the-loop, without the silence

`ctx.wait_for_approval` parks a run correctly and tells nobody. The request
exists only as a journal entry somebody has to go looking for, which in practice
means it is found a day late.

LOOM owns parking the run, journaling the request, and validating the answer.
**Delivering it to a person is the provider's** — implement `HumanChannel` and
pass `Runtime(human=...)`. `HumanRequest` carries the JSON Schema of the answer,
so a Slack, email, or web provider renders a form from the request rather than
special-casing node ids.

```
$ loom pending
run                             subject  asked of      delivered  prompt
run_01M02PB1TS6K1P978375TZ2JC2  refund   fin@acme.com  no (log)   Approve $420.0?
  loom respond run_01M02PB1TS6K1P978375TZ2JC2 refund --approve
```

Note `no (log)` — the default channel records requests without claiming to have
delivered them. A run parked with nobody listening is indistinguishable from a
patient one, so the surface says which it is.

Delivery runs inside a durable call, so it happens **exactly once per request
across replays**: a restart does not re-ping the same person.

### Guardrails, anywhere

The four verdicts — ALLOW, REJECT, REPLACE, TRIPWIRE — are unchanged. What is
new is that they attach to more than an agent's tool calls:

```
clean = await ctx.guard("guard.pii", PiiIn(value=draft, redact=True))   # REPLACE
await ctx.node("io.http_request", request, guards=["guard.policy"])     # around a node
```

Outside an agent loop **REJECT raises**. There is nobody to hand the
explanation to, and a falsy verdict a caller could ignore would let the guarded
work proceed anyway. A guard that *raises* is treated as a tripwire, never an
allow — a check that could not run has found nothing.

### Writing your own

One file. `@register_node` derives the schemas, the import line, and the
rendered call from the class, so nothing is declared twice:

```
@register_node
class SlaCheckNode(Node[SlaIn, SlaOut]):
    spec = NodeSpec(id="custom.sla_check", category=NodeCategory.CONTROL,
                    summary="Has this ticket breached its SLA?")
    Input, Output = SlaIn, SlaOut

    async def run(self, ctx, payload: SlaIn) -> SlaOut:
        window = await ctx.step(sla_window, payload.plan)
        return SlaOut(breached=payload.opened_hours_ago > window)
```

Ship it by entry point (`[project.entry-points.loom_node]`) and it reaches every
Runtime. `docs/guides/nodes.md` is the walkthrough; every snippet on it runs in
CI, and `tests/test_node_guide.py` builds the node by following it.

### What the coding agent sees

The prompt carries **category headers and counts — never the node list**, so
registering the five-hundredth custom node lengthens no prompt. Detail arrives
on demand:

| Tool | Returns |
|---|---|
| `search_nodes(query, category=…)` | matching nodes; an empty query with a category lists it |
| `show_node(id)` | schemas, examples, effect, requirements |
| `node_contract(id)` | **the code to write** |

That last one is the difference that matters. A schema is a *description of* a
call, and the agent's next action is to write one — so it gets the invocation
instead:

```
$ loom node human.approval
# human.approval  v1.0.0  [human]   suspends: yes   effect: write   requires: human_channel

from loom.nodes.human import ApprovalIn, ApprovalOut

result: ApprovalOut = await ctx.node(
    'human.approval',
    ApprovalIn(
        subject='refund-4821',                           # str — Identifies this decision within the run.
        prompt='Approve a $420 refund for order 4821?',  # str, optional — What the person is being asked.
        assignees=['finance@acme.com'],                  # list[str], optional — Who is being asked.
        timeout=86400,                                   # float | int | timedelta | None, optional
    ),
)

# ApprovalOut: approved: bool, responder: str, comment: str, decided_at: datetime | None
```

Rendered from the node's own models, so it cannot drift from the code.

See `examples/cookbook/20_human_nodes.py`, `21_guardrail_nodes.py`, and
`22_custom_nodes.py`.

## Paged reads

Hosted APIs cap a page below what you ask for, and none of them call it an
error: ask Jira for 500 issues, get 100, with a 200 OK. A workflow reporting
`f"{len(rows)} found"` on that is wrong in the way that survives review — the
number is real and only the framing lies.

A search returns an ordinary list that also knows whether it saw everything:

```python
import asyncio

from loom import Context, Runtime, step, workflow
from loom.stores import MemoryStore
from loom.toolsets.pagination import Page, Results, collect

ROWS = [f"BUG-{i}" for i in range(312)]


@step
async def search_bugs(max_results: int = 200, cursor: str | None = None) -> Results[str]:
    """A stand-in for jira_search_issues — same shape, no credentials."""
    async def fetch(at: str | None, size: int) -> Page:
        start = int(at or cursor or 0)
        batch = ROWS[start : start + min(size, 100)]   # the server caps at 100
        nxt = start + len(batch)
        return Page(batch, str(nxt) if nxt < len(ROWS) else None, total=len(ROWS))
    return await collect(fetch, limit=max_results, page_size=100)


@workflow(name="open_bugs")
async def open_bugs(ctx: Context, limit: int = 200) -> str:
    """Bounded: one call, and say what it covers."""
    issues = await ctx.step(search_bugs, max_results=limit)
    if not issues.complete:
        return f"showing {issues.summary()}"
    return f"all {len(issues)}"


@workflow(name="drain_bugs")
async def drain_bugs(ctx: Context, _: object = None) -> str:
    """Unbounded: one page per step, resuming where the last run stopped."""
    cursor = await ctx.state.get("cursor")
    page = await ctx.step(search_bugs, max_results=100, cursor=cursor)
    await ctx.state.set("cursor", page.cursor)
    return f"{len(page)} rows, next cursor {page.cursor}"


async def main() -> None:
    rt = Runtime(store=MemoryStore())
    rt.register_all([open_bugs, drain_bugs])
    print((await rt.run(open_bugs, 200)).output)   # showing 200 of 312
    print((await rt.run(open_bugs, 400)).output)   # all 312
    print((await rt.run(drain_bugs)).output)       # 100 rows, next cursor 100
    print((await rt.run(drain_bugs)).output)       # 100 rows, next cursor 200


asyncio.run(main())
```

`.complete` survives being returned from a step, so it reads the same on a
replay. For a set with no natural bound — a mailbox, an audit log — raising the
limit is the wrong answer: one call for 50,000 rows is a single journal entry
that a crash refetches whole. One page per step is journaled per page, and the
cursor in `ctx.state` outlives the run.

Each page is its own journal entry, so a crash resumes at the page it died on.
[`19_pagination.py`](examples/cookbook/19_pagination.py) runs both patterns end
to end.

Writing a toolset? Return `Results[T]` and you are done — the manifest flag, the
docs the coding agent reads, and the build check all derive from that one
annotation. See the [toolsets guide](docs/guides/toolsets.md).

## Testable time

A durable workflow is mostly a thing that waits, and the waiting is the part
worth testing. `ManualClock` is the Runtime's clock, so moving it moves
everything that reads time — `ctx.sleep`, cron schedules, retry backoff:

```python
import asyncio
from datetime import UTC, datetime

from loom import Context, Runtime, workflow
from loom.runtime.clock import ManualClock
from loom.stores import MemoryStore
from loom.testing import advance


@workflow(name="reminder")
async def reminder(ctx: Context, _: object = None) -> str:
    """Wait four minutes, then act."""
    await ctx.sleep(240)
    return "reminded"


async def main() -> None:
    clock = ManualClock(datetime(2026, 3, 2, 9, 0, tzinfo=UTC))
    rt = Runtime(store=MemoryStore(), clock=clock)
    rt.register(reminder)
    parked = await rt.run(reminder)      # parks on a four-minute timer
    await advance(rt, minutes=5)         # ...which has now already happened
    print((await rt.get(parked.run_id)).output)


asyncio.run(main())
```

`advance()` moves the clock, ticks the schedulers, and waits for the runs it
started — the three steps "let five minutes pass" has to mean.

## Talking while it works

A run that takes four minutes has nothing to say for four minutes unless you
ask it to:

```python
import asyncio

from loom import Context, Runtime, workflow
from loom.stores import MemoryStore


@workflow(name="indexer")
async def indexer(ctx: Context, _: object = None) -> str:
    """Say what it is doing, and remember where it stopped."""
    await ctx.report("fetching page 1")  # visible in loom watch, MCP, and HTTP
    seen = await ctx.state.get("seen", default=0)  # survives across runs
    await ctx.state.set("seen", seen + 1)
    return f"run number {seen + 1}"


async def main() -> None:
    rt = Runtime(store=MemoryStore())
    rt.register(indexer)
    print((await rt.run(indexer)).output)          # run number 1
    second = await rt.run(indexer)
    print(second.output)                           # run number 2
    print([r.message for r in rt.stream.since(second.run_id)])


asyncio.run(main())
```

`ctx.publish(name, payload)` broadcasts an event to whoever is waiting.
(`ctx.emit` did both jobs and is now a deprecated alias for `publish`.)

## Command Line

```bash
pip install "loomsdk[cli]"
```

```bash
loom run onboard --input '{"email": "a@b.com"}'   # or -i @payload.json
loom run onboard --follow                          # stream steps as they finish
loom runs --status failed
loom show <run> / loom watch <run>

loom approve <run> refund [--reject]               # unpark a human-gated run
loom send <run> <event> '{"token": "x"}'
loom cancel / retry / replay <run>

loom check flows/order.py       # write order.graph.json + order.description.md
loom serve --port 8000          # HTTP API
loom ui                         # terminal UI, needs [tui]
```

**Exit codes are the contract:** `0` completed, `1` failed, `2` usage,
`3` **suspended**, `4` cancelled. A run parked on a human has neither succeeded
nor failed, and collapsing it into either makes calling scripts do the wrong
thing. `--json` on every command; `--server URL` runs any of them against a
remote LOOM instead of importing locally.

## MCP Server

Drive workflows from Claude Code, Claude Desktop, or Cursor.

```bash
pip install "loomsdk[mcp]"
claude mcp add loom -- loom mcp --module flows.py
```

Ten tools — list, run, inspect a journal, approve, send an event, cancel, retry,
replay — plus resources and prompts. `--transport http` for networked clients.

Two things it does that a thin wrapper would not: a **suspended** run comes back
with what it is waiting for and the exact call that unparks it, plus a note that
suspended is not failure; and the difference between `retry_run` (from the
failure, current code) and `replay_run` (from the journal, no side effect
repeated) is spelled out, because a model reliably guesses wrong.

See [docs/guides/mcp.md](docs/guides/mcp.md).

## Agent Backends

The workflow code is framework-agnostic. The agent framework is configured on the Runtime:

```python
import asyncio

from langchain_anthropic import ChatAnthropic

from loom import Context, Runtime, workflow
from loom.agents.backends.langchain import LangChainBackend
from loom.stores.memory import MemoryStore

rt = Runtime(
    store=MemoryStore(),
    agent_backend=LangChainBackend(llm=ChatAnthropic(model="claude-sonnet-5")),
)


# Workflow code — no LangChain imports
@workflow(name="research")
async def research(ctx: Context, query: str) -> str:
    result = await ctx.agent(f"In one sentence, what is {query}?")
    return result.output


async def main():
    rt.register(research)
    print(await rt.run("research", "durable execution"))


asyncio.run(main())
```

```
<ExecutionResult run=run_01K… workflow='research' completed
 output='Durable execution is a programming model that persists a workflow…' 1 step 61 tokens>
```

Swap `LangChainBackend` for `AgnoBackend` or `PydanticAIBackend` and the
workflow above does not change — that is the point of the layer.

Available backends: `LangChainBackend`, `AgnoBackend`, `PydanticAIBackend`, `BuiltInBackend`, or implement your own `AgentBackend` protocol.

## Storage Backends

```python
from loom import Runtime

# Development (zero infra)
from loom.stores.memory import MemoryStore
rt = Runtime(store=MemoryStore())

# Local persistence
from loom.stores.sqlite import SQLiteStore
rt = Runtime(store=SQLiteStore("workflows.db"))

# Production (MongoDB) — connecting is async, so inside an async function
from loom.stores.mongo import MongoStore

async def mongo_runtime() -> Runtime:
    store = MongoStore("mongodb://localhost:27017", database="workflows")
    await store.ensure_indexes()
    return Runtime(store=store)

# Production (PostgreSQL)
from loom.stores.postgres import PostgresStore

async def postgres_runtime() -> Runtime:
    store = PostgresStore("postgresql://user:pass@localhost/workflows")
    await store.connect()
    return Runtime(store=store)


# The workflow never knows which of these it is running on.
for runtime in (Runtime(store=MemoryStore()), Runtime(store=SQLiteStore("workflows.db"))):
    print(type(runtime.store).__name__)
```

```
MemoryStore
SQLiteStore
```



## Trigger System

```python
import asyncio
from datetime import UTC, datetime, timedelta

from loom import Context, Runtime, workflow
from loom.runtime.clock import ManualClock
from loom.runtime.dispatcher import TriggerDispatcher
from loom.stores.memory import MemoryStore
from loom.triggers.specs import Schedule


@workflow(name="daily_report", triggers=[Schedule("0 9 * * 1-5")])
async def daily_report(ctx: Context, _: None = None) -> str:
    return "report generated"


async def main():
    # A ManualClock, so this reads the same on any day at any hour. register()
    # computes the next fire from the runtime's clock, so on the real clock this
    # example printed nothing whenever you happened to run it after 09:00.
    monday_9am = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    rt = Runtime(store=MemoryStore(), clock=ManualClock(monday_9am - timedelta(hours=1)))
    dispatcher = TriggerDispatcher(rt)
    await dispatcher.register(daily_report)
    # tick() submits whatever is due. Pass a time to see it fire without
    # waiting until Monday; in production start_scheduler() ticks for you.
    for run_id in await dispatcher.tick(now=monday_9am):
        result = await rt.resume(run_id)
        print(result.status.value, "|", result.output)


asyncio.run(main())
```

```
completed | report generated
```



## Cookbook Examples


| #   | Example                                                            | Pattern                                                  |
| --- | ------------------------------------------------------------------ | -------------------------------------------------------- |
| 01  | [Sequential pipeline](examples/cookbook/01_sequential.py)          | Step chaining, Retry                                     |
| 02  | [Parallel fan-out](examples/cookbook/02_parallel.py)               | ctx.gather()                                             |
| 03  | [Durable sleep](examples/cookbook/03_durable_sleep.py)             | ctx.sleep(), scheduler                                   |
| 04  | [Error handling](examples/cookbook/04_error_handling.py)           | Retry, OnError.ROUTE, Failure                            |
| 05  | [Human-in-the-loop](examples/cookbook/05_human_in_the_loop.py)     | ctx.wait_for_event(), send_event                         |
| 06  | [AI agent step](examples/cookbook/06_ai_agent_step.py)             | ctx.agent() with Claude                                  |
| 07  | [Coding agent](examples/cookbook/07_coding_agent.py)               | NL spec -> generated, verified workflow                  |
| 08  | [Jira agent](examples/cookbook/08_jira_agent.py)                   | Jira toolset + coding agent                              |
| 09  | [Jira CLI](examples/cookbook/09_jira_cli.py)                       | Coding agent end to end; `--debug` shows every tool call |
| 10  | [LangChain backend](examples/cookbook/10_langchain_react_agent.py) | LangChain ReAct via AgentBackend                         |
| 11  | [Agno backend](examples/cookbook/11_agno_backend.py)               | Agno agent via AgentBackend                              |
| 12  | [Pydantic AI backend](examples/cookbook/12_pydantic_ai_backend.py) | Pydantic AI via AgentBackend                             |
| 13  | [Cron trigger](examples/cookbook/13_cron_trigger.py)               | Schedule-based dispatch                                  |
| 14  | [Workflow manager](examples/cookbook/14_workflow_manager_cli.py)   | Agent that manages workflows                             |
| 15  | [Queue consumer](examples/cookbook/15_queue_consumer.py)           | At-least-once ingress, exactly-once runs                 |
| 16  | [HTTP server](examples/cookbook/16_http_server.py)                 | create_app() + LoomClient                                |
| 17  | [Files and artifacts](examples/cookbook/17_files_and_artifacts.py) | Attachment, blobs, versioned artifacts                   |
| 18  | [Gmail and Calendar](examples/cookbook/18_gmail_calendar.py)       | Google toolsets, approval before send                    |
| 19  | [Pagination](examples/cookbook/19_pagination.py)                   | Paged reads: bounded coverage, unbounded page-per-step   |




## Project Status

**LOOM is a work in progress.** It is a real, tested library today — not a
demo — but it is not the finished thing, and it says so on purpose rather
than papering over the gaps:

| Area | Status |
|---|---|
| Durable execution: journal, replay, retry vs. replay, suspension | **Shipped, tested** |
| Workflow Coding Agent: 7-stage verification, entity resolution, code-or-judgement classification | **Shipped, tested** |
| Typed toolsets (Gmail, Calendar, Jira, Confluence), pluggable agent backends | **Shipped, tested** |
| CLI, TUI, MCP server, HTTP API over one `RuntimeFacade` | **Shipped, tested** |
| Sandboxed execution (generated code runs with no ambient credentials) | **Shipped, tested** — `ExecutionSandbox` (`InlineSandbox`, `SubprocessSandbox`, `DockerSandbox`), see [implementation plan](docs/design/implementation-plan.md) §3.1 |
| Versioned, activatable workflow source (commit/rollback, not just a code hash) | **Designed, not built** — `SourceStore`/`VersionStore`, see [implementation plan](docs/design/implementation-plan.md) §4 |
| Session-shaped execution traces for debugging | **Designed, not built** — `TraceView`, same doc |
| Agent-rendered visualization, verified against the extracted graph | **Designed, not built** — see [`phases/phase-4-visualization.md`](phases/phase-4-visualization.md) |

The honest read: what's shipped is solid enough to build on for a laptop, a
side project, or a host that isolates generated code behind `DockerSandbox`.
What's designed-but-not-built is versioned source, session-shaped traces, and
agent-rendered visualization — not the isolation boundary. A host running
untrusted, model-generated code should construct `Runtime(sandbox=DockerSandbox(...))`
rather than the default `InlineSandbox`.

**This is where the community makes the difference.** The gaps above are
scoped, written down, and ordered by dependency (see
[`docs/design/implementation-plan.md`](docs/design/implementation-plan.md) §4 — each phase
lists its own exit criteria as tests) precisely so they're contributable, not
just aspirational. If any of this is what you need, a PR against one of those
phases is the fastest way to get it. See [Contributing](CONTRIBUTING.md).

## FAQ

**How is the Workflow Coding Agent different from a normal coding agent?**
A normal coding agent writes code once for a human to commit and run by hand.
This one writes code that runs inside a durable runtime: journaled and
resumable (a crash retries the failed step, not the whole run), reusable by
construction (it's a `@workflow`, callable forever), generated once and run
free forever (the LLM call is at authoring time, not every run), and
triggerable (`Schedule("0 9 * * *")`, a webhook, a new email) without extra
infrastructure. A coding agent writes code for a human to run once; this one
writes code for a runtime to run forever.

**Why not use a no-code / drag-and-drop workflow builder?** No-code exists
because people couldn't reliably write code — and a canvas is safe by
construction because it can't do anything the palette didn't intend. Both
premises are getting weaker: coding agents write correct, idiomatic code
reliably for well-scoped tasks today, and improve every model generation. A
loop, a few nested conditionals, a sub-workflow call, or mixing a rule and a
judgement call in the same function is native to code and a fight on most
canvases. You don't lose the picture — LOOM projects a verified graph from
the code, so you get the diagram and the `for` loop instead of one at the
cost of the other.

More questions — production readiness, safety of generated code, vendor
lock-in — are answered in the [full FAQ](docs/faq.md).

## Development

```bash
git clone https://github.com/pipeshub-ai/loom.git
cd loom
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check src tests

# Type check
mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License. See [LICENSE](LICENSE) for details.

Built by [PipesHub AI](https://pipeshub.com).