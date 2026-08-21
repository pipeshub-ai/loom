# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — every agent is told what day it is

`loom author "who won the IPL in 2026"` wrote a refusal into the file where the
workflow should have been: the season "hasn't happened yet", it explained, four
months after it ended. Every verification stage passed it, and correctly — the
file compiled, ran, and answered. Nothing downstream can catch that, because a
model's sense of "now" is the end of its training data and it has no way to
notice the sense is stale.

So the system prompt now says when it is. `loom.agents.now.time_block()` renders
the local date and time, the zone it is in (`Asia/Kolkata (UTC+05:30)` — the
IANA name *and* the offset, since `IST` is three different zones and an offset
alone cannot place a DST boundary), and the same instant in UTC, followed by the
sentence that makes the date usable: your training data ended before this, so
treat what you remember about what has happened as out of date, and look it up
rather than declaring it impossible.

It reaches `WorkflowCodingAgent` — appended even when `instructions=` has
replaced the base prompt, because what a caller replaces is the *instructions*
and the date has never been one of them — and every `Agent`, so `ctx.agent()`
gets it too. The coding agent's copy carries one extra sentence: this is when
the workflow is being **written**, so resolve a relative date the spec states
and bake it in the way a resolved entity id is baked in, but anything the
workflow needs about its own runtime comes from `ctx.now()`, never from a date
copied out of the block.

Rendered **once per agent run, never per turn** — `build_system_prompt()` is
called once for a job and `execute()` builds its messages once for a turn loop
— so a system prompt stays byte-identical across the conversation and remains
eligible for provider-side caching. Time comes from the `Clock` port rather than
`datetime.now()`, and `LocalFacade`, `run_agent_durably` and `BuiltInBackend`
all hand over the Runtime's own, so an agent inside a workflow under
`ManualClock` is told the moment the test chose. The zone is read from
`$LOOM_TIMEZONE`, then `$TZ`, then `/etc/localtime` — which is the only place a
Unix host keeps the zone's *name*, as `tzname()` returns an abbreviation — and
falls back to the offset `astimezone()` reports.

`Agent(time_aware=False)` is bit-for-bit what shipped before, for an agent whose
job cannot turn on the date.

### Changed — the authoring job gets 30 turns, not 22

`max_discovery_turns` was 20 and repair adds 2, so a spec naming several
systems ran out at 22 having produced nothing and spent everything. Discovery
is now 28, for a job budget of 30.

The message it fails with also named two things a caller does not have:
`UsageLimitExceeded` appends "raise max_turns or narrow the task", and the
agent appended "raise max_discovery_turns or narrow the spec" after it — two
remedies in one sentence, both constructor arguments, for somebody who ran
`loom author` and holds `--turns`. The library's own advice is trimmed when
there is a better one, and the surface's names the flag.


### Added — an interrupted authoring job can be picked up

`CodingSession` held its transcript in a list and its budget in a dataclass,
and both died with the process: Ctrl+C four minutes in discarded the toolset
schemas the model had fetched, the entity ids it had resolved against real
services, its plan, and every token paid for.

Snapshots go to `CacheStore`, which every backend already implements, and are
taken **per model turn** rather than per `ask()` — `ask()` is one whole ReAct
loop, so persisting around it saves nothing until discovery is over, which is
the window an interruption actually lands in. `loom author --resume <id>`
restores the transcript and what it cost; `--resume list` shows what is
available. The id is offered only once a turn has completed, because naming one
that resolves to nothing helps nobody.

### Added — `loom completion bash|zsh|fish`

Generated from `build_parser()`, so a renamed flag cannot leave three shell
dialects promising the old one. Workflow names and run ids are completed by
calling back into `loom --json`, since both change while the shell is open.

### Added — `--max-tokens` and `--max-cost` on `author`

They existed on the agent and reached no surface, so `max_cost_usd` bounded
nothing anybody could set.

### Fixed — advice that could not help

Three of them, all the same shape. An interrupted `author` pointed at
`loom runs --status running`, sending people to look for a run that never
existed. A job stopped by a *token* ceiling was told to raise its *turn*
budget. And a resume was offered for jobs that had no snapshot to resume.

### Removed — `RuntimeBridge`

The deprecated shim over `LocalFacade`, whose only remaining callers were its
own tests — 176 of the 318 lines of `tests/test_phase9.py`, plus five workflow
fixtures that `@workflow` was registering process-globally into every other
test in the session.

### Fixed — two `.env` readers that read different files

`auth_commands` had its own parser pointed at `Path.cwd()/.env` while the rest
of the CLI read the project root's, so `loom connect` from a subdirectory saw a
different file — or none — than `loom run` did. One reader now, which falls
back to the working directory when there is no project: a file the CLI *reads*
is one the user put there on purpose, unlike a file it writes.


### Fixed — an authored workflow could not be run by name

`loom author -o flows/digest.py` wrote a file and `loom run digest` then
reported an unknown workflow: a name resolves through `[tool.loom] modules`
and nothing had added it, so the last step of the loop failed in a way that
reads as the authoring having failed. The module is registered now — the line
the scaffolded `pyproject.toml` already tells you to add — and the hint names
the workflow rather than printing `loom run <workflow>` literally, since a
name comes from `@workflow(name=...)` and is routinely not the filename.

### Fixed — the progress clock froze between events

`rich.live.Live` re-renders whatever object it holds, and it was handed a
finished `Text`, so the elapsed seconds moved only when an event called
`_redraw`. Twenty frames drawn over two and a half seconds of silence, every
one reading "0s" — during a model call, which is most of the wall clock and
exactly when somebody is wondering whether it has hung.

### Added — `loom` is an interactive session

`loom` with no subcommand, at a terminal, opens one (`[cli]` extra). Type what
you want: with a file in focus that changes it, with none it writes a new one
and focus follows what it wrote. Piped, redirected or in CI it prints help and
exits 0 exactly as it always has.

A slash command **is** the subcommand — `/run digest -i @x.json` is parsed by
the same parser and dispatched to the same handler as `loom run digest -i
@x.json`, down to the exit code. The tempting shape is a registry of small
functions over the facade, and it is a second implementation of every command:
the two drift, and the drift is invisible because both resolve and both run.

### Added — the agent is visible while it works

`loom author` awaited one coroutine and printed nothing until it returned:
twenty discovery turns, sixteen verification stages, three repair rounds each
re-invoking the model, a smoke run and a replay, all silent. The seventeen
`logger.info` calls narrating it went to a logger no CLI configures, and there
was no `-v`.

Nothing new was needed under it. `AgentContext.hooks` already existed and the
runner already drove the agent family from it; `CodingSession` built the one
`AgentContext` a job makes and left the field empty. `edit()` passed the
registry through and `generate()` did not — one keyword argument, in the same
file, and no unit test could see it. `tests/test_cli_progress.py` therefore
asserts wiring rather than rendering.

`tool_start`/`tool_end` join the agent hook family, carrying the tool, its
arguments, the outcome and the elapsed time. Observation, not a second way to
refuse a call — that is already an effect hook on `kind="tool"`, which never
fires for an agent running outside a workflow, because it has no broker.

### Added — `loom doctor`

Store URL and whether it can actually be written, which provider key is set,
whether the declared modules import, workflows registered, toolsets reachable,
extras installed. Exit 1 on anything that would fail later. Every one of those
failures was previously found by running a real command and working backwards
from a symptom.

### Changed — a project keeps its runs

The default store was `memory://`, so every `loom` process built its own
journal and a run recorded by one invocation did not exist for the next.
Twelve of the forty commands were inert out of the box, and `--detach` printed
an identifier for a run that ceased to exist when the command ended.

Resolution is now `--store` > `$LOOM_STORE` > `[tool.loom] store` >
`.loom/runs.db` beside the nearest `pyproject.toml` > `memory://`. The last is
reached only with no project at all, because nowhere-to-write and
not-wanting-to-write are different things. `.env` is read from the project
root, real environment variables winning — the cookbooks have done that since
they existed and the CLI did not.

### Changed — nothing is written before it has been shown

`loom edit` wrote the file and *then* printed the diff, the graph delta and the
explanation. No confirmation, no backup. `loom author -o existing.py` clobbered
silently for the same reason. Both now show the change and ask; `--yes` writes
without asking and non-interactive **denies**, because a gate that could not
run has not passed.

### Fixed — a completed run could exit 1

`Printer.value` interpolated a run's own output into a markup-enabled
`console.print`, so a workflow returning `"[/tag]"` raised `MarkupError` from
inside the renderer, nothing caught it, and a run that completed printed a
traceback and exited 1 — while `--json` printed it and exited 0. A rendering
fault could change what the exit-code contract reported.

The renderers carrying data build `rich.text.Text`, which has no markup to
parse. `_strip_markup` now removes the ten tags this package writes and nothing
else: it used to match any bracketed lowercase word, which deleted `[dev]` out
of the `pip install -e '.[dev]'` that `loom init` prints — the first command a
new user runs, handing them an install that leaves them without the pytest the
same line tells them to run.

### Fixed — `--follow` did not follow

It did not imply `--detach`, so `start` drove the run to completion and
`follow` then polled a run that had already finished: every journal line
arriving at once, after the fact, from the one flag whose purpose is that they
do not. Measured at 1.9s/2.7s/3.9s for three one-second steps, against all
three within 30ms. `RuntimeFacade.journal()` gained the `offset` `reports()`
already had.

### Fixed — giving up on a run reported success

`STATUS_EXIT` mapped `running` to 0, so `loom watch --timeout` on a run still
going exited green — the conflation exit 3 exists to prevent, one state over.
`follow()` now reports whether the run *settled*, which is not readable from
the status alone.

### Fixed — an absent `--input` overrode the workflow's own default

The engine passes the input positionally, so omitting the flag passed an
explicit `None`: `async def flow(ctx, x: str = "a")` ran with `None` and failed
inside the first step with a `TypeError` that reads as a broken workflow.
`WorkflowDefinition.input_default` exposes the declared default rather than
`invoke` applying it — an explicit `None` and an absent argument are different
things, and only the caller knows which it has.

### Fixed — thirteen capabilities reached the CLI and nothing else

`RuntimeFacade` declares 33 methods and the MCP server registered 19 tools, so
`edit`, `pending`, `respond`, `pause`, `unpause`, `pin`, `nodes`, `node`,
`publish` and `artifact_history` were unreachable from a client — which could
start a run and never answer the human gate it parked on. All are tools now,
and `tests/test_surface_parity.py` fails on a port method that reaches no
surface and carries no written reason.

### Fixed — `loom setup claude` configured the wrong Claude

It wrote Claude *Desktop*'s config while reading as the whole family, so anyone
running Claude Code got a command that reported success and wired up nothing.
The clients are now `claude-code` (project `.mcp.json`) and `claude-desktop`;
`claude` still resolves to the desktop app and says which one it picked.

### Fixed — `loom refresh --all` never existed

Documented in two places in `CLAUDE.md` and copied into the new CLI guide.
`loom refresh` with no names already means every stored credential. Found by
`tests/test_cli_docs.py` on its first run.

### Added — a CLI guide, and grouped help

`docs/guides/cli.md`, the first page about the surface most people meet;
`getting-started.md` was Python-first and never mentioned `loom`. `--help`
groups its forty commands by what you are trying to do instead of printing
argparse's flat list with `author` and `run` sixth and seventh.


### Fixed — a browser screenshot was captured and then discarded

`SnapshotIn.vision` asked the provider for a picture, `local.py` took it, and
`PageSnapshot.screenshot` carried it as an `Attachment` — then `_page_out` built
its `PageOut` without that field and dropped it. A caller passed `vision=True`,
paid for the pixels, and received nothing back.

`PageOut.screenshot` now carries it, and `browser.navigate` gained `vision` of
its own so proving you reached a page does not need a second node call. An
`Attachment` rather than a path: it journals losslessly, and with
`Runtime(blobs=…)` it offloads by content hash instead of putting a
quarter-megabyte of PNG in a journal row.

Found by watching the coding agent rather than by reading the code. Given a spec
that asked for screenshots, it read all six browser node contracts in four turns
— exemplary — and then spent twelve turns searching the catalogue for
"screenshot", "vision", "capture", "image" and "artifact", found nothing that
could produce one, and exhausted its budget without writing a line. The wander
was not indiscipline; it was looking for a capability the suite did not have.

### Fixed — browser nodes were unreachable from every surface

`Runtime.from_env()` wires a store, blobs and an agent backend from the
environment, and did not wire a browser provider. So `loom run` refused any
workflow calling a `browser.*` node with "requires browser, which this Runtime
does not have configured", and the only way to satisfy it was to construct the
Runtime by hand in Python — which no CLI, MCP or HTTP caller does. The whole
node suite was library-only by accident.

It now wires `LocalBrowserProvider` when the `browser` extra is installed.
Having a provider costs nothing until a `browser.*` node is actually called, and
a missing extra leaves it `None` so the node's own requirement check reports it
— the same shape `_backend_from_env` and blobs already follow.


### Fixed — the durability core's notion of which call is which

A durable call took its journal path from a counter shared by every branch,
allocated when the call was *constructed*. Under `ctx.gather` — the documented
parallel primitive — the numbering therefore depended on how long the previous
step took, so a replay with different timings served two logically distinct call
sites each other's recorded values. Silently: the default `VerifyMode.WARN`
logged the mismatch and served the value anyway, and the run reported
`completed` with different output.

`ctx.gather` now gives each coroutine branch its own numbering space (`0.0`,
`0.1`, `1.0`), allocated synchronously in argument order before any branch
starts, through a `contextvars` override the branch's task inherits. Calls that
were already constructed — `ctx.gather(ctx.step(a), ctx.step(b))` — keep their
flat paths, so every journal written before this still replays.

Raw `asyncio.gather`, `wait`, `as_completed`, `create_task`, `ensure_future`
and `TaskGroup` in a workflow body are now determinism violations, reported by
the scanner at `@workflow` time and by `CodeValidator` as a blocking error for
generated code. Inside a `@step` they are ordinary Python and allocate no paths.

**`VerifyMode.STRICT` is the default.** The argument for `WARN` was that a
difference is not always a bug, which is true — but the *common* cause was the
engine's own race, and warning about that meant handing one branch the other's
answer. With branch-local numbering gone, what is left is a real divergence. An
entry journaled before verification existed carries no fingerprint and is still
skipped, which is what makes the new default safe to upgrade into.

### Fixed — the run trace matched nothing, ever

Journal paths are ordinals (`"0"`, `"3.1"`); graph node ids are slugs (`fetch`,
`jira.create_issue`). `_match_to_node` compared the two and split on `/`, a
separator the journal never uses, so a completed run rendered with every node
`pending` — and the canvas overlay, `loom watch` and the time-travel scrubber
all inherited it. The test that covered it built an entry with `path="fetch"`,
a shape the engine has never produced.

Entries are now matched by name, with lexical order as the tie-break, which
survives the two shapes an ordinal cannot: a loop, where one node produces many
entries, and a branch, where lexical order is not execution order.
`RunTrace.unmatched_entries` reports what named nothing, so a stale committed
graph is visible rather than silently empty. Control-flow nodes report
`structural` rather than lying with `pending`. `TimeTraveler` is built on the
same overlay instead of its own copy of the rule.

### Fixed — controls that were declared and not enforced

- **`GuardedBroker.max_calls`** counted completions, so it bounded nothing under
  concurrency: every in-flight call read the count before any of them wrote it,
  and ten concurrent calls passed a ceiling of three. The slot is reserved
  before the await.
- **RBAC** — `retry`, `approve`, `send_event` and `publish` mutated a run with no
  check at all, and `Permission.GRANT_APPROVE`, `FLOW_AUTHOR` and `FLOW_DEPLOY`
  were checked nowhere, while `test_phase5` asserted the role *table* and never a
  call site. `@requires(...)` binds a permission to the method so it can be
  enumerated; a registry test fails on any public mutating coroutine that
  declares none.
- **`SandboxPolicy`** — `network` and `allowed_imports` were accepted and
  silently dropped, the exact failure the module's own docstring forbids. The
  check is derived from the policy rather than a hand-kept list of two field
  names; `network` is three-valued (`NetworkPolicy`), because two states could
  not carry "no opinion" and "must be impossible" separately; `allowed_imports`
  is *enforced* in the child by an `__import__` guard keyed on the calling frame.
- **`Runtime(strict_determinism=True)`** was assigned and read at zero sites for
  its whole life while the docs said it raised `NondeterminismError`. It now
  raises `ConfigurationError` naming the three checks that do work.
- **`SingletonPolicy(mode="cancel_previous")`** admitted the new run and
  cancelled nothing. It raises rather than pretending.

### Fixed — nodes are not toolsets

A node call journals as a step whose target is `<category>.<id>`, which the
broker's bridged-toolset branch read as a *toolset* called `control` — one no
manifest declares and no grant can name. So declaring any toolset grant denied
every `ctx.node()` call, `control.switch` included, with an error telling the
author to grant a toolset that does not exist.

`GrantSet.nodes` is its own dimension, and `EffectCall.kind` is `"node"`.
`control`, `transform` and `guard` reach nothing and pass without an entry even
under `strict`; `io`, `agent` and `human` need one. `artifact` and `event` are
refused under `strict` rather than left unchecked, which is what that flag
promises.

### Fixed — cancellation, and which code a parked run resumes against

`cancel()` recorded the request in an in-memory set and wrote `CANCELLED`
straight onto the record, so a worker in another process never saw it, kept
executing steps, and overwrote the status on its next update — and because its
body never raised, the compensation stack never unwound. `cancel_requested` is
now persisted and observed through the lease heartbeat, taking effect at the
next durable boundary.

`Runtime(version_policy=…)` answers what an in-flight run resumes against after
a deploy: `LATEST` (the default, and what every release before this did),
`PINNED` and `REFUSE`. `resolve_workflow` read the in-process registry and
nothing else, so a run parked on a 24-hour approval resumed against whatever was
deployed meanwhile.

### Fixed — cost accounting, and parallel tool calls

`Usage.input_tokens` is defined as the **total** prompt cost, and every provider
normalises to it. `estimate_cost` used to subtract cache reads from a count
that, for Anthropic, already excluded them: 500 real input tokens beside 20,000
cache reads were billed as zero. Cache *writes* were never counted at all, which
under-billed every agent loop by the whole write surcharge. Rates are per family
(`CACHE_RATES`) rather than a flat 0.25 that was right for no vendor;
`claude-opus-5` is priced; `is_priced()` makes an unpriced model an explicit
question rather than a silent `0.0` that disables every dollar budget.

Anthropic's converter turned each tool result into its own user turn, so a turn
with two parallel tool calls produced two consecutive user messages. Results
answering one assistant turn now share one.

### Fixed — the smoke runner held the host's credentials

`smoke_run` inherited `os.environ` in full — every API key the process held —
while the MCP tool exposing it told the model "no real network or credentials".
`SmokeIsolation` passes an allowlist of what an interpreter needs to start;
`inherit_env=True` is the escape hatch. The authoring tools that *act* —
`smoke_test_workflow`, `save_workflow`, `call_read_operation` — now require
`workflows:author` through `AuthoringGate`; the three that read a catalogue stay
open, which is what "not facade-scoped" was ever right about.

### Added — `loom edit`, `loom pause`, `loom pin`

- **`loom edit <file> "<change>"`**, and `RuntimeFacade.edit`. Generating was the
  only entry point, so every change to a workflow meant regenerating it from a
  spec. The result goes through all sixteen verification stages, which is what a
  visual editor cannot do; an `EditResult` carries a unified diff *and* a node
  delta projected from both versions. `EDIT_INSTRUCTIONS` states the rules an
  edit has and a generation does not — smallest change, never rename a step (a
  name is what the journal records), decline rather than guess.
- **`loom pause` / `loom unpause`.** A run parked only when its own code said so,
  so the only things an operator could do to a misbehaving run were cancel it or
  watch it. A pause suspends on `resume:<run_id>` at the next durable boundary,
  never mid-step.
- **`loom pin <run>`** turns a run into a pytest regression file built from its
  journal via `given(...)`. Values are redacted on the way out — a step's
  *outputs* were never redacted into the journal — and the file says so when
  redaction changed something.

### Added — a way to measure the coding agent

`loom.eval` ships a runner, a deterministic `StructuralJudge`, a dataset seeded
from the committed reference specs, and `scripts/run_eval.py` gating on no
regression against a baseline. The package previously held three Pydantic models
and no instrument, while `phases/phase-7` specified a model-stratified suite.

`CodingSession` and `GenerationBudget` fix the two defects that made the agent
worse than it looks: every repair round was a *fresh conversation*, losing the
toolset schemas and resolved entity ids discovery had paid real API calls for,
and each invocation restarted the turn and cost counters, so nothing bounded the
job. `WorkflowCodingAgent(max_total_tokens=…, max_cost_usd=…)` bounds it now.

`search_operations` finds the operation rather than the toolset, and toolset
scoring is length-normalised so ranking stops being a proxy for how much
documentation a manifest carries.

### Changed — flow control counters are a port

`AdmissionController` held every counter in a process-local dict, so
`Runtime(admission=…)` provided no concurrency limit, no rate limit and no
singleton guarantee in any multi-worker deployment — the only kind that has
these problems. `AdmissionState` is a seam:
`InMemoryAdmissionState` (default, TTL'd so a high-cardinality partition key
stops leaking) and `StoreBackedAdmissionState` over the `CacheStore` +
`LockProvider` every store already implements.

Event-log reads no longer walk what retention deleted — a `tail` marker bounds
them — and `_remember_topic` takes its own lock, since the append lock is per
topic and that key is global.

`Runtime.capabilities()` reports which optional ports are wired, which nothing
could ask before.

### Added — driving a web page

`Runtime(browser=LocalBrowserProvider())`, and six `browser.*` nodes: navigate,
snapshot, observe, act, extract, close. Two Protocols in `loom/browser/`, one
reference provider over Playwright, a fake for offline tests, and
`loom.testing.conformance.verify_browser_session` so a host proves its own
Browserbase or Kernel adapter. The position `loom/knowledge/` takes about vector
databases, for the same reason.

**No new dependency.** `[browser]` is the extra `BrowserProbe` already needed;
`[stealth]` adds Apache-2.0 `patchright`, opt-in. Skyvern and `workflow-use` are
AGPL-3.0 and were ruled out; `browser-use` is MIT and pins ~40 packages with
`==` including `pydantic==2.12.5`, so it is welcome as a host adapter through
the `loom_browser_provider` entry point and unusable as a dependency.
`tests/test_dependency_licences.py` enforces that as a gate.

**Controls are addressed by accessible role and name**, never a selector. Two
matches raise rather than resolving — picking one is how an automation clicks
the wrong button and reports success. `tests/corpus` measures the claim against
10 frozen real pages and 114 hand-labelled controls: **76%**, offline and
deterministic.

**An act declares its effect**, so `TaintBroker` refuses a submit on a run that
has read a page, with no browser-specific code in `runtime/`. Drift repairs a
read and refuses a write. `SessionScope.DURABLE` lets a run park on a person
mid-flow and reattach afterwards, with `HumanRequest.live_view_url` carrying the
takeover link.

### Added — the coding agent can write browser workflows

A browser block in `DEFAULT_SYSTEM_PROMPT`, two verification stages, and
`docs/guides/browser-automation.md` whose every snippet compiles and resolves in
CI. `SelectorStage` reports a CSS or XPath address written into a browser
workflow; `BrowserEffectStage` reports an act with no declared effect, and a
declared write that nothing in the file asks a person about.

Both are asserted in **both** directions. A stage that fires on correct code is
worse than no stage, because the repair loop acts on `report.errors` and will
rewrite working code to silence it — so `SelectorStage` is pinned against
ordinary strings as well as real selectors, and the whitespace descendant
combinator is keyed on a `.class` sigil since `#a #b` is two hashtags.

`BrowserProbe`'s census now carries role and accessible name in the same shape
`PageSnapshot.tree` uses, so an authoring observation is directly usable as a
smoke fixture. Its summary reports how many controls are addressable by name,
which is the sentence that decides whether the approach works on that page.

The smoke sandbox installs `FakeBrowserProvider(permissive=True)`, for the
reason `AutoRespondChannel` exists: otherwise a generated browser workflow can
only reach a connection error, and the cheapest repair for an error a model
cannot fix is to delete the browser work.

The prompt block cost 2235 characters and shipped at 681. `DEFAULT_SYSTEM_PROMPT`
is held to a budget whose margin is one sentence wide precisely so an addition
has to justify itself, and it caught this one. The opening line duplicated step
1's DISCOVER exit and was merged into it — where step 1 was wrong without it,
naming plain Python as the only answer when no toolset matches — and the worked
example was deleted outright, because `node_contract` renders the call from the
node's own models and a copy in the prompt is a second source that can drift.

### Added — a plan cache, and a narrower human exemption

`browser.observe` remembers which control an intent meant, across runs, over
`Runtime.cache`. Tier 0 answers an exact name for free; tier 1 costs a model
call, and this is what stops that being paid on every run. **A hit is verified
against the live page before it is used**, so a stale entry costs a wasted
lookup rather than a wrong click — and it is *replaced*, so the run that finds
it also fixes it. Keyed on the page shape (scheme, host, path, never the query)
so two rows of one form share a plan, and scoped per workflow.

The `asks_human` taint exemption is narrowed from `requires=["human_channel"]`
to that **and** `suspends`. On `requires` alone, any third-party node could
declare a channel it never uses and receive blanket taint exemption for whatever
else it did; a node that also parks the run is waiting for a person. All five
shipped `human.*` nodes declare both, so nothing legitimate changed.

Its residual is now asserted rather than implied: a tainted run can still put
what it read into an approval's `context` and name its own `assignees`. Both are
deliberate — showing the reviewer what was read is the request's purpose — but
it means the human channel is a delivery path a tainted run can reach, and
whether that leaks depends on whether the host's channel honours a
workflow-chosen recipient. LOOM has always disclaimed that; the disclaimer is
now in the suite.

### Added — a browser session is treated as the credential it is

`DEFAULT_REDACT_KEYS` gains `storage_state`, `cookie` and `cookies`. A
`storage_state` is an authenticated session in a JSON blob, worth as much to
anyone reading a trace as the password that produced it, and it travels as an
ordinary field — so nothing about its shape stopped it reaching a journal.

The existing whole-word rule needed no special case: `storage_state` is
multi-word and matches wherever it appears, while single-word `cookie` must be
the last word, catching `session_cookie` and leaving `cookie_banner_text`
alone. `storage_ref` is deliberately *not* redacted — it names where the jar is
kept rather than what is in it, and is the half a person reading a trace needs.

### Fixed — two defects the typecheck gate caught that the suite could not

`browser.close` had lost its `return` to a bulk edit and would have returned
`None` where its contract declares `CloseOut`; `BrowserEffectStage` passed an
`ast.AST` to a function requiring an `ast.Call`. Neither was visible to the
tests. `python scripts/typecheck.py` is clean across 413 source files.

### Fixed — mypy's findings about other files were blamed on generated code

`TypeStage` kept every `: error:` line mypy printed and split the filename off,
so a complaint about a *dependency's* stubs arrived as a complaint about the
generated workflow with the evidence of its origin removed. Not hypothetical:
numpy's stubs use PEP 695 `type` statements — a syntax error below 3.12 — so any
environment with numpy installed handed the repair loop `737: error: Type
statement is only supported in Python 3.12` to fix in a file that has no line
737. The environmental-failure trap that "fed 401s into the repair loop until a
workflow came back gutted", in a new costume.

Errors are now attributed to the file they came from, and mypy stopping early
("errors prevented further checking") reports the stage **skipped** rather than
clean — the same distinction `scripts/typecheck.py` exits 2 for, because a gate
that did not run must be distinguishable from one that passed.

Two tests were asserting the environment rather than the claim, and both are
now honest about it: `test_toolsets` skips when mypy could not look, and the
MCP pipeline test no longer demands `ok` from every stage — clean code means
nothing *failed*, not that every check ran.

### Fixed — three ways a policy could not see what it was deciding about

All found while wiring the above, all outside the browser package, and all the
same shape: a control that reads as enforcing something and enforced nothing.

- **`effect_by` was dead for every node.** `_effect_arguments` returned `{}` for
  anything that was not a `dict`, and `ctx.node` passes the validated Pydantic
  model — so `io.http_request(method="DELETE")` reached the broker classified
  WRITE, and a deployment on `block_writes=False, block_destructive=True`
  permitted exactly the calls it was configured to refuse. Hooks and guardrails
  deciding on values saw an empty mapping for nodes too.
- **A node's own I/O was unclassified.** An inline `ctx.call` carried no effect
  metadata, so `io.http_request(method="GET")` dispatched the node as READ and
  its inner `http:GET` as WRITE — the node's classification defeated one level
  down by itself. `call` now inherits the enclosing node's resolved class and
  target; `step` does not, because it names a function that may carry its own
  from a manifest.
- **The taint rule blocked its own exit.** Every `human.*` node is WRITE and
  open_world, both accurate, so a tainted run could not reach the person whose
  approval is the only thing that clears the taint. `EffectCall.asks_human` now
  carries it, derived from `requires=["human_channel"]` rather than a name
  match.

### Changed — the MCP server speaks mcp 2.x

`FastMCP` is gone. The 2.0 SDK did not rename it in place, it removed
`mcp.server.fastmcp` outright, so `loom mcp` is now built on `MCPServer` from
`mcp.server.mcpserver` and the extra declares `mcp>=2.0,<3`.

The previous `mcp>=1.2` was wrong at both ends, and quietly. 2.0 is the current
stable release, so `pip install loomsdk[mcp]` already resolved to a version
where the server could not import at all; and nothing older than 1.14 ever
worked either — 1.13.1 fails 12 of the MCP tests with 31 errors, because mcp's
own tool registration calls `issubclass()` on a union annotation. Neither bound
is discoverable by probing imports: every symbol LOOM uses resolves as far back
as 1.12.3, which then registers no tools.

Both bounds are measured against the suite rather than argued, and the ceiling
is now closed. An open upper bound is precisely what let 2.0 arrive as a silent
breaking change — `phases/phase-9-mcp-server.md` predicted it and prescribed
`mcp>=2.0,<3.0`. Every 2.x release is still taken; 3.0 becomes a decision
somebody makes rather than something a fresh install discovers.

What moved, beyond the name: `host`, `port` and `transport_security` are
run-call arguments now instead of constructor ones — where a server binds is a
property of serving it, not of the tool registry. `build_server` still takes
them and records them as `TransportOptions` on the built server; `serve` hands
them to `run`. Accepting them and binding elsewhere would have been the one
genuinely dangerous option, because nothing fails: the server starts, listens
somewhere else, and the client simply never connects.

`ToolAnnotations` and `Tool` fields are snake_case (`read_only_hint`,
`input_schema`); `call_tool` returns a `CallToolResult` rather than the content
sequence; `McpError` is `MCPError`.

### Fixed — the type gate was checking the MCP surface against `Any`

`mcp.*` is declared `ignore_missing_imports`, and the type-check environment
installed `[dev]`, which does not include the extra. Every annotation in
`mcp_server/` therefore resolved to `Any` and mypy reported success over ~900
lines it had not checked — the same shape of hole as the numpy trap the wrapper
exists to report. The environment is now `[dev,mcp]`, in CI and in
`scripts/typecheck.py`'s provisioning alike; it found two real errors on the
first run. `mcp` pulls no numpy, so the reason for `[dev]` over `[all]` is
unaffected.

### Added — `loom author`, and authoring on the facade

The coding agent had no surface. Everything it could do was reachable only by
writing a Python driver, which is a large part of why its loop went so long
without anyone pressing on it: the one component nobody could run casually was
the one making the decisions.

`RuntimeFacade.author(spec, *, packages, smoke_input, observe)` — on the port,
so the CLI (`loom author`), the MCP server (`author_workflow`) and anything else
built on it share one implementation, with `test_surface_parity` failing the
build if an adapter implements less than the whole method. `cmd_author` reads
flags and renders; it decides nothing.

Local only, with the reason stated rather than a `NotImplementedError`:
authoring runs where the code will run, reading this process's toolsets, nodes
and probes to decide what the workflow may call, and a server's model key would
be spending someone else's budget. `AuthorizedFacade` gates it on a new
`workflows:author` scope — separate from `workflows:publish`, because authoring
spends tokens and, with observation on, reaches systems the spec names.

`loom.agents.providers.from_env()` now owns the key→provider mapping that
`_backend_from_env` had inline; the Runtime and the coding agent were reading
the same three variables from two places. No key set reports which keys to set.

End to end through the CLI, against a real API:

```
$ loom author @spec.txt --output brief.py
  wrote brief.py
  claude-sonnet-5  1074+2956 tokens  repairs=1  ran=yes
  looked at: observe_target, node_contract, validate_code
  step      io.http_request fetch of endoflife.date/api/python.json
  agent     summarize newest cycle
$ loom run python_eol_newest_release
  output  **Newest Python release cycle: 3.14** (latest patch: 3.14.7)
```

The plan prints as columns rather than `[step]`, because `Printer.line` renders
through rich, which reads a bracketed word as a style tag and removes it — it
printed a bare list of nodes until it was read. `verbatim` documents that trap;
this is the second place to hit it.


### Changed — one current default per vendor

`claude-sonnet-5` for Anthropic and `gpt-5.6-terra` for OpenAI, everywhere a
model name is written down: the provider defaults, the backend defaults
(`PydanticAIBackend`), every cookbook example, and the guides. `claude-sonnet-4-6`
appeared in nine of those places as a hand-written argument, so a reader copying
any example pinned it whatever the provider default said.

The LangChain example loses its `temperature=0` with the model. Claude 5 rejects
sampling controls outright — a hard 400 — which `AnthropicProvider` already
handles in `_rejects_sampling_controls`, but `ChatAnthropic` is constructed
directly and nothing was there to drop it.

Both new defaults are **priced**, which is the part that is not cosmetic:
`estimate_cost` returns `0.0` for a model with no rate on file, so a default
nobody priced makes every budget unenforceable while looking like it is working.
`claude-sonnet-5` is $2.00 / $10.00 per million — the launch introductory rate,
which Anthropic has since made the standard price rather than raising it to
$3/$15 — and `gpt-5.6-terra` is $2.00 / $12.00. `gpt-5.6-sol` stays unpriced;
it is nobody's default, and its rate is still not on file.


### Fixed — the agent had the probe and never reached for it

Watched turn by turn on the task probes were built for: a Playwright workflow
against a real booking page. With `observe_target` in its tool list and the
target in the spec, the agent spent **43 turns and eleven minutes** reading the
tool documentation of every integration it had — jira, gmail, slack, confluence,
google_drive, quickbooks, sharepoint, teams, tavily, hubspot — searching the node
catalogue for "screenshot", "attachment", "emit", and validating a throwaway
`async def foo(x): return x` to find out whether a name was importable. It
called the probe once, at the last turn, against an unrelated URL. It produced
no code.

Two causes, both in what the agent was told rather than what it was given:

- **Discovery had no exit.** Step 1 of the prompt is "search_toolsets with
  keywords from the spec", and nothing said what to do when nothing matches.
  There is no browser toolset, so it kept looking. It now says that two empty
  searches mean there is no integration for this — a normal answer — and that
  plain Python in a `@step` is the rest of the job.
- **Nothing pointed at the probes.** `ProbeRegistry.prompt_block()` now names
  them and says when they apply, appended beside the node catalog's block and
  omitted entirely when nothing can be looked at. A tool's docstring sits in the
  tool list; that is not the same as the model knowing this task calls for it.

The same task afterwards: **two turns, 26 seconds, clean.** Turn one observed
the page, turn two wrote the file. The generated code carries the reason in a
comment — *"Observed page has no native `<input>/<textarea>/<select>` — the
widgets are custom (role-based)"* — and its selector covers ARIA roles. Run
through the CLI it reports three controls, including the `div` acting as
"Confirm and continue" that the first version of this workflow missed entirely
while reporting zero and no error.

One caveat worth recording, since it cost a run: a probe is only useful when the
spec names something concrete to look at. A spec that says "input: a URL" and
never gives one leaves the agent inventing a target — it observed
`opentable.com` twice. Name the page the workflow will actually run against.


### Fixed — two tests that failed for reasons outside the code they test

**`TestWallTimeout` blamed the timeout for someone else's orphan.** The test ran
its sandbox under a fixed `run_id`, and the container is named
`loom-sbx-{run_id}-{nonce}`; docker's `--filter name=` matches on *substring*,
so the assertion covered every container that test had ever created. One suite
killed mid-run leaves a container in `Created` state that nothing reaps —
`sweep_orphans` is scoped to the instance label on purpose, so a later process
will not touch another replica's work — and from then on the test failed
permanently, on a machine where the timeout path was working correctly. It now
uses a unique run id per execution and asserts on the container it created.

**A killed subprocess was reported as a broken README block.** The docs checks
treat any non-zero return code as the example failing, and a process stopped by
a signal has neither exited on its own nor written a complete traceback — so a
README block whose process was killed under load came back quoting half an
import line as the evidence, matched nothing in `is_environmental`, and failed
the suite. Re-running passed. A negative return code is now treated the way the
timeout beside it already was: the process was stopped from outside, so it has
said nothing about the example. A positive non-zero code still fails, so a
genuinely broken example is caught exactly as before.


### Added — the coding agent can look at what it is writing code against

`loom.agents.probes`, and `WorkflowCodingAgent(probes=default_probes())`. Until
now the agent could ask what *loom* offers and nothing about the world its code
would run in, so it wrote against whatever the spec's author remembered. The
failure that produced this: asked to "collect every visible form control", it
generated `querySelectorAll('input, textarea, select')` — correct for the spec —
and got nothing back, because that page builds its controls out of `div`s. The
run completed and reported zero fields. Told only that the result was empty, the
model's repair was to wait longer, which is the right first guess from the code
alone and cannot ever work.

`Probe` is `supports(target)` and `observe(target, hint=…)`, returning an
`Observation` with a summary, a structured detail, and evidence as
`Attachment`s. `HttpProbe` (core) reports a JSON response's real field names;
`BrowserProbe` (`[browser]`) renders the page and counts native controls **and**
role-based widgets side by side. On the page above it now says: *0 native form
control(s), 1 role-based widget(s) … a selector over input/textarea/select will
find nothing here* — which is not a description of the page but the reason the
obvious selector fails on it.

Four properties worth knowing:

- **Read-only is built, not promised.** A probe is handed to a model, so
  "please do not write" is not a control. Neither probe has a code path that
  sends anything but GET. `verify_probe` takes a `methods_seen` callback and
  fails on anything outside GET/HEAD.
- **Absence degrades to exactly what shipped before.** An empty registry is
  falsy and `observe_target` is omitted rather than offered-and-useless, the
  rule `ask_user` already follows.
- **The probes escalate rather than compete.** `HttpProbe` wins for a URL, and
  when handed HTML says so and names `probe='browser'`.
- **Authoring-time only.** Nothing here is reachable from a workflow body or
  journaled. A workflow reaches the world through `@step`, `ctx.node` and
  toolsets; looking is not something the workflow later does.

`Probe` joins the seam catalog as its eighteenth port, with a conformance kit
alongside the ones for event sources and stores.


### Fixed — `ctx.node` never appeared in the projected graph

For the node system's whole life, a workflow calling a catalogued node produced
a graph that did not mention it. `_CTX_CALL_MAP` — the extractor's list of which
`ctx` calls draw — had no `node` entry, so the canvas, `loom describe`, and the
committed `graph.json` all silently under-reported any flow built the way the
node system is meant to be used. The graph is projected from the code precisely
so that adding or hiding work shows up in a diff, and for node calls it did not.

Nodes now draw by category: `human.*` as human, `agent.*` as agent, everything
else as tool — because a parked run and a model call are the two things a reader
most needs to pick out, and one generic icon for both hides them. Deliberately
not mapping `control.switch` to `SWITCH`: that kind carries branch edges, and a
node call is one statement with one successor however the node behaves inside.

Two mechanisms keep it fixed, because the two failure modes need different ones.
`DURABLE_CTX_CALLS` names every journaled call and a test asserts it is a subset
of `_CTX_CALL_MAP` — a method missing from the map is wrong for every workflow
at once, so no check over generated code could ever find it. And a new
`projection` stage (cost 18, non-blocking) catches the other shape: a method the
extractor does model, in code its walker never reaches, which
`return await ctx.step(f, x)` was until recently.

That stage justified itself on its first run over the shipped corpus, finding
one more: `if await ctx.wait_for_approval("refund"):` put the approval in the
`if` **test**, which the walker never descended into, so a cookbook workflow's
human gate — the single most review-worthy thing in it — was missing from its
own graph. `visit_If` and the loop visitor now descend into the test and the
iterable, the way `visit_Return` descends into the returned expression. All 41
shipped workflows project completely.

### Added — `catalogue` stage: a step that re-implements a node

A hand-rolled `httpx` call draws on the canvas as an opaque effect where
`io.http_request` would have drawn as itself, and nothing tells the next author
the node existed. The stage (cost 19, non-blocking) reports the two cases where
a node is a straight replacement — HTTP, and document parsing — and only where
that node is registered in this environment, since advice to use something that
is not installed is worse than no advice.

It reads the node catalogue without populating it. An earlier draft called
`load_builtin_nodes()`, which was redundant on both paths that reach a stage
and was enough to change what an unrelated suite saw: a verification stage that
mutates process-global state makes its own findings depend on who ran first.


### Added — the coding agent is now told what its code produced

The pipeline could always say whether generated code *ran*. It could not say
whether the code *answered*. `smoke_run` recorded the workflow's output as
`output_preview` from the beginning and handed it to exactly one consumer —
`ReplayStage`, which compares two runs for **equality** and never for sense — so
a workflow returning `{"field_count": 0, "fields": []}` against a spec asking
for "every visible form control" passed every gate: compiled, imported, ran,
terminated `completed`, replayed identically. Green, and wrong.

Two changes, and the first is the smaller one:

- **`_repair_prompt` carries the run's output**, beside the traceback it always
  carried. A model asked to fix "you returned nothing useful" could not
  previously see what it had returned.
- **`OutcomeStage`** (cost 55, non-blocking) turns that into a repair. Two
  conditions coincide before it speaks, the discipline `CoverageStage` uses: the
  spec asked for completeness — its vocabulary, imported rather than copied, so
  the two cannot drift — **and** the run came back with an empty collection
  anyway.

It reports an *error* rather than a warning, which is the one surprising choice.
The repair loop runs on `report.errors`, so a warning is a finding nobody ever
sees. What makes that safe is already in the loop: **unchanged code ends it**. A
model that judges the empty result correct says so by leaving the file alone,
and the message says as much in those words. It is silent on a synthetic input,
for the same reason `SmokeStage` calls that run unverifiable rather than failed.

`SmokeResult` gained `empty_paths`, computed in the runner where the output is
whole. The first implementation parsed `output_preview` instead, passed every
test written for it, and then went silent on the exact workflow it was built for
— whose empty `fields` list sat behind 1500 characters of page text, past the
preview's 400-character cap. `CheckContext` gained `prior`, so a stage whose job
is to interpret an earlier stage can read it instead of paying to re-run it and
repeating whatever side effects it had.


### Fixed — prompt caching could 400 a long agent loop out of existence

`AnthropicProvider` marks the second-to-last message as a cache breakpoint. When
that turn was a pure tool call its text block is empty, and the API rejects the
combination outright: *"cache_control cannot be set for empty text blocks"*, a
400 rather than a warning. Because the marker follows the end of the
conversation, this fires deep into a long one and fails the whole request — the
workflow coding agent lost a 57-message session to it and returned no code, with
the error surfacing as an `unsupported` issue rather than anything naming the
cause.

A turn with nothing to mark is now skipped. Losing one request's cache read
costs tokens; sending the marker on an empty text block costs the request. Only
empty *text* is skipped — a `tool_use` block caches fine, and it is the common
tail of an assistant turn.


### Fixed — `loom check` merged every workflow in a file into one graph

A module holding two workflows produced a single `<stem>.graph.json`, named
after the first and containing the second's nodes as well. The committed graph
exists so that adding or hiding a step shows up in a diff, and that only holds
if a graph covers one flow.

`loom check` now writes a pair of artifacts per workflow, qualified as
`<stem>.<flow>.graph.json` when a module declares more than one and left at
`<stem>.graph.json` when it declares one. `loom graph` and `loom describe` print
a single graph, so they take `--workflow/-w`; `check` needs no selector because
it covers all of them. The stale unqualified artifact is reported as a problem
rather than deleted.

Two extraction fixes came with it. The AST pass now decides which nodes a graph
has — the registry pass knows what a *module* declares, not what a *flow* runs,
so seeding from it put every `@step` in the file into every flow. And a durable
call in a `return` is extracted as its own node: `return await ctx.step(f, x)`
is a step and a return, and it had only been reaching the graph through the
registry catch-all that put it in every flow in the file.

### Fixed — `loom init` scaffolded a project the CLI could not address

The generated `pyproject.toml` had no `[tool.loom] modules`, which is what makes
a bare workflow name resolve. So in a project whose only content came from the
scaffold, `loom run quickstart` failed — as did `loom approve <run> <subject>`,
which has to import the code to resume it. Both failed with a traceback rather
than a hint.

### Fixed — an approval could be granted with nobody approving it

`loom approve`, `loom respond`, and `loom send` delivered the event and looked
the run up afterwards. Those read as equivalent and are not: `Runtime.send_event`
reserves a falsy run id for "every run awaiting this name", and the stores keep
that broadcast row until some run takes it. So `loom approve '' publish` exited
2 saying it found no such run — having queued an approval that the *next* run to
reach that gate consumed, completing a human gate with no human and no record
that anyone had been asked.

All three now resolve the run before they deliver, which is the order
`mcp_server/tools.py` has always used. `Runtime.send_event` refuses an empty run
id outright, so a host calling the engine directly cannot reach the broadcast
path by accident either; `None` still means broadcast, which is what
`ctx.publish` wakes parked runs with.

### Fixed — a SQLite store URL typo ended in a traceback that named nothing

`sqlite3` reports "unable to open database file" without saying which file, so
`LOOM_STORE=sqlite:///runs.db` — three slashes, an absolute path to `/runs.db`
at the filesystem root — surfaced as forty frames ending in a sentence about no
particular path. `SQLiteStore` now raises `ConfigurationError` naming the file
it could not open, and, for a root-level path, the two spellings that work.

The docs said `sqlite:///runs.db` in six places and meant the relative one. This
store takes the name in the URL's authority position: **two slashes for a path
beside you, three for an absolute one** — the opposite of SQLAlchemy's
convention, and left that way deliberately, since `f"sqlite://{path}"` over an
absolute path is how every caller in this repo spells the absolute case and
reinterpreting three slashes would silently redirect those writes.


### Distribution renamed to `loomsdk`

`pip install loomsdk`, `import loom`. The distribution was going to be
`loomflow`, but that name on PyPI belongs to an unrelated, actively-maintained
project (`Anurich/LoomFlow`, last release 2026-07-07) — so `pip install
loomflow` would have installed somebody else's package. Nothing had been
published under either name, so this costs no migration.

The import package is unchanged and always was `loom`. The secondary console
script is renamed to match the distribution: `loom` and `loomsdk` both work,
where the second used to be `loomflow`.

### Renamed — `workflow_builder` is now `loom`

The import package is `loom`; the distribution is `loomsdk`.

```python
from loom import Context, Runtime, step, workflow    # was: from workflow_builder import ...
```

Everything else already said Loom — the `loom` CLI, `$LOOM_STORE`, the
`loom_toolset` and `loom_node` entry points, `[tool.loom]` in `pyproject.toml`.
`workflow_builder` survived only in `import` lines, which is the same
two-names-for-one-thing shape this project keeps finding at the root of its own
bugs.

`import workflow_builder` keeps working and warns once. It forwards submodules
through a meta-path finder rather than re-importing them, so
`workflow_builder.nodes.Node is loom.nodes.Node` — a shim that imported twice
would give you two classes with one name and break every `isinstance` across the
boundary. **Removed in 1.0.**

**One behavioural change.** `MongoStore`'s default database is now `loom`, not
`workflow_builder`. A database name lives in *your* storage rather than in this
code, so switching it silently would point an existing deployment at an empty
database — which reads as "no runs" rather than as an error.
`ensure_indexes()` now checks for the old database and says so:

```
database 'loom' is empty, but 412 runs exist in 'workflow_builder' — the name
this store used before the package was renamed to loom. Pass
MongoStore(uri, database="workflow_builder") to keep using them, or rename the
database.
```

Nothing else in the rename changes behaviour: it is `git mv` plus a sweep of
module paths, including the ones that live in string literals (`tools_module`,
`node_class`, `import_module`, the validator's import allowlist).

Architecture audit remediation. Several capabilities were implemented and unit
tested but never reached by the engine; these changes wire them up, and the
accompanying tests drive each one through the public API rather than calling the
implementing module directly.

### Fixed
- **Saga compensation now runs.** `ctx.compensate()` handlers were registered and
  never executed. The engine invokes them on both failure and cancellation, and
  records any handler that itself failed in `record.metadata["compensation_failures"]`.
- **`ctx.continue_as_new()` now rotates instead of crashing.** `ContinueAsNew`
  subclasses `BaseException` and was not caught, so it escaped `rt.run()`. The
  current run completes, a successor starts from the seed with a clean journal,
  and the chain is linked by `root_run_id` and `metadata["continued_as"]`.
- **Unserializable step outputs raise instead of vanishing.** They were replaced
  by a `{"__unserializable__": ...}` placeholder and served back on replay as if
  real. Durable values now raise `SerializationError`; step *inputs*, which are
  recorded for humans and never replayed, still degrade.
- **`Page` is journalable.** A `@step` returning `Page[T]` could never be
  persisted, so the documented pagination pattern was not actually durable.
- **The MCP `RuntimeBridge` drives a real Runtime.** It previously returned
  hardcoded results (`{"result": "ok"}`) and a fabricated journal.
- **`ctx.agent()` no longer discards `session_id` and `max_turns`.**
- 12 tests that used `asyncio.get_event_loop().run_until_complete()` failed in a
  full-suite run; the global toolset catalog is now isolated per test.

### Added
- **Unified toolset registry.** `ToolsetRegistry` extends `ToolsetCatalog`, so one
  object serves both discovery (`search`/`show`/`stub`, used by the coding agent)
  and execution (`resolve_tools`, used by `ctx.agent()`). `Runtime.toolsets` chains
  to the process-global registry, so `register_toolset()` and `loom_toolset` entry
  points are visible to every Runtime without Runtimes leaking into each other.
- **Toolset identity.** `ToolsetManifest.kind` and `.provider`, a `qualified_id`
  of `<kind>:<provider>:<id>`, an `mcp` reserved prefix, and a registration
  conflict error — so a first-party Jira toolset and an MCP one no longer
  silently overwrite each other.
- **Agent conversation continuity.** `AgentBackend` gains `history`, `agent_id`,
  `max_turns`, and a `supports_history` capability flag; passing `session_id` to a
  backend that cannot honour it raises rather than silently forgetting.
  `Runtime(sessions=...)` defaults to a store-backed session. `Agent.session(key=...)`
  — previously advertised in a docstring but absent — is implemented.
- **Blob offload.** `Runtime(blobs=BlobService(...))` stores oversized journal
  payloads by content hash and references them as `blob:<sha256>`. Binary values
  round-trip through the journal.
- **Configurable coding agent.** `WorkflowCodingAgent(instructions=...)` replaces
  the base prompt, `extra_instructions=` appends, and `allowed_packages=` both
  documents the target environment in the prompt and enforces it in
  `CodeValidator`, which now checks imports against the stdlib plus that allowlist.
- **Explicit tool effects.** `Toolset.from_steps`/`from_callables` accept
  `effects={...}` overrides instead of only guessing from operation names, derive
  `output_schema` from return annotations, and `resolve_tools(effects=...)` can
  hand an agent a strictly read-only toolset.

### Added — production layer

The Phase-5 modules existed but nothing in the engine reached them. They are now
wired, opt-in, and covered by tests that go through `Runtime` rather than
calling the policy objects directly.

- **Flow control.** `@workflow(flow_control=FlowControlPolicy(...))` plus
  `Runtime(admission=AdmissionController())`. Evaluated *before* the record is
  created, so a debounced or skipped trigger leaves no run behind; the in-flight
  slot is released on terminal transitions, which is what makes `singleton` mean
  "one live run". Rejection raises `AdmissionRejected`, carrying the decision so
  "come back later" is distinguishable from "never".
- **RBAC.** `Runtime(role=Role.OPERATOR)` enforces `flow:run`, `flow:cancel`,
  `run:view`, and `run:replay`. `role=None` (the default) enforces nothing.
- **Leader election.** `start_scheduler(elector=LeaderElector(...))` — only the
  lease holder scans for due runs, so many processes can share one store.
- **Retention.** `RetentionManager.compact()` is implemented rather than
  returning an empty result: journals are dropped once a terminal run passes the
  warm cutoff, records deleted much later, and suspended runs are never touched
  however old they look. Adds `ExecutionStore.delete_execution`.
- **Queue ingress.** `triggers/queue.py` with a `QueueBackend` protocol, an
  `InMemoryQueue` reference implementation, and `QueueConsumer`. A message is
  acked only after its run is durably recorded, and the run's idempotency key
  derives from the message — at-least-once delivery, exactly-once execution.
  Failed submits requeue and then dead-letter. Configuration is read from the
  workflow's own `OnEvent` trigger.
- **HTTP surface.** `loom.server.create_app(runtime)` (needs the
  `api` extra) exposes workflows, runs, journals, events, cancel, and replay;
  `LoomClient` is the thin client. This is the realistic answer to using LOOM
  from other languages: authoring stays Python, operation does not have to be.
- **Grant enforcement.** `@workflow(grants=GrantSet(toolsets=["jira:read"]))`
  narrows what `ctx.agent()` can reach. `GrantSet.allows_operation` matches
  `<toolset>[.<group>][:<effect>]`; naming a denied toolset raises `GrantDenied`.
- Cookbook 15 (queue consumer) and 16 (HTTP server).

### Fixed — found while wiring the above
- `Runtime.submit()` accepted `idempotency_key` and never checked it, so a
  redelivered message started a duplicate run. It now resolves to the existing
  run, and the check happens before admission so a redelivery does not consume a
  rate-limit slot.
- `Runtime.submit()` dropped `metadata` and `tags` instead of storing them.
- The `@workflow` overload stubs omitted the new keyword arguments, so type
  checkers rejected valid calls.
- `ExecutionStore.truncate_journal` was declared as taking `from_seq: int` while
  every implementation takes `from_path: str`.

### Added — files, artifacts, and developer tooling

- **`Attachment`** (`storage/attachment.py`) — a file's bytes plus its filename,
  MIME type, and size, in one journalable value. `from_bytes` / `from_path` /
  `from_text`, and `offload(blobs)` to move the content to blob storage while
  keeping the metadata inline.
- **Named, versioned artifacts** (`storage/artifact.py`) — `ArtifactService` over
  blob storage, plus `ctx.put_artifact` / `ctx.get_artifact` /
  `ctx.artifact_versions`. Blob storage is immutable by construction; this is
  the layer that lets a stable name point at changing content. Republishing
  identical bytes resolves to the existing version, and reads are journaled so a
  replay pins the version the original run saw.
- **`S3BlobBackend`** for S3-compatible storage (`pip install loomflow[s3]`),
  and `RetentionManager.compact(blobs=...)` now deletes the blobs it orphans —
  compaction previously reclaimed rows while leaking content forever.
- **Orphan recovery** — runs take a lease (`node_id`, heartbeated at a third of
  `lease_ttl`) and `Runtime.reclaim_orphans()` resumes runs whose worker died.
  Previously a crashed worker left a run `RUNNING` forever, since no timer covers
  a run that is not waiting for one. The `lease_owner` / `lease_expires_at`
  fields existed and nothing wrote them. Wired into `start_scheduler`.
- **Run provenance** — `ExecutionRecord.code_hash` records the workflow body a
  run started from.
- **A working CLI**, installed as both `loom` and `loomflow`:
  `check` (emit `<flow>.graph.json` + `<flow>.description.md`), `graph`
  (`--format mermaid|json|react-flow`), `describe`, and `init`.
- **The visualization pipeline is connected** (`graph/pipeline.py`). The
  `graph/` package — 1,107 lines across eight modules — was imported by nothing.
  `loom check` now runs the registry and AST passes, merges them, narrates the
  result, and verifies the narration mentions every node.
- **React Flow export** (`graph/reactflow.py`) — WGIR as `{nodes, edges}` with
  positions and optional per-node run status from a `RunTrace`.
- **Smoke-running generated code** (`agents/smoke.py`) — the coding agent now
  compiles the code, executes it in a subprocess against `MemoryStore` and
  `MockModelProvider`, and feeds any traceback back into the repair loop.
  `testing/mock.py` was previously imported by nothing.
- Cookbook 17 (attachments + versioned artifacts).

### Changed — persistence is the host's decision, not the workflow's

The coding agent's prompt mandated `from loom.stores.memory import
MemoryStore` and a `Runtime(store=MemoryStore())` in *every* file it generated,
so each workflow chose its own persistence and could not be pointed at Postgres
without editing it. Generated code now declares steps and workflows only.

- **`loom.stores.from_url(url)`** builds a store from
  `memory://` / `sqlite:///path.db` / `postgres://…` / `mongodb://…`, with errors
  that name the `pip install` that fixes a missing driver. Replaces a private
  copy that had been hiding in the MCP bridge.
- **`Runtime.from_env()`** reads `$LOOM_STORE` (default `memory://`), so the same
  workflow runs against memory, SQLite, or Postgres unchanged.
- The coding-agent prompt and the `loom init` template now keep the `Runtime`
  import inside `if __name__ == "__main__"`, so importing a workflow module as a
  library never constructs one.
- `CodeValidator` warns when a generated module constructs a store at import
  time, and does not when it happens under a `__main__` guard.

### Added — prompt caching, and an 88% cut in generation cost

Profiling one generation: **245,000 input tokens against 3,800 output** — a 64:1
ratio. An agent loop resends its whole context every turn, so the system prompt
and tool schemas, identical from the first turn to the last, were paid for 27
times over. Tool definitions alone were 922 tokens per turn.

`AnthropicProvider(cache=True)` (the default) marks three prefixes cacheable:
the system prompt, the tool block, and the conversation up to the last completed
exchange. Measured on the same generation: 229,000 uncached-equivalent input
tokens became **27,000 effective**, with 225,000 served as cache reads.
`cached_input_tokens` is now reported on `Usage`, so the saving is visible
rather than assumed. Pass `cache=False` for a single-shot call, where writing a
cache costs more than it saves.

### Added — one verification pipeline for generated code

The checks existed but were wired into the generator individually, each with its
own shape, so adding one meant editing the orchestrator and only one kind of
failure could drive a repair.

- **`Check` protocol and `CheckPipeline`** (`agents/checks.py`). Stages run
  cheapest-first and stop at the first blocking error — there is no point
  type-checking code that does not compile. Adding a stage is registration, not
  surgery; `stages=` replaces the arrangement outright.
- **Stages** (`agents/stages.py`): compile, static (the AST rules), lint, types,
  smoke, replay, critique. Each wraps a capability that already existed rather
  than reimplementing it.
- **Lint and type checking**, via ruff and mypy when the environment has them.
  Both *skip* rather than fail when absent — a check that cannot run has found
  nothing, and saying so is not the same as passing. Lint selects `F,E9` only:
  style is not correctness and a model should not spend a repair round on line
  length. Type findings are warnings, since generated code is not annotated to a
  strict standard.
- **Replay determinism.** Runs the code twice and compares. The static lint
  catches `datetime.now()` and `random`; this catches what it cannot see.
- **Uniform repair.** Every stage emits `CodeIssue`s and the loop consumes the
  pipeline's output, so a type error and a traceback reach the model by one
  path. Errors that are about the environment rather than the code never drive
  a repair.

### Added — generated fakes, so the sandbox can actually run integration code

`ToolsetManifest.fakes_module` had existed since Phase 3 and was referenced by
exactly one place — `certify.py`, to complain when it was missing. Nothing
populated or used it, so a workflow that talks to a real service could only ever
reach a 401 in the smoke sandbox: it proved nothing, and asking the repair loop
to fix it invited deleting the integration.

`agents/fakes.py` builds the stand-ins from each operation's declared
`output_schema` rather than from a hand-written fixture set — there is one
contract, so a second one cannot drift from it. Values are coerced to the step's
annotated return type through a `TypeAdapter`, so a caller reading `.field` gets
the model it expects rather than a dict. `fakes_module` still wins where it is
set: a generated stub knows the shape of an answer, not its meaning. The stub
keeps the `@step` wrapper, so retry and journalling still apply — the point is
to remove the network, not the durability.

### Fixed — the coding agent could exhaust its turns without producing anything

A tool declared `arguments_json: str` while models naturally send a nested
object, so the call was rejected *before dispatch* and retried until the budget
died — 21 turns, 1 execution, then an exception. It now accepts either shape.

Two guarantees follow from it: `generate()` never propagates an agent-loop
failure, returning the reason as an `unsupported` issue the caller can act on;
and the turn budget is split into `max_discovery_turns` (search, inspect,
resolve, write) and `max_repair_attempts`, because folding them together means a
spec naming several entities starves the repair it then needs.

Also stops warning "No @step found" for a workflow built entirely from toolset
operations — those are steps already, and the prompt says to call them directly.

### Added — the coding agent resolves what a spec refers to, before writing code

A spec names things the way a person says them — "Vishwjeet", "in progress".
APIs match identifiers and their own configured vocabulary, and nothing joins
the two, so a query built from the words in the spec returns **zero rows and no
error**. That reads as "nothing to do" when it means "nobody by that name" or
"this board does not use that status", which is the worst way for a workflow to
be wrong.

- **`call_read_operation`** is a new authoring-time tool: the agent can now
  execute a toolset operation while writing code, where before it could only
  read manifests. Strictly read-only — a `write` or `destructive` operation is
  refused, because authoring must not change the system it is writing code
  about, and a model exploring an API should not be able to send mail by way of
  research.
- **`OperationSpec.resolves`** marks an operation as turning a human's words
  into a stable identifier (`resolves="user"`), and `ToolsetManifest.resolvers()`
  reports them. Generic: any toolset declares its own, and the generated
  documentation tells the agent to resolve before filtering.
- **A resolution ladder in the system prompt.** Named in the spec → resolve now
  and bake the identifier in with the human name beside it in a comment. Comes
  from the workflow's input → resolve at runtime. Ambiguous → do not guess:
  report the match for a read, park on `ctx.wait_for_approval()` for a write.
  Unresolvable → return an error naming what was tried.
- **`ctx.agent()` is for judgement, not lookup.** "Who is Vishwjeet" has one
  correct answer and a tool that returns it; an agent node there puts a
  nondeterministic call, and its cost, into every run to re-answer a question
  settled once while writing the code.

Verified end to end: given a misspelled name, the agent now resolves it during
authoring and emits `ASSIGNEE_ACCOUNT_ID = "712020:…"  # Vishwjeet`.

### Added — toolsets load on demand, and unknown ones are refused

- **The system prompt carries index cards, not every operation.** It previously
  pasted every operation of every registered toolset, so the prompt grew with
  what was installed rather than with the task, and each new integration taxed
  every unrelated generation. Signatures and schemas are one `show_toolset` call
  away. Names and the import line stay, since those are what make a capability
  discoverable and stop an import being invented.
- **Only registered toolsets exist.** A spec needing Slack where no Slack
  toolset is configured previously produced confident code importing a module
  that is not installed. `CodeValidator(available_toolsets=…)` now rejects it,
  and the agent is told to say the task cannot be done here instead — naming
  what it needs and what is available.
- **A refusal reads as a refusal.** Returning no code ran the ordinary
  validators over an empty string and reported "no @workflow found" and
  "missing import" — symptoms of emptiness that buried the reason. An empty
  result now carries the agent's own explanation as a single `unsupported`
  issue.

Also raises the turn budget to `max_repair_attempts + 12`: discovery,
inspection, and entity resolution all precede any code, and they share one
allowance with repair.

### Added — Jira toolset audit

`assign_issue` and `get_project` existed in the client and were reachable from
nothing — the recurring defect in this codebase. A test now asserts every public
client method is reachable from a documented tool. Ten operations become
sixteen, adding `jira_resolve_user` (typo-tolerant name → accountId),
`jira_get_project_metadata` (the status, priority, and issue-type names a board
actually uses), `jira_get_comments`, `jira_assign_issue`, `jira_get_project`,
and `jira_delete_issue`.

`jira_resolve_user` exists because Jira's user search is a substring match:
"Viswajeet" misses "Vishwjeet" entirely and returns an empty list, which reads
as "no such person". It retries with shorter prefixes and ranks by similarity,
returning `exact=False` so a caller can tell a suggestion from a fact.

### Fixed — Claude 5 rejects temperature, and it took the coding agent with it

`AnthropicProvider` sent `temperature` and `top_p` to every model. Claude 5
answers that with a hard 400 — "`temperature` is deprecated for this model" —
and since the coding agent lowers the temperature for determinism, *every*
generation failed on Anthropic. Both are now omitted for `claude-sonnet-5`,
`claude-opus-5`, and `claude-haiku-5`; Claude 4.x still receives them, because
dropping them everywhere would ignore a caller who meant them.

### Fixed — an expired access token shadowed a working refresh token

`GoogleCredentials.mode` preferred `GOOGLE_ACCESS_TOKEN` over the refresh-token
trio. The setup helper prints both, so both end up in `.env` — and an access
token lives about an hour. The result is a setup that works, then silently stops
within the hour, with the durable credential sitting unused beside it. A run
that sleeps outlives its access token by design. The refresh flow now wins when
its three variables are present; a lone access token is still honoured.

### Fixed — a repair that could not run discarded working code

The repair round shares its turn budget with the generation that preceded it, so
a long discovery phase leaves nothing for a repair — and the resulting
`UsageLimitExceeded` propagated out of `generate()`, throwing away a candidate
that may well have been fine. It is now caught: the best code so far is
returned with the smoke result recorded.

### Fixed — registry-generated docs named no import

A toolset registered by manifest was documented as a list of operation ids
(`messages.search`) under a heading about `ctx.agent()`, with no import path
anywhere. Asked to write code against that, a model invented
`from loom import gmail` — a module that does not exist — and did so
confidently, because the operation id was the only name it had been shown.

- `OperationSpec.function` names the `@step` implementing an operation, and
  `ToolsetManifest.tools_module` names the module holding them.
  `ToolsetManifest.import_line()` composes the two, returning `""` unless both
  are present: half an import would be guessed at, which is the original bug.
- `ToolsetRegistry.describe()` now emits that import line and lists operations
  under their function names, so what the agent reads contains code it can
  write. A manifest with no module is labelled *not importable* rather than
  silently looking like one — the distinction between "call this from code" and
  "name this in a prompt" was what got conflated.
- Populated for the first-party toolsets (Gmail, Google Calendar, Jira), with
  tests that import every declared module, resolve every declared function, and
  execute the composed import line. Documentation that names a symbol is a
  promise the symbol exists.

Regenerating the same spec from the registry alone now produces correct imports
in one pass, where it previously needed hand-written `TOOL_DOCS` to avoid
inventing one.

### Fixed — the smoke test certified code that had never run

Found by putting a spec through `WorkflowCodingAgent` and reading what came
back: a workflow importing `from loom import gmail`, a module that does not
exist, marked `is_clean: true` with the smoke run passing.

- **A suspended run is no longer a pass.** The smoke runner only failed on
  `status == "failed"`. A workflow opening with `ctx.sleep(4 minutes)` parks
  before any step executes, and suspended is not failed — so nothing inside was
  ever entered and the unresolvable import went unnoticed.
- **The sandbox now fakes the clock**, as it already fakes the model provider:
  `inline_timer_threshold` is raised and `asyncio.sleep` returns immediately, so
  a long wait costs nothing and the steps behind it actually run. The first
  attempt at this instead *failed* parked runs, which turned out worse — the
  repair loop then pressured the model into deleting the wait, which was the one
  thing the spec had asked for.
- **Environmental failures no longer drive repair.** The sandbox has no
  credentials, so any real integration returns 401 there. Feeding that back as
  "your code failed, fix it" asks the model to repair something that is not
  broken, and the cheapest way to comply is to delete the integration —
  observed: a Gmail workflow came back as a stub returning
  `{"status": "authentication_required"}`, passing smoke because it no longer
  did anything, marked clean. `SmokeResult.environmental` now separates "could
  not verify" from "is broken"; the former skips repair and is recorded as a
  warning that says the code was not executed end to end. A missing import or a
  bad signature stays repairable, since those are what the check exists to find.
- `SmokeResult` gains `steps_executed`, counting only journal kinds that mean
  generated code ran — an allowlist, because `ctx.sleep()` journals a clock read
  of its own and a denylist counts waiting as working.

### Fixed — a workflow file could not import its neighbour

`loom mcp --module flows.py` did not put the file's directory on `sys.path`, so
`import helpers` raised `ModuleNotFoundError` and every multi-file workflow
project broke. Running the same file with `python flows.py` would have worked.

### Fixed — `loom mcp` never ran the scheduler

`ctx.sleep()` parked a run that then never woke, because nothing was scanning
for due timers: durable sleep was unusable through the one surface built to stay
running. The timer loop is now wired into FastMCP's `lifespan`, on by default,
with `--no-scheduler` for when another process owns the store.

### Added — Gmail and Google Calendar toolsets

`toolsets/google/`: two toolsets over one OAuth layer, pure httpx over the REST
APIs, no `google-api-python-client`. Registered separately as `gmail` and
`google_calendar` so a grant can name one without the other — a workflow that
reads a calendar has no business holding a mail-send scope.

- **Gmail** — search (Gmail query syntax), get, send, reply in-thread, modify
  labels, mark read, archive, trash, list labels, profile, and attachment
  download as a LOOM `Attachment` (so it offloads to blobs).
- **Calendar** — list/get/create/update/delete events, quick-add from a phrase,
  free-busy, and list calendars.
- **Auth** — `GOOGLE_ACCESS_TOKEN`, or the client-id/secret/refresh-token trio,
  or a service-account file. Only the last needs a new dependency (a `[google]`
  extra), so the common path adds none. Tokens cache until just before expiry
  and refresh under a lock, so ten parallel steps mint one token; Gmail and
  Calendar share it.

Four decisions worth recording, each of which is a bug in the obvious version:

- **Errors are classified rather than blanket-retried.** The existing Jira
  toolset wraps every call in `Retry(max_attempts=3)`, so a 400 is retried three
  times to get the same answer more slowly. Google 4xx (bar 429) now raises a
  `NonRetryableError` subclass, which `PERMANENT_ERRORS` already stops — no
  per-step configuration needed. A 403 splits on `reason`: quota retries,
  missing scope does not.
- **Sending does not retry.** Gmail has no idempotency key, so a request that
  times out *after* delivery is indistinguishable from one that failed, and an
  automatic retry sends the mail twice. Journaling stops a replay from
  re-sending; it cannot stop a retry within one attempt. Calendar writes do
  retry once, because a duplicate event is visible and deletable.
- **`send_updates` defaults to `"none"`.** Creating a hundred events should not
  email a hundred people as a side effect of a default.
- **Replies set `In-Reply-To` and `References`, not just `threadId`.** The
  thread id threads correctly in Gmail and nowhere else, so a reply that sets
  only that starts a new conversation in every other client.

Also adds `utils.require_any_env` to the cookbook helpers: credentials that come
in alternative complete shapes could not be expressed with `require_env`, which
is all-of and would have demanded a token *and* the refresh trio.

### Added — an MCP server an assistant can actually drive

`loom mcp` serves this Runtime over the Model Context Protocol, so Claude Code,
Claude Desktop, and Cursor can list, run, inspect, and unpark workflows. Built on
the official `mcp` SDK's `FastMCP` (a `[mcp]` extra); `--transport` selects
`stdio` (default), `http`, or `sse`, with `--host`/`--port` for the networked
ones.

- **Ten tools**: `list_workflows`, `run_workflow`, `get_run_status`, `list_runs`,
  `get_run_journal`, `approve_run`, `send_event`, `cancel_run`, `retry_run`,
  `replay_run`. Four resources and five prompts round out the surface.
- **One port, two clients.** The CLI's `CliBackend` and the MCP server's
  `RuntimeBridge` were two names for the same thing. Both now depend on
  `loom.facade.RuntimeFacade`, with `LocalFacade` and `RemoteFacade`
  as the implementations — so `--server URL` works for `loom mcp` exactly as it
  does for `loom run`, and every capability is written once.
- **The server explains states a model misreads.** A suspended run is reported
  with what it is waiting for, the exact tool call that unparks it, and a note
  that suspended is not failure; the server instructions distinguish `retry_run`
  (re-runs from the failure against current code) from `replay_run` (re-executes
  from the journal, repeating no side effect). Unknown runs return an error
  payload rather than raising, which would abort the caller's turn.
- **Workflows advertise their input shape.** `WorkflowDefinition.input_schema()`
  derives JSON Schema from the body's annotation, and `run_workflow` refuses a
  payload whose declared type cannot match — naming the expected type and an
  example — instead of starting a run that dies inside a step. Found by driving
  the server with Claude Code: given no schema it guessed `{"email": ...}` for a
  body annotated `email: str`, and two of three sessions burned a failed run
  before recovering. With the schema, both recovered sessions ran clean first try.

`RuntimeBridge` remains as a deprecated alias that warns and delegates to
`LocalFacade`.

Tested at three levels: the capability functions against a real `LocalFacade`
with no `mcp` import, a real `FastMCP` instance driven in process, and a real
subprocess speaking stdio to the official MCP client through a full handshake and
a suspend-approve cycle.

### Added — a full CLI and a terminal UI

The CLI was authoring-only: four commands, none of which could run a workflow,
read a journal, approve a parked run, or start the HTTP server that had been
added earlier. Thirteen commands join the four.

- **Running** — `run` (with `--input` as JSON, `@file.json`, or a bare string;
  `--follow`, `--detach`, `--idempotency-key`), `runs`, `show`, `watch`.
- **Acting** — `approve` / `--reject`, `send`, `cancel`, `retry`, `replay`.
- **Serving** — `serve` (uvicorn was already a dependency and nothing started
  it), `workflows`, `publish`.
- **`loom ui`** — a Textual UI behind a `[tui]` extra. Runs list, the selected
  run's live journal, and a queue of runs parked on a human that can be approved
  in place; that last pane has no non-interactive equivalent.

Three decisions worth recording:

- **Exit codes are part of the contract**: `0` completed, `1` failed, `2` usage,
  `3` **suspended**, `4` cancelled. A run parked on a human has neither succeeded
  nor failed, and collapsing it into either makes calling scripts misbehave. A
  suspended run prints the command that unparks it.
- **Local and remote share one command set.** `CliBackend` has a `LocalBackend`
  (imports modules, `Runtime.from_env()`) and a `RemoteBackend` (`LoomClient`),
  so `--server URL` is the only thing that changes between them.
- **`argparse`, not Typer.** Typer would be pleasant but would become a hard
  runtime dependency of a package whose install currently pulls only `pydantic`
  and `pydantic-settings`. `rich` is an optional `[cli]` extra; without it every
  command still works in plain text.

Also adds `POST /runs/{id}/retry` — `Runtime.retry()` had no HTTP equivalent
until the CLI needed one in remote mode.

### Fixed
- The TUI's hidden filter input took focus on start, so every keyboard shortcut
  was swallowed as text. It is disabled while hidden, and the run list is
  focused explicitly on mount.
- `Printer.error` passed `file=` to `rich.Console.print`, which does not accept
  it — so every CLI error path raised a `TypeError` traceback instead of
  printing the message. The stream is bound at Console construction.
- `RemoteBackend.journal` returned the server's `name` key while the local
  backend returned `step_id`, so the same journal had two shapes depending on
  which backend answered.

### Added — OpenAI and Gemini model providers

- **`OpenAIProvider`** (`pip install loomflow[openai]`). Also serves any
  OpenAI-compatible endpoint via `base_url` — Azure, Together, Groq, vLLM,
  Ollama. Routes o-series and gpt-5 models to `max_completion_tokens` and omits
  `temperature`/`top_p`, which those models reject; supports strict tool schemas,
  JSON-schema response formats, and per-call `tool_choice`.
- **`GeminiProvider`** (`pip install loomflow[gemini]`), on the
  `google-genai` SDK. Handles the three ways Gemini's format diverges: the system
  prompt is configuration rather than a turn, assistant turns use the `model`
  role, and a function *response* is keyed by function **name** while LOOM tracks
  calls by id. It also strips the JSON Schema keywords Gemini rejects (`$defs`,
  `$ref`, `additionalProperties`), which Pydantic emits for any nested model, and
  reports `TOOL_CALLS` when Gemini returns a function call labelled `STOP`.
- `loom.agents.providers` now exports all three lazily, so importing
  it requires no vendor SDK. `anthropic` also became a named extra.
- Pricing entries for current OpenAI, Anthropic, and Google models, including
  `gpt-5.6-luna` at $0.20 / $1.80 per million tokens. Its siblings `gpt-5.6-sol`
  and `gpt-5.6-terra` are deliberately left unpriced — `estimate_cost` returning
  zero is honest, whereas guessing their rates would be confidently wrong.

Defaults are `gpt-5.6-luna` for OpenAI and `claude-sonnet-5` for Anthropic.

`OpenAIProvider` sends `reasoning_effort="none"` when a `gpt-5.6` model is given
function tools, because that combination is otherwise a hard 400 on
chat/completions. Verified against the live API: the whole `gpt-5.6` generation
needs it, `gpt-5.4`/`5.5` work either way, and `gpt-5`/`gpt-4.1` *reject* the
value — so the override is a narrow list rather than a family prefix. An explicit
`reasoning_effort` from the caller still wins.

### Fixed
- **`estimate_cost` took the first matching prefix, not the longest**, so
  `gpt-4.1-mini-2025-04-14` priced as `gpt-4.1` — five times too much. Any dated
  model id whose family also has a shorter entry was affected.
- **`ctx.agent()` results decoded back as plain dicts on replay.** Neither agent
  `DurableCall` declared an `output_type`, so `result.output` raised
  `AttributeError` on the second attempt but not the first — every workflow using
  an agent was broken on replay.
- **Agent token usage never reached the run total.** `Journal.total_usage()`
  skipped every `AGENT` entry to avoid double-counting children that per-turn
  journalling does not yet create, so an agent run always reported zero tokens
  and zero cost. It now skips the rollup only when the entry actually has
  children.

### Fixed — cookbook

- **The repair loops asked the model to fix code it could not see.** The coding
  agent is ephemeral, so each retry starts a fresh conversation; sending only a
  traceback produced confident nonsense. Both `SmokeResult.as_feedback(code)` and
  `SupervisorVerdict.as_feedback(code)` now carry the source. Caught by running
  cookbook 07, which had started returning uncompilable code.
- Examples now read `.env` at the repo root via `utils.require_env`, so keys
  already committed there work without exporting anything. 06, 07, 08, and 09
  had their own divergent checks; 09 also demanded credentials before `--help`.
- Example 13 drove a real 150-second wall-clock wait to demonstrate cron. It now
  advances a synthetic clock through `dispatcher.tick(now)` and finishes in under
  a second, which is also how you would test a schedule.
- Example 08 gained `--read-only`. Its third query creates a task in a real Jira
  instance; running the demo should not require accepting that. Which queries
  write is marked explicitly on each `Query`, because every spec begins "Create a
  workflow" and any keyword heuristic reads them all as writes.
- `tests/test_cookbook.py` treats the cookbook as documentation that runs:
  structure-checks every example, executes the nine that need no credentials, and
  import-checks the eight that do, so API drift surfaces in CI rather than for
  the next person who copies one.

### Added — hardening

- **Journal growth guards.** `Runtime(journal_warn_entries=5_000,
  journal_max_entries=50_000)`. A forever-flow that never rotates re-reads its
  whole journal on every attempt, degrading quadratically and silently; it now
  warns once, then fails with `BudgetExceeded` naming `continue_as_new` as the
  fix. Set the max to 0 to disable.
- **`ctx.emit(name, payload)`** — a journaled broadcast event, the counterpart to
  `ctx.signal(run_id, ...)`. The graph extractor already mapped `ctx.emit` to a
  node kind; the method did not exist. A drift test now asserts every `ctx.*`
  name the extractor knows about is real.
- **Import-symbol validation.** `CodeValidator` resolves `from X import Y` and
  reports `'loom' has no attribute 'Retryy'; did you mean 'Retry'?`.
  Previously only package *names* were checked, so a real package with a
  misspelled symbol passed validation and failed on the user's machine. Only
  `loom` and the stdlib are imported during validation — importing an
  arbitrary package to check a name would run its side effects.
- **Supervisor review** (`agents/supervisor.py`). `WorkflowCodingAgent(supervisor=
  CodeSupervisor(model))` adds a second model that reviews the finished code
  against the spec for durability, determinism, retry safety, error handling, and
  spec fidelity. It reviews rather than rewrites; the author agent does the
  fixing. A revision that breaks the smoke run is discarded — a reviewer's
  opinion does not outrank working code. A supervisor that errors approves by
  default rather than failing the generation it was advising.
- **Persisted workflow catalog** (`runtime/registry.py`). `await rt.publish(flow)`
  records name, version, `code_hash`, source path, triggers, and input schema —
  never the code, since the file on disk stays the source of truth. Publishing is
  explicit, so importing a module never writes to storage. `rt.published()` lists
  the catalog; `rt.provenance(run_id)` resolves a run to the code that produced it
  by matching `code_hash`. `GET /workflows` now includes published workflows the
  serving process did not import, marked `executable: false`.
- Docstrings for `Context.tag`, `.span`, `.raise_if_cancelled`, and `.sleep_until`,
  enforced by a test that fails on any undocumented public `ctx.*` method.

### Fixed — found while building the above
- **Type annotations were never resolved.** With `from __future__ import
  annotations` — used throughout this SDK and its own templates — a step
  declaring `-> Invoice` recorded the *string* `"Invoice"` as its output type, so
  replay handed the workflow a raw dict instead of an `Invoice`. Silently, and
  only on the second attempt. `resolve_annotations()` now evaluates them.
- **The shipped console script had never worked.** `cli/` (an empty package)
  shadowed `cli.py`, so `loomflow` raised `ImportError`; and `cli.py`
  imported `Workflow` and `WorkflowExecutor`, neither of which exists.
- **`CacheStore.set(key, value, 0)` silently discarded the value**, since zero
  was read as "already expired". Non-positive TTL now means no expiry, which is
  what callers mean. Documented in the protocol.
- **Graph extraction walked whole modules**, so the `if __name__` guard, helper
  functions, and `@step` internals all became graph nodes. Extraction is now
  scoped to workflow bodies.
- The `loom init` template used a stale API and its generated test hit the
  network; the scaffolded project now runs and its tests pass offline.

### Changed
- `resolve_tools()` and `resolve_one()` raise `RegistryError` for an unknown
  toolset id rather than silently returning fewer tools (`resolve_one` previously
  raised `KeyError`).
- `fastapi` moved into the `dev` extra so the in-tree HTTP tests run by default
  instead of skipping.
- `ExecutionStore` gains `delete_execution`; `truncate_journal` is declared as
  `from_path: str`, matching every implementation.

## [0.1.0] - 2026-08-11

### Added
- `TriggerDispatcher` for routing events to workflow triggers
- `MongoStore` — production storage backend using MongoDB (motor)
- `PostgresStore` — production storage backend using PostgreSQL (asyncpg)
- `AgentBackend` protocol — pluggable agent execution layer
- `LangChainBackend` — run agents via LangChain/LangGraph
- `AgnoBackend` — run agents via Agno framework
- `PydanticAIBackend` — run agents via Pydantic AI
- `ToolsetRegistry` with entry-point discovery (`loom_toolset` group)
- Jira toolset (search, create, update, transition, comment, assign)
- Confluence toolset (search, get page, create, update)
- `WorkflowCodingAgent` — ReAct agent that generates workflow code from natural language
- 14 cookbook examples covering sequential, parallel, durable sleep, error handling, human-in-the-loop, AI agents, coding agent, Jira, LangChain, Agno, PydanticAI, cron triggers, and workflow management
- Workflow management tools (`list_runs`, `get_run`, `cancel_run`, `retry_run`)

## [0.0.10]

### Added
- Agent framework integration adapters (LangGraph, CrewAI, Pydantic AI, OpenAI Agents SDK, Claude SDK, Agno, AutoGen)
- Conformance test suite for adapter correctness
- Bi-directional tool conversion (LOOM tools to/from framework-native tools)

## [0.0.9]

### Added
- MCP server with tools, resources, and prompts for Claude Desktop and Cursor
- stdio and SSE transports
- Workflow introspection via MCP resources

## [0.0.8]

### Added
- 10 reference workflows ported from n8n/Gumloop patterns
- Lead outreach, content pipeline, inbox triage, CRM sync, social publisher
- Doc extraction, battle cards, meeting prep, Stripe ETL, PDF chatbot
- Reference specs and reference test suite

## [0.0.7]

### Added
- Small model compatibility layer
- Tiered prompt templates (full, compact, minimal)
- Schema simplification for constrained-context models
- Scaffolding engine for guided code generation
- Code validator and repair pipeline

## [0.0.6]

### Added
- Template system for common workflow patterns
- n8n workflow importer
- Community toolset SDK
- Knowledge, memory, and skill toolsets
- Drift detection for toolset manifests
- Eval framework for generated workflows

## [0.0.5]

### Added
- `PostgresStore` and `MongoStore` (initial implementations)
- Blob service for large payloads
- Flow control: saga/compensation, fan-out/fan-in
- `TemporalBackend` durability port
- HA/leader election for scheduled triggers
- OpenTelemetry tracing integration
- Structural Replay for safe schema migration
- RBAC grant system

## [0.0.4]

### Added
- Graph visualization via WGIR (Workflow Graph Intermediate Representation)
- AST-based skeleton extraction from `@workflow`/`@step` decorators
- Skeleton-first narration: commit-time description generation
- CI golden-set checks for visualization output
- Canvas and run-trace views

## [0.0.3]

### Added
- Three-tier toolset disclosure (index, manifest, full docs)
- Toolset generation pipeline from OpenAPI specs
- `ConnectionBroker` for credential management
- `FilterSpec` for event routing predicates
- Grant system for capability-based access control
- `loom certify` CLI command

## [0.0.2]

### Added
- `AgentExecutor` protocol and `AgentDefinition` registry
- Agent persistence (sessions that survive restarts)
- Hook pipeline (pre/post agent turn)
- Budget enforcement (token and cost limits)
- Coding agent (initial version)
- Mock run system for testing agent workflows

## [0.0.1]

### Added
- Core runtime engine with deterministic re-entry
- Journal-based durable execution
- `@workflow` and `@step` decorators
- `Context` API: `ctx.step()`, `ctx.sleep()`, `ctx.wait_for_event()`, `ctx.spawn()`, `ctx.gather()`
- `MemoryStore` (in-memory, for tests)
- `SQLiteStore` (file-based, for local development)
- Step retry with configurable backoff
- Suspension model (sleep, wait-for-event)
- CLI (`loomflow` command)
- `Tracer` protocol with `NoopTracer`
