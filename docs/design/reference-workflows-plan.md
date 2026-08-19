<!-- docs-illustrative -->

# Reference Workflows — Rebuild Plan

Remediation for [reference-workflows-audit.md](reference-workflows-audit.md).
Supersedes the delivery half of
[phase 8](../../phases/phase-8-reference-workflows.md); the ten chosen workflows
and their specs stand.

---

## 1. What "production ready" has to mean here

Phase 8's exit criteria were *"runs on MemoryStore"* and *"has tests"*, and the
audit is what that bar buys: ten files that satisfy both on paper while five of
them do not import. The bar has to be a property of the code, checked by
something that runs.

A reference workflow is production ready when all nine hold. Each maps to a
LOOM capability that already exists, so this is a checklist over the SDK rather
than new invention:

| # | Property | Enforced by |
|---|---|---|
| 1 | Executes end to end with no credentials, against fakes | `agents/fakes.py`, a CI job |
| 2 | Every external call goes through a **toolset step** or a **node** — no bare `httpx` in an example | AST rule in the example gate |
| 3 | Credentials resolve from the environment or `ConnectionBroker`; **never** a workflow input, never a step argument | AST rule + §3.1 redaction |
| 4 | Declares its **trigger** (`Schedule`, `Webhook`, `OnEvent`, `OnAppEvent`) | `WorkflowDefinition.triggers` non-empty |
| 5 | Every outward-facing write is gated by `ctx.wait_for_approval` or a `human.*` node, or documents why not | review; `AutoRespondChannel` in tests |
| 6 | Fan-out is bounded (`ctx.map(max_concurrency=…)` or `control.throttle`) | AST rule: no `ctx.gather` over an unbounded comprehension |
| 7 | Declares `grants=GrantSet(...)` naming exactly the toolsets it uses | `Runtime.register()` already raises on a bad grant |
| 8 | Replays identically — `assert_replays(wf, payload)` | test |
| 9 | Ships a committed `.graph.json` + `.description.md`, checked with `loom check --fail-on-change` | CI |

Properties 2, 3, and 6 are new AST rules. They belong in the example gate rather
than in `CodeValidator`, because a hand-written workflow may legitimately call
`httpx` — a reference example may not, since its whole job is to show the way
the library is meant to be used.

---

## 2. Sequencing

Six phases. **Phase 0 is not optional and nothing else should start before it**
— every later phase produces files that decay on the same schedule as the
current ten unless something executes them.

```
P0  gate + unbreak            ─┐
P1  SDK gaps (blocking)       ─┼─→ P2  rewrite the six buildable today
                               │
P3  four new toolsets         ─┴─→ P4  RAG primitives → wf10
                                   P5  social publishing → wf02, wf05
```

---

## Phase 0 — Make the examples executable, and un-break the five — **LANDED**

**Why first:** the audit's root cause is that no gate reaches `examples/`. Fixing
the five import errors without this buys a green suite that rots again.

### What shipped

| Task | Delivered |
|---|---|
| 0.1 | `scripts/run_examples.py` — runs every `examples/**/*.py`, sharing `docs_examples.py`'s environmental classifier. Scripts run in place (the cookbook imports its own `utils`); reference workflows are **imported**, which is what a wrong keyword argument fails. |
| 0.2 | CI job `examples`, under `[dev,all]`, `--timeout 400`. |
| 0.3 | CI lint is now `ruff check src tests scripts examples`. |
| 0.4 | `tests/test_phase8.py` rewritten: 63 tests that import, **run**, and `assert_replays` all ten, over `run_with(...)` + `given(...)` seeds — no network, no credential, no mocked HTTP client. Was 104 substring searches. |
| 0.5 | `Retry(delay=…)` → `initial_delay=` in wf06–wf10. All ten import. |
| 0.6 | `tests/test_example_conventions.py` — the three AST rules, with the current violations recorded per file and a stale-entry check so the list can only shrink. |

### What it found on its first run

The gate justified itself immediately — four cookbook examples and two library
defects, none of which any existing check could see.

**`tests/test_phase8.py` had 104 tests over ten workflows and could not observe
any of them.** The replacement has 63 and observes all ten. Fewer tests, and
the first ones that can fail for the right reason.

| Found | Was | Disposition |
|---|---|---|
| **`Runtime.run(wf, LeadConfig(...))` was refused** | `JSON_TYPES["object"] = (dict,)`, so a workflow declaring `config: LeadConfig` rejected a `LeadConfig` — with an error naming the type it refused as the type it wanted. | **Fixed** in `runtime/validation.py`; two tests in `test_replay_safety.py`. A LOOM defect, not an example bug. |
| **wf10 crashed on its first delivered event** | `ctx.wait_for_event("user_question", default=None)` returns the raw payload; the body annotated it `ChatMessage` and read `.question`. Only reachable once a question actually lands, so a run that timed out looked fine. | **Fixed** — `output_type=ChatMessage`. |
| `08_jira_agent` (5m17s), `10_langchain_react_agent` (2m17s) | Not defects — genuinely slow agent examples. | Runner default raised to 300s; CI uses 400s. |
| `18_gmail_calendar` reported as a failure | `require_env` prints to **stdout** and exits 1, and the classifier read only stderr. | Classifier now reads both streams; `"missing env vars"` added to `ENVIRONMENTAL`. Reports **skipped**. |
| `09_jira_cli` reported as a failure | An interactive CLI correctly refusing an empty stdin. | New `run-examples:` directive convention; the file declares `--example 1`. |

Two scoping calls worth recording. `no-bare-http` applies to
`examples/reference/` only — a reference workflow's subject *is* the
integration, while `16_http_server.py` legitimately uses `httpx` to call the
server it just started. And `02_parallel.py` is allowlisted for
`bounded-fan-out` rather than capped: it is the example whose subject is fan-out,
and adding `max_concurrency` to satisfy a rule aimed at production workflows
would blunt the lesson.

**Not fixed, and not mine:** `ruff check src tests scripts` reports 5 pre-existing
errors (RUF059 ×4, RUF043 ×1) in `test_event_dispatch.py`, `test_scheduling.py`,
and `test_toolsets_salesforce_hubspot.py`. They reproduce against `HEAD` on
unmodified files, so the lint job is red independently of anything here —
likely a ruff newer than CI's pin.

| Task | Detail |
|---|---|
| 0.1 | `scripts/run_examples.py` — execute every `examples/**/*.py` in a subprocess with a timeout, reusing `docs_examples.py`'s `ENVIRONMENTAL` classifier so a missing extra reports **skipped**, not passed. |
| 0.2 | New CI job `examples`, alongside `docs-examples`, running 0.1 under `[dev,all]`. |
| 0.3 | Extend CI lint to `ruff check src tests scripts examples`. |
| 0.4 | Replace `tests/test_phase8.py` outright. Structural greps go; each workflow gets a real test that imports it, runs it under `run_with(...)` + `given(...)` seeds, and calls `assert_replays`. |
| 0.5 | Fix `Retry(delay=…)` → `initial_delay=` in wf06–wf10, so the five import while Phase 2 is in flight. |
| 0.6 | Add the three AST rules from §1 (properties 2, 3, 6) to the gate — failing loudly for now, since all ten violate them until Phase 2 lands. Land the rules **disabled with an allowlist** of the ten current files; Phase 2 removes entries as it rewrites. |

**Exit — met.**

```
$ python scripts/run_examples.py
38 ran, 1 skipped (missing dependency, service, or credential), 0 failed
```

The one skip is `18_gmail_calendar.py`, which has no Google credentials here.

**And the alarm rings.** Re-injecting the original defect — `Retry(delay=1.0)`
in wf01 — fails both new gates and would have passed the old one:

| Check | Result |
|---|---|
| `scripts/run_examples.py` | `FAIL … TypeError: Retry.__init__() got an unexpected keyword argument 'delay'`, exit **1** |
| `test_phase8.py::test_workflow_imports[wf01]` | **FAILED** |
| the grep it replaced (`assert "Retry(" in src`) | 4 matches — **passes** |

---

## Phase 1 — SDK gaps that block more than one workflow — **LANDED**

| Task | Delivered |
|---|---|
| 1.1 | `loom/core/redaction.py` + wiring in `context.py`, `engine.py`, `facade.py`. `Runtime(redact_keys=…)`. 46 tests. |
| 1.2 | `transform.parse_document` (`loom/nodes/documents.py`), `[documents]` extra. 19 tests. |
| 1.3 | `io.http_request` takes `connection=`, resolved through `Runtime(connections=ConnectionBroker())`. 10 tests. |

Three things the work changed about the plan, each because the premise was
wrong:

**`loom.Secret` already existed, and is stronger than what was planned.** The
plan said to recognise `pydantic.SecretStr` and re-export it as `loom.Secret`.
But `loom/core/secret.py` already ships a `Secret` that *refuses to serialize*
— a step returning one fails loudly at write time instead of leaking quietly at
read time — and `SecretStr` already encoded as `'**********'` with no help from
LOOM. So the re-export was dropped (it would have put two different `Secret`s
one autocomplete apart, the collision `loom.nodes` exists to avoid), the
pydantic behaviour is now pinned by a test rather than reimplemented, and the
module was renamed `redaction.py` to stop colliding with `secret.py`.

**A `Secret` argument used to erase the whole recorded input.** Because it
refuses to encode, `_encode_debug` degraded the entire `{"args": …}` dict to
`{"__unserializable__": "dict"}` — safe, and telling a reader nothing about the
arguments beside it. Secret-typed values are now replaced *before* the encoder
sees them, so siblings survive.

**The denylist cannot see a positional argument, which is how every credential
is actually passed.** `ctx.step(parse, text, api_key)` has no key to match. So
a step call now binds its arguments to the function's signature first. Without
that, 1.1 would have closed the rarer half of the defect.

**And the two halves collided.** `io.http_request`'s new field was called
`credential`, which the 1.1 denylist promptly redacted out of the journal —
erasing the one part that is safe and useful to record. Renamed to
`connection`, which is what it actually holds and matches `ConnectionBroker`'s
own vocabulary.

### 1.1 Journal secret redaction (P0 — affects every LOOM user)

Reproduced in the audit §3.1: a credential passed to a step is durably recorded
and printed by `loom show`. This is not a reference-workflow bug; it is a
library defect these workflows exposed.

Design, following the codebase's existing shape:

- Recognise `pydantic.SecretStr` / `SecretBytes` in the input-recording path and
  write `"***"`. Journal *payloads* are unaffected — they must round-trip.
- Add `loom.Secret` as a re-export so a workflow author has one obvious name.
- A configurable key-name denylist (`Runtime(redact_keys=…)`, defaulting to
  `token`, `api_key`, `secret`, `password`, `authorization`) applied to recorded
  kwargs and to `dict` inputs.
- Document the residual honestly: a secret concatenated into a positional string
  cannot be detected, which is a further reason property 3 forbids passing one
  at all.

**Test:** the audit's reproduction, inverted — assert `sk-` never appears in a
loaded journal.

### 1.2 Document extraction node (blocks wf06, wf10)

`io.parse_document` (or `transform.extract_document`), taking an `Attachment` or
a `blob:` ref and returning text plus per-page offsets. Optional `[documents]`
extra (`pypdf`, `python-docx`); absent, the node raises the standard "requires"
error the registry already renders. Fakes come from `output_schema` for free.

### 1.3 Credential-aware `io.http_request` (unblocks every un-toolsetted API)

Today the node takes a raw `headers` dict, so the only way to call an
un-toolsetted service is to journal a token. Add `credential="stripe"`, resolved
through `ConnectionBroker` at dispatch and never recorded. This is what lets a
reference workflow legitimately call something LOOM has no toolset for while
still satisfying property 3 — and it lowers the urgency of Phase 3.

**Estimate:** 1.1 ≈ 2 days, 1.2 ≈ 2 days, 1.3 ≈ 1 day.

---

## Phase 2 — Rewrite the six that are buildable today — **LANDED**

All six rewritten onto toolsets, nodes, triggers, grants, and human gates.
`tests/test_phase8.py` grew from 63 tests to 87: each rewrite added a second
`Case` variant for the branch a happy-path test leaves unasserted — a reviewer
saying *no*, a volume approval refused, an empty calendar.

**The allowlist shrank from 11 entries to 5**, which is the mechanism working
as designed: what remains is exactly `wf02`, `wf05`, `wf09`, `wf10` — the four
deferred to Phases 3–5 — plus `02_parallel.py`, which is deliberate.

### Three engine defects the rewrites surfaced

Each is the same shape: **a capability was demanded when the call was
constructed, so it was demanded on every body re-entry — including the ones
where the journal already held the answer.**

| Was | Now |
|---|---|
| `ctx.agent("…")` raised `ConfigurationError` at construction with no `agent_backend`, so a finished run could not be replayed in a process with no model | Checked inside `perform`, which a recorded call never reaches |
| `ctx.node(...)` checked `spec.requires` at construction, so a completed `human.review_edit` could not be replayed without a human channel | `NodeRegistry.check_requirements` moved into the journaled call |
| `Runtime.run(wf, LeadConfig(...))` refused the model the workflow declared | Fixed in Phase 1 — an object schema now admits a model instance |

The property the eager node check existed for is unchanged and pinned by a
test: *a fresh* run with no channel still fails **before it parks**, because a
run parked with nobody listening is indistinguishable from patience. What
changed is only that a recorded answer is an answer, whatever this process can
reach. `tests/test_replay_safety.py::TestReplayDoesNotNeedTheCapability` holds
all four directions.

### What each became

| # | Toolsets | Nodes | Trigger | Human gate |
|---|---|---|---|---|
| 01 | exa, hubspot, gmail, slack | `agent.extract_structured` | `Schedule` | `human.review_edit` per draft, between `gmail_create_draft` and `gmail_send_draft` |
| 03 | gmail, google_calendar, slack | `agent.classify`, `agent.extract_structured` | `Poll` | an *unconfident* verdict routes to a review channel rather than being filed by the guess |
| 04 | salesforce, hubspot, slack | `control.batch` | `Schedule` | `human.approval` above a volume threshold |
| 06 | gmail, google_drive | **`transform.parse_document`**, `agent.extract_structured`, `agent.summarize` | `OnAppEvent` | none — reads and writes only inside the org |
| 07 | exa, confluence, slack | — | `Manual` | `human.review_edit` before publishing claims about competitors |
| 08 | google_calendar, google_meet, google_drive, exa, slack, asana | `agent.summarize`, `agent.extract_structured` | `Schedule` | `human.review_edit` before filing tasks against named people |

**wf04's vendor swap went ahead** — Salesforce → HubSpot, as recommended. Every
property the Airtable version demonstrated survives; the Airtable variant
returns in Phase 3 with no change to the shape.

**wf08 got the biggest upgrade.** Its "wait for a `meeting_transcript` event"
was parked on something nothing would ever send. It now reads Meet's own
artifacts — and a transcript is a *Google Doc*, so `drive_export_file` is the
call and `drive_download_file` is a 403 that reads as a permissions problem.
Because Meet reports a transcript before its Drive file exists, the workflow
polls with `ctx.sleep` rather than assuming, which is what
`MeetRecording.is_ready` exists to warn about.

### The original plan, for reference

| # | Was | Becomes | Toolsets | Nodes | Trigger | Human gate |
|---|---|---|---|---|---|---|
| 01 | `lead_outreach` | scrape → enrich → draft → **approve** → send | `exa`/`tavily`, `hubspot`, `gmail` | `agent.extract_structured`, `control.throttle` | `Schedule` | `human.review_edit` on the drafted mail, then `gmail_create_draft` → approve → `gmail_send_draft` |
| 03 | `inbox_triage` | search → classify → route → calendar | `gmail`, `slack`, `google_calendar` | `agent.classify`, `agent.extract_structured` | `Poll` or `OnAppEvent("app.gmail.message")` | none — read + route only, documented |
| 04 | `crm_sync` | source → upsert → mark → notify | `salesforce` → `hubspot` (both shipped) | `control.batch`, `control.dedupe` | `Schedule` | `human.approval` above a configurable record count |
| 06 | `doc_extraction` | attachment → parse → extract + summarize → store | `gmail`, `google_drive` | **`io.parse_document`** (1.2), `agent.extract_structured`, `agent.summarize` | `OnAppEvent("app.gmail.message")` | none — read + store |
| 07 | `battle_cards` | research → analyse → write doc | `exa`/`tavily`, `google_drive` or `confluence` | `agent.judge`, `agent.summarize` | `Manual` / `Webhook` | `human.review_edit` before publishing |
| 08 | `meeting_prep` | calendar → research → brief → **wait** → actions → tasks | `google_calendar`, `google_meet`, `google_drive`, `exa`, `slack`, `asana`/`jira` | `agent.summarize`, `agent.extract_structured` | `Schedule` | `human.approval` before filing tasks |

Two notes worth carrying into the rewrites.

**wf08 gets materially better.** Its "wait for a transcript event" was a
placeholder; LOOM now has the real path — `meet_list_recordings` reports the
artifact, `drive_export_file` reads the transcript Doc, and
`MeetRecording.is_ready` is why the wait is still needed. The workflow becomes a
demonstration of a documented API trap rather than a stub.

**wf04's vendor swap.** Airtable does not exist as a toolset. Retargeting the
same pattern to Salesforce → HubSpot keeps every property the workflow
demonstrates (batched upsert, continue-on-error, write-back, notify) and ships
in Phase 2 instead of Phase 3. The Airtable version returns in Phase 3 as a
variant if wanted. **This is the one place the plan changes what a workflow
*is*, and it wants sign-off.**

**Estimate:** ~1.5 days each including tests, graph, and description ≈ 9 days.

---

## Phase 3 — Four new toolsets — **LANDED**

All four shipped, plus the Stripe `EventSource` and the wf09 rewrite. LOOM now
carries **27 toolsets and 354 operations**, up from 25 and 320.

| Delivered | Ops | Tests |
|---|---|---|
| `toolsets/stripe/` + `StripeSource` | 12 | 46 |
| `toolsets/quickbooks/` | 9 | 32 |
| `toolsets/airtable/` | 7 | 28 |
| `toolsets/google/sheets/` | 6 | 23 |
| `RowIdPaging` in `pagination.py` | — | 7 |
| wf09 rewritten onto Stripe + QuickBooks | — | 12 (four branches) |

### The theme: idempotency, three ways

Grouping these four is not filing — **each handles the same problem
differently, and the difference is the API's, not a preference.** Stripe has a
first-class `Idempotency-Key` and replays the original response for 24 hours,
so its writes *do* retry, and the key is a required parameter rather than
something the client mints. QuickBooks and Airtable have none, so their creates
carry `Retry(max_attempts=1)` and the way to make one safe across runs is to
stamp an external id and look for it first. Sheets is the same: append is not
retried, update is.

wf09 is where all three meet, and it is why the pairing was worth building:
Stripe's write needs a key, QuickBooks' needs a lookup, and the amounts are in
different units on either side — `4200` against `42.00`.

### Two defects the checks caught, both mine

**`verify_effect_profile` earned its place immediately.** It caught two
operations declared `idempotent=False` sitting on a bare `@step` — which
**retries three times**. A docstring saying "not retried" over a decorator that
retries is worse than no docstring. The same mistake turned out to be in **six
Phase 2 reference-workflow steps**, where nothing was checking; all six now
carry `Retry(max_attempts=1)`.

**`test_effect_guess.py` found a gap in the name heuristic.** The destructive
verb list knew `delete/archive/trash/unshare/end` and not **`clear`**, so
`sheets_clear_range` guessed WRITE against a declared DESTRUCTIVE — an
under-classification, which that suite treats as a hard failure because
over-classifying costs an approval and under-classifying costs a deletion.
`clear` is now in the list. That gap would have applied to any user-written
toolset with a `clear_*` operation.

### One new paging dialect

Stripe's `starting_after` takes **the id of the last row on the current page**,
and the envelope carries only `has_more` — so the continuation is not in a field
at all, which no existing style could express. `RowIdPaging` is that, and an
empty page ends the walk whatever `has_more` says, because there is no row to
continue from.

Per-toolset detail is in `src/loom/toolsets/CLAUDE.md`.

### The original plan, for reference

Each follows the shipped three-file pattern (client, tools, manifest) plus a
contract test, and is checked by `tests/test_manifest_imports.py`.

| Toolset | For | Effort | The trap to encode |
|---|---|---|---|
| **Airtable** | wf04 (faithful variant) | S | Offset pagination; a field is addressed by *name* by default and by id under a flag — the wrong one writes nothing and reports success. 5 req/s/base hard limit. |
| **Stripe** | wf09 | M | `Idempotency-Key` is first-class and must be plumbed, not optional; webhook signature verification belongs behind an `EventSource`, not in the workflow. Cursor pagination via `starting_after`. |
| **Google Sheets** | wf01 (tracking sheet) | S | Rides the existing `toolsets/google/auth.py` — one more separately-grantable toolset beside the four already there. A1 ranges, `valueInputOption` (`RAW` vs `USER_ENTERED`) silently changes what is stored. |
| **QuickBooks** | wf09 | M | OAuth2 with mandatory refresh (the `salesforce` client is the template); realm id is part of every path; sandbox and production are different hosts, as with Salesforce. |

Stripe additionally wants an `EventSource` (`loom/events/sources/stripe.py`)
so `stripe_etl` is triggered by a **verified** webhook through `WebhookIngress`
rather than by a workflow that trusts its input. `verify` gets the raw body,
per the existing contract, and `loom.testing.conformance.verify_event_source` is
the kit.

wf09 then rewrites onto Stripe + QuickBooks + Slack, with `human.approval` above
a configurable amount.

**Estimate:** Airtable 2d, Sheets 2d, Stripe 4d, QuickBooks 4d, wf09 rewrite 2d.

---

## Phase 4 — Knowledge primitives, then wf10 — **LANDED**

| Delivered | Tests |
|---|---|
| `loom/knowledge/` — `EmbeddingProvider`, `VectorStore`, `StoreBackedVectorStore`, `split_text`, `cosine` | 40 |
| `OpenAIEmbeddings`, `GeminiEmbeddings`, `MockEmbeddings` | — |
| `knowledge.chunk` / `knowledge.index` / `knowledge.search` nodes | — |
| `Runtime(embeddings=…, vectors=…)` | — |
| `verify_vector_store` conformance kit | 3 bite-proofs |
| wf10 rewritten | 10 (three branches) |

**The allowlist is now 3 entries** — wf02, wf05, and the deliberate
`02_parallel.py`. Every reference workflow except the two blocked on gated
social APIs is on the toolset layer.

### One real bug, caught by a test that nearly did not exist

`MockEmbeddings` built vectors with `struct.unpack("f", digest_bytes)` — and an
arbitrary 32-bit pattern is a NaN or an infinity often enough to matter. One
NaN component makes the whole normalised vector NaN, every score against it
comes back NaN, every comparison with NaN is False, and the ranking silently
collapses. From a *test* provider, which is the worst place for it: every RAG
test in every downstream project would have been asserting against noise.

It now reads four digest bytes as an unsigned integer mapped into `[-1, 1]`,
which cannot produce either. Verified over 2000 vectors: zero NaN, zero
infinities, self-similarity exactly 1.0.

### The design, and what it refuses

**LOOM ships no vector database** — two ports, one reference store over
capabilities every LOOM store already has, and a conformance kit. Same position
`loom/events/` takes about brokers.

**A search always returns something**, which is the failure the whole subsystem
is shaped around. Every `Match` carries its score, `min_score` is how a
workflow refuses one, and `dropped_below_threshold` is reported because
"nothing scored well" and "the index is empty" are different facts.

**Two embedding models occupy two different spaces**, and the arithmetic across
them succeeds while meaning nothing — so a namespace records its model and
refuses another, and a dimension mismatch raises rather than ranking by noise.

**Chunk ids are derived from content**, so re-indexing updates rather than
doubling — which is what lets wf10's `continue_as_new` hand its successor the
same namespace without rebuilding it.

The conformance kit is proved to bite three ways: an upsert that appends, a
metadata filter applied after `top_k` rather than before, and an index that
accepts a second embedding model.

### The original plan, for reference

Follow the event-backbone shape exactly — **ports, one reference implementation,
a conformance kit, and no bundled vendor**:

| Piece | Shape |
|---|---|
| `EmbeddingProvider` | Protocol beside `ModelProvider`; one method, `embed(texts) -> list[Vector]`. `AnthropicProvider` has no embedding endpoint, so ship `OpenAIEmbeddings` and `GeminiEmbeddings` lazily under the existing extras, plus a deterministic `MockEmbeddings` for tests. |
| `VectorStore` | Protocol — `upsert`, `query`, `delete`, `count`. `MemoryVectorStore` as the reference; `SQLiteVectorStore` if cheap. Hosts adapt pgvector / Pinecone / Qdrant themselves. |
| `loom.testing.conformance.verify_vector_store` | So a host proves its adapter, exactly as `verify_event_log` does. |
| Nodes | `knowledge.chunk`, `knowledge.index`, `knowledge.search` — chunking is a `@pure` rule, so it is a node not an agent. |
| `Runtime(embeddings=…, vectors=…)` | Two more ports on the Runtime; both default to `None` and nothing is enforced unless composed in. |

Phase 6 of the original phasing already lists "knowledge/memory/skill toolsets";
this is that work, pulled forward because a reference workflow depends on it.

wf10 then becomes: `OnAppEvent` trigger → `io.parse_document` → `knowledge.chunk`
→ `knowledge.index` → chat loop over `ctx.wait_for_event` → `knowledge.search` →
`ctx.agent` → `slack_post_message`, with **`ctx.continue_as_new` every N turns**
so the journal stays bounded — which is the pattern this workflow should have
been teaching all along.

**Estimate:** ports + memory impl + conformance 5d, nodes 2d, providers 2d,
wf10 rewrite 2d.

---

## Phase 5 — Social publishing (wf02, wf05) — **LANDED**

Option 1 taken: **retargeted to channels LOOM can reach.** Both workflows keep
every pattern the originals demonstrated and now actually run.

| | Was | Is | Tests |
|---|---|---|---|
| wf02 | LinkedIn + X + generated image | exa → per-channel drafts → `human.review_edit` → Slack, Teams, Confluence | 3 branches |
| wf05 | LinkedIn + X + Facebook | variants → dedupe (batch **and** history) → `agent.judge` → approval → Slack, Teams | 3 branches |

**The allowlist is now one entry** — `02_parallel.py`, deliberately. Every
reference workflow satisfies all three AST rules.

Three decisions worth recording:

**The image is gone, and that is written down rather than faked.** LOOM has no
image provider, and an `ImageProvider` port for one example is a subsystem in
search of a user. When one exists, wf02 gains a step; until then the docstring
says so, which beats a step that pretends.

**The specs were rewritten to match.** They drive the coding-agent eval, so a
spec describing LinkedIn against code publishing to Slack would make that eval
measure nothing. Changing the workflow without changing the spec is the drift
this whole audit was about.

**wf05 uses `ctx.state` for cross-run dedupe, and feeds the read into a step.**
Two runs a week apart producing the same post is the failure that matters, and
a within-batch check cannot see it. But `ctx.state` is deliberately not
journaled, so branching on it directly is not replay-safe — passing it into a
journaled step records the *decision* even though the read is not. The step's
arguments then legitimately differ on replay, which is exactly the case
`VerifyMode.WARN` exists for and why the default is not `STRICT`. History is
written only for what actually posted, so a failed publish can be retried
rather than looking said.

### The original plan, for reference

The only place blocked by something outside the repository.

- **LinkedIn**: posting requires the Community Management API, which is
  partner-approved. Not something a reference example can assume.
- **X/Twitter**: write access is paid-tier only.

Three ways to go, in preference order:

1. **Re-target to channels LOOM can reach.** wf02 becomes a content pipeline
   publishing to Slack / Teams / Confluence / a Drive doc. Every pattern the
   originals demonstrate — parallel generation, dedupe, per-platform fan-out,
   partial failure — survives intact, and the workflow actually runs. **Recommended.**
2. **Ship the toolsets with credentials that most readers cannot obtain,** and
   have the examples degrade to "would have posted" without them — the pattern
   `27_meeting_prep.py` already uses.
3. **Drop wf02/wf05** and replace them with two workflows drawn from the same
   n8n top-10 that LOOM can serve end to end.

Image generation (wf02) is a separate small gap: either an `ImageProvider` port
mirroring `ModelProvider`, or drop the image and note why.

**Estimate:** 3d under option 1.

---

## 3. Rough total

| Phase | Days |
|---|---|
| 0 — gate + unbreak | 2 |
| 1 — SDK gaps | 5 |
| 2 — six rewrites | 9 |
| 3 — four toolsets + wf09 | 14 |
| 4 — knowledge primitives + wf10 | 11 |
| 5 — social | 3 |
| **Total** | **~44 engineer-days** |

Phases 3, 4, and 5 are independent of each other and parallelise. Phases 0 and 1
do not — everything depends on them.

**If only one thing gets done: Phase 0.** It converts a suite that is green over
dead code into one that fails when an example breaks, and it is two days.

---

## 4. Open decisions

Three, all in Phase 2/3/5, all needing a call before that phase starts:

1. **wf04** — retarget to Salesforce → HubSpot now (Phase 2), or hold it for an
   Airtable toolset (Phase 3)?
2. **wf09** — build Stripe + QuickBooks toolsets, or demonstrate the same ETL
   shape against a credential-aware `io.http_request` (Phase 1.3)?
3. **wf02 / wf05** — retarget to reachable channels, ship gated toolsets that
   degrade, or replace both workflows?
