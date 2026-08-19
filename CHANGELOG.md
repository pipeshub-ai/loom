# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
