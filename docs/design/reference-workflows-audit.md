<!-- docs-illustrative -->

# Reference Workflows — Audit

**Subject:** the ten n8n/Gumloop workflows in `examples/reference/`, built under
[phase 8](../../phases/phase-8-reference-workflows.md).

**Verdict:** five of the ten do not import. The other five have never been run
against realistic data. `tests/test_phase8.py` reports **104 passing tests** over
all ten, because every one of those tests is a substring search against the file
text — nothing in the repository imports, constructs, or executes a reference
workflow. The suite is green over dead code.

This page is the evidence. The remediation is
[reference-workflows-plan.md](reference-workflows-plan.md).

---

## 1. The measurement

```
$ python -m pytest tests/test_phase8.py -q
104 passed in 0.18s

$ python -c "import reference.wf06_doc_extraction"
TypeError: Retry.__init__() got an unexpected keyword argument 'delay'
```

Import each of the ten, then run each with every HTTP call stubbed:

| # | Workflow | Imports | Runs | Detail |
|---|---|---|---|---|
| 01 | `lead_outreach` | yes | completed | zero rows — never exercised |
| 02 | `content_pipeline` | yes | completed | zero rows — never exercised |
| 03 | `inbox_triage` | yes | completed | zero rows — never exercised |
| 04 | `crm_sync` | yes | completed | zero rows — never exercised |
| 05 | `social_publisher` | yes | **failed** | `KeyError: 'choices'` — parses a raw OpenAI envelope with no guard |
| 06 | `doc_extraction` | **no** | — | `Retry(delay=...)` |
| 07 | `battle_cards` | **no** | — | `Retry(delay=...)` |
| 08 | `meeting_prep` | **no** | — | `Retry(delay=...)` |
| 09 | `stripe_etl` | **no** | — | `Retry(delay=...)` |
| 10 | `pdf_chatbot` | **no** | — | `Retry(delay=...)` |

`Retry`'s field is `initial_delay`. The five that import use it; the five that
were written later use `delay`, which has never existed.

---

## 2. Why nothing caught it

Three gates exist and none of them looks at this directory.

**`tests/test_phase8.py` never executes anything.** Every assertion is
`wf_file.read_text()` plus `in`. The "SDK pattern coverage" class asserts
`"ctx.gather(" in src`. A file consisting of that string in a comment passes.

**`scripts/docs_examples.py` does not reach `examples/`.** Its docstring names
precisely this bug —

> Running an example is the only check that catches a *wrong* API. Missing
> imports fail a linter and bad syntax fails a compiler, but
> `Retry(backoff=2.0)` resolves every name, compiles cleanly, and raises
> `TypeError` the moment anyone follows the docs.

— and `markdown_files()` returns `README.md` and `docs/**/*.md`. Python files
under `examples/` are extracted from nothing and executed by nothing.

**CI lint is `ruff check src tests scripts`.** `examples/` is excluded, and ruff
would not have caught a wrong keyword argument anyway.

By contrast `examples/cookbook/` is healthy — spot-checking `01`, `17`, `20`,
`24`, `27` runs all five clean. It stays healthy by being hand-maintained, not
by being gated, which is the same exposure one commit away.

---

## 3. What they were written against

`git log -- examples/reference` is two commits: the initial import, and a
directory move. Everything LOOM has shipped since arrived after these files
stopped changing — 25 toolsets, 25 nodes, agent backends, human channels, the
event backbone, artifacts and blobs, cron and triggers, grants, hooks, the
sandbox, and `loom.testing`. The reference workflows use **none** of it.

Every external call is a hand-rolled `httpx.AsyncClient()` against an invented
hostname:

```
https://api.enrichment.example/v1/enrich
https://api.openai.example/v1/chat/completions
https://api.mail.example/v1/send
https://hooks.slack.example/services/T00/B00/xxx
```

Nine consequences follow from that one choice, and each is a property LOOM
already provides to anything that goes through a toolset.

### 3.1 Secrets reach the journal

Several workflows take an API key as *workflow input* and pass it as a step
argument — `ctx.step(ai_parse_document, text, config.openai_api_key)`. Step
inputs are recorded for humans, and nothing in the journal path redacts:

```
$ grep -rn "SecretStr\|redact" src/loom/runtime/journal.py src/loom/runtime/context.py src/loom/core/serde.py
(no matches)
```

Reproduced against a trivial workflow:

```
call_api -> {'args': ['hello', 'sk-SUPER-SECRET-123'], 'kwargs': {}}
```

`loom show <run>` prints it. `wf09_stripe_etl` goes further and hardcodes
`qb_api_key = "qb-api-key-placeholder"` inside the workflow body.

**This is a LOOM gap, not only a workflow bug** — any user who passes a
credential to a step today has it durably recorded.

### 3.2 No error classification

`resp.raise_for_status()` makes a 400 and a 503 the same exception, so
`Retry(max_attempts=3)` sleeps three times through a malformed request that was
never going to succeed. The shipped toolsets raise `NonRetryableError` subclasses
for exactly this; a raw client cannot.

### 3.3 No pagination, and the count is reported as a total

`fetch_emails(mailbox, max_emails)`, `fetch_records(base, table, batch_size)`,
`scrape_leads(url, max_leads)` each take one window and return its length as the
answer. This is the failure `Results` and `.complete` exist to prevent, and none
of these steps returns a `Results`.

### 3.4 Retries on non-idempotent writes

`send_email` carries `Retry(max_attempts=3, initial_delay=2.0)` and no
idempotency key. A timeout after the mail server accepted is indistinguishable
from a failure, so the retry sends twice. LOOM's own toolsets turn retries
**off** for `gmail_send_message`, `chat.postMessage`, and `meetings.create` for
this reason. The reference workflows do the opposite of the library's own rule.

### 3.5 No fakes, so no smoke test is possible

`agents/fakes.py` builds a fake response from an operation's `output_schema`.
A raw `httpx` call has no manifest and no schema, so the smoke stage can only
reach a DNS failure — which the repair loop then correctly refuses to act on,
because it is an environment error rather than a code error.

### 3.6 No effect classes, so the production layer is inert

`ToolsetCatalog.effect_of()` maps a `@step` to its declared `EffectClass`. A
local `@step` calling `httpx` is unclassified, so under `TaintBroker` nothing
can taint, `GrantSet` narrows nothing, and `dry_run` suppresses nothing. Running
these workflows under the production layer would enforce zero of it while
appearing configured.

### 3.7 LLM calls bypass `ctx.agent()` entirely

Every "AI" step is an HTTP POST to `api.openai.example` and a string dig into
`["choices"][0]["message"]["content"]` — which is what fails wf05. This forfeits
the provider abstraction, `Usage` and cost accounting, guardrails, bounded tool
results, session memory, and `MockModelProvider` in tests.

It also drops the **code-or-judgement** discipline. `wf03_inbox_triage`
classifies mail by prompting for a category word and string-matching the reply,
where `ctx.node("agent.classify", ...)` is the built-in for that and returns a
typed result.

### 3.8 No triggers are declared

Every spec names a trigger — "run on a schedule", "receive a Stripe payment
event", "wait for the transcript to be uploaded". `grep -rn "triggers=" examples/reference`
returns nothing. All ten are manual-run only; not one can be started by the
thing its own spec says starts it. `Schedule`, `Interval`, `Webhook`, `OnEvent`,
`Poll`, and `OnAppEvent` all exist and are unused here.

### 3.9 No human in the loop

Ten workflows that send cold email to scraped strangers, publish to social
media, write to a CRM, and file financial receipts contain zero
`ctx.wait_for_approval` and zero `human.*` nodes. `AutoRespondChannel`,
`human.approval`, and `human.review_edit` all exist.

---

## 4. Two more defects in the bodies

**Unbounded fan-out.** `ctx.gather(*[ctx.step(enrich_lead, l) for l in leads])`
with `max_leads` defaulting to 50 issues 50 concurrent calls to a rate-limited
API. `ctx.map(..., max_concurrency=10)` and `control.throttle` both exist and
are unused.

**Unbounded journals.** `wf10_pdf_chatbot` loops up to `max_questions` turns at
three durable calls each; `wf08_meeting_prep` parks for 24 hours. Neither calls
`ctx.continue_as_new`, which is the construct that keeps a forever-flow's
journal bounded.

**Fragile reliance on `OnError.CONTINUE`'s fallback.** `wf01` and `wf04` branch
on `if ok:` where `ok` is a step declared `on_error=OnError.CONTINUE` with no
`fallback=`. That returns `None`, which is falsy, so it works — by accident. A
step whose success value is `0`, `""`, or `[]` under the same pattern silently
counts a success as a failure.

---

## 5. What LOOM is actually missing

Separating the two questions: what these workflows need that exists and was not
used, versus what does not exist at all.

### Exists, unused

gmail · google_calendar · google_drive · google_meet · slack · hubspot ·
salesforce · jira · asana · clickup · confluence · teams · outlook_mail ·
outlook_calendar · onedrive · sharepoint · github · gitlab · exa · tavily ·
duckduckgo · zoom — and the nodes `agent.classify`, `agent.summarize`,
`agent.extract_structured`, `agent.judge`, `control.dedupe`, `control.batch`,
`control.throttle`, `human.approval`, `io.http_request` — plus artifacts, blobs,
`ctx.agent`, triggers, grants, and all of `loom.testing`.

### Does not exist

| Capability | Needed by | Notes |
|---|---|---|
| **Embeddings + vector index + semantic search** | wf10 | No `EmbeddingProvider` port, no `VectorStore`, no knowledge node. `grep -rn "def embed\|EmbeddingProvider" src/loom/` returns one docstring in `exa/tools.py`. wf10 cannot be made real without this. |
| **Document text extraction** | wf06, wf10 | No PDF/DOCX path anywhere in `src/loom/`. Both workflows fake it. |
| **Image generation** | wf02 | No provider port, no node. |
| **Journal secret redaction** | all | §3.1. Affects every LOOM user, not only these files. |
| **Credential-aware `io.http_request`** | all | The node takes a raw `headers` dict, so using it for an un-toolsetted API journals the token. It should be able to name a credential resolved through `ConnectionBroker`. |
| **Airtable toolset** | wf04 | |
| **Stripe toolset** | wf09 | |
| **QuickBooks toolset** | wf09 | |
| **Google Sheets toolset** | wf01 ("tracking spreadsheet") | |
| **LinkedIn toolset** | wf02, wf05 | Posting API is partner-gated; see the plan. |
| **X/Twitter toolset** | wf02, wf05 | Write access is paid-tier only; see the plan. |
| **Lead enrichment (Apollo/Clearbit)** | wf01 | |

### Executable-example gate

The absence that produced everything above: **nothing runs `examples/`**. Until
that is a CI job, any repair to these ten files decays on the same schedule.
