# Grants, state, and progress

Three things a workflow gains once it does more than call a function and return:
a bound on what it may invoke, a memory that outlives one run, and a way to say
what it is doing while it does it.

All three are opt-in. A bare `Runtime()` enforces nothing, remembers nothing
beyond the journal, and says nothing — which is what an embedded single-process
Runtime wants.

<!-- docs-preamble -->

```python
import asyncio

from loom import Context, Runtime, step, workflow
from loom.runtime.effects import DirectBroker, GuardedBroker
from loom.security.authority import Authority
from loom.security.grants import GrantSet
from loom.stores import MemoryStore
```

## The effect broker

Every durable operation — every step, agent call, child workflow, and tool
invocation — passes through an `EffectBroker`. The default performs them and
checks nothing:

```python
print(Runtime().broker)
```

The seam is always there rather than something you switch on, because a seam
you opt into gets bypassed. It costs about two microseconds per dispatch,
against a step doing real I/O.

Swap in a `GuardedBroker` to enforce an `Authority` on **every dispatch** — not
once when tools were resolved. That distinction is the whole point: a grant
checked at resolution can be outlived by the tool it produced, and an agent that
kept a tool object keeps the permission with it.

```python
async def guarded() -> None:
    runtime = Runtime(
        store=MemoryStore(),
        broker=GuardedBroker(max_calls=50),
        authority=Authority(grant=GrantSet(toolsets=["jira.issues:read"])),
    )
    print(runtime.broker, runtime.authority.grant.toolsets)


asyncio.run(guarded())
```

That authority can call `jira.issues.search` and cannot call
`jira.issues.create`. The refusal says so, and says what would have allowed it:

```text
'jira.issues.create' is not granted. Held: jira.issues:read.
Add 'jira.issues.create:write' to the grant to allow it.
```

An agent gets that back as a tool result rather than an exception, so it can
pick another route and the transcript records why.

### The three checks

- **Dry run** — `Authority(dry_run=True)` refuses writes and performs reads, so a
  rehearsal runs against real data rather than mocks.
- **Call ceiling** — `GuardedBroker(max_calls=N)` stops a runaway loop. Refusals
  do not count against it, so a run cannot exhaust its own budget by being
  denied.
- **Grant** — the authority's `GrantSet`, checked per call.

**An empty grant set permits everything.** Deny-by-default applies *within* a
dimension you have spoken to: declare `toolsets` and every unlisted toolset is
refused. Declaring nothing is not the same as declaring that nothing is
permitted, and reading it that way would break every workflow the moment a
broker was configured.

A workflow can narrow itself with `@workflow(grants=...)`, and that applies when
the Runtime declared nothing. It can never widen the Runtime's authority — a
declaration that could grant itself more would be worthless.

### Dispatch is per operation, not per attempt

A step that retries three times is one effect. Retries are the retry policy's
business; a denial is not retryable, and counting attempts against a ceiling
would make a flaky upstream look like a runaway loop.

Replay dispatches nothing at all: journaled results are served from the journal,
so a rehearsal costs no permissions either.

### Writing a broker

```python
class AuditingBroker:
    """Log every effect, then let it through."""

    async def dispatch(self, call, authority):
        print(f"{call.kind} {call.target}")
        return await DirectBroker().dispatch(call, authority)


print(AuditingBroker())
```

`call.describe()` is the serialisable projection — everything a broker needs to
decide, with none of the local machinery for carrying the call out.

## Workflow state

`ctx.state` is a key-value space scoped to the workflow and shared by every run
of it. Mutable and current, where an artifact is immutable and versioned.

```python
@workflow(name="guide_poller")
async def poller(ctx: Context, _: object = None) -> int:
    """Poll from wherever the last run stopped."""
    since = await ctx.state.get("cursor", default=0)
    await ctx.state.set("cursor", since + 1)
    return since


async def poll_three_times() -> None:
    runtime = Runtime(store=MemoryStore())
    runtime.register(poller)
    for _ in range(3):
        print((await runtime.run(poller)).output)


asyncio.run(poll_three_times())
```

Reads and writes are **not journaled**. They cannot be: the value is shared, so
a replay serving the original would be reading a fact about the past and calling
it the present. The consequence is that a workflow branching on state does not
replay identically — put the read inside a step when that matters.

The default is backed by the execution store, so it needs no new infrastructure:
one SQLite file on a laptop, whatever database is already deployed in
production. Supply your own `StateStore` to put it somewhere else.

## Progress

A run that takes four minutes has nothing to say for four minutes. `ctx.report`
is where it says something.

```python
@step
async def guide_fetch() -> str:
    """Fetch a page."""
    return "page 1"


@workflow(name="guide_talker")
async def talker(ctx: Context, _: object = None) -> str:
    """Narrate the work as it happens."""
    await ctx.report("fetching page 1")
    page = await ctx.step(guide_fetch)
    await ctx.report("indexing", kind="progress")
    return page
```

Reports are not journaled either — journaling them would make progress chatter
part of the replay contract, so a workflow could not be made more talkative
without changing what its replays produce. A replay does report again, under the
replay's own run id, because a replay really is executing.

They surface through every surface that can already read a run:

```bash
loom watch <run>          # progress interleaved with journal entries
```

```python
from loom.facade import LocalFacade
from loom.mcp_server import tools


@workflow(name="guide_watched")
async def watched(ctx: Context, _: object = None) -> str:
    """Narrate two steps of work."""
    await ctx.report("fetching page 1")
    await ctx.report("indexing", kind="progress")
    return "done"


async def watch() -> None:
    runtime = Runtime(store=MemoryStore())
    runtime.register(watched)
    facade = LocalFacade(runtime)

    run_id = (await facade.start("guide_watched", None))["run_id"]

    print([r["message"] for r in await facade.reports(run_id)])
    print(await tools.get_run_progress(facade, run_id))


asyncio.run(watch())
```

Over HTTP the same reports come from `GET /runs/{id}/reports`, or
`LoomClient.reports(run_id, offset=...)`.

The default buffers in memory, bounded per run, and is in-process only — a run
reported on by one worker is not visible to another. A host that needs
cross-process fan-out supplies a `RunStream` adapter; that is what the port is
for. An adapter that only accepts reports and cannot serve them back yields
nothing rather than fabricating an answer.

## Events versus output

`ctx.publish(name, payload)` broadcasts an **event** to whoever is waiting.
`ctx.report(message)` streams a run's **output**. `ctx.emit` was both, which is
the kind of ambiguity that produces code that reads correctly and does the other
thing; it is now a deprecated alias for `publish` and warns once.

Journals written under the old name still replay — the entry keeps its `emit:`
prefix, because renaming it would have made every in-flight run unreadable to
the code that has to finish it.
