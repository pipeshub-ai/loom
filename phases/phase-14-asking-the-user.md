# Phase 14 — Asking the user

> **Shipped.** Everything in §3 is implemented; §5 remains out of scope.
> `loom/agents/interaction.py`, `tests/test_interaction.py` (36),
> `tests/test_ask_user_wiring.py` (26).

`ask_user` exists, is unit-tested, and until this week had never been offered to
a model: `WorkflowCodingAgent` took `user_interaction=` and no caller supplied
one. That is now wired (CLI and five cookbook examples) and the ladder's rung 3
splits on it. This phase is about the *tool itself*, which was designed before
there was anything to compare it against.

There is something to compare it against now. Three sources, each answering a
different question:

| Source | Answers |
|---|---|
| Claude Code's `AskUserQuestion` | what a good asking **UX** is |
| MCP `elicitation` (spec + SDK) | how a **server** asks, over a protocol |
| LangGraph `interrupt`/`Command` | what asking looks like when it must **survive a restart** |

## 1. What the research says

### 1.1 Claude Code — `AskUserQuestion`

The strongest primary source, because its schema is legible rather than
described. Six decisions worth taking:

- **Batching.** One call carries **1–4 questions**. Loom asks one per tool
  call, so four ambiguities cost four model turns and interrupt the person four
  times. Batching is the single biggest difference.
- **An "Other" escape is automatic.** *"Users will always be able to select
  'Other' to provide custom text input."* A closed option list is a guess about
  the answer space, and the agent's list comes from a lookup that may have
  missed.
- **A recommendation goes first.** *"If you recommend a specific option, make
  that the first option in the list and add '(Recommended)'."* An answerable-by-
  Enter question is answered; an open one is deferred.
- **A `header` of ≤12 characters**, rendered as a chip. Questions are scanned
  before they are read.
- **`multiSelect`** per question.
- **A stated bar for asking at all**: *"Reserve this for decisions where the
  user's answer changes what you do next — not for choices with a conventional
  default or facts you can verify in the codebase yourself."* Loom's prompt says
  something weaker.

### 1.2 MCP — elicitation

Servers request input **through the client**, so no server ever reads stdin.
`Context.elicit(message, schema)` is present in the pinned `mcp` 2.x SDK, and
`mcp.types` carries the whole family, so this needs no dependency.

Four constraints shape the model:

- **A restricted, flat schema.** Strings, numbers, booleans, enums; `oneOf` of
  `{const, title}` for labelled choices; an array of enums for multi-select;
  `default` on every primitive. *"Complex nested structures … are intentionally
  not supported to simplify client user experience."*
- **Three response actions, not two.** `accept` (data), `decline` (*"user
  explicitly declined"*), `cancel` (*"dismissed without making an explicit
  choice"*). The spec is explicit that these want different handling: *"Decline:
  offer alternatives. Cancel: prompt again later."*
- **Only during an active client request**, so *"a user is never prompted out of
  nowhere"*. `author_workflow` is a tool call, so authoring fits exactly.
- **Never secrets in form mode.** *"Servers MUST NOT use form mode elicitation
  to request … passwords, API keys, access tokens."* URL mode exists for that.

### 1.3 Cursor 2.1, and why the bar matters

Cursor added clarifying questions in Plan mode: detect ambiguity, ask before
spending compute. Their reported numbers — 34% fewer errors, 42% fewer
iteration cycles — are vendor-published and unaudited, but the direction is the
same one this repo already reached from the other end: our own run picked
`PA-1844 "SaaS V2"` over `PA-1769 "Launch SAAS"` and truthfully reported zero
overdue tickets, when the other epic had 33.

### 1.4 LangGraph — the durable shape

`interrupt()` pauses, `Command(resume=…)` continues, and **it does not work
without a checkpointer**. That is the seam Loom already has and does not use
here: authoring is an in-memory loop, so a question is a blocking wait rather
than a park. Noted in §5 as deliberately out of scope.

## 2. Gaps, ranked

| # | Gap | Where | Cost |
|---|---|---|---|
| 1 | One question per call | `ask_user(question=…)` | a turn and an interruption per ambiguity |
| 2 | No free-text escape | `options` only | the person cannot say "neither" |
| 3 | Two outcomes, not three | `UserResponse(answer, skipped)` | "proceed without me" and "nobody was there" are the same value and want opposite fallbacks |
| 4 | No multi-select | `input_type` ∈ text/select/confirm | "which of these fields?" is unaskable |
| 5 | No default, no recommendation | — | every question costs a decision |
| 6 | **A server cannot ask** | no interaction on the MCP facade | `author_workflow` is the main non-CLI surface and it is mute, though the SDK supports elicitation |
| 7 | **Answers are not recorded** | `CodingResult` | authoring is not reproducible |
| 8 | `allow_skip` never set | `make_ask_user_tool` | dead field |
| 9 | No dedup | `AskUserGate` counts only | budget burns on re-asks |
| 10 | 300s blocking timeout | `CLIUserInteraction` | a PTY with no human waits five minutes |

**Gap 7 is the one that is Loom's rather than parity.** Everything above it,
Claude Code and Cursor already do. Nobody records the Q&A as part of the
artifact — and this repo's whole thesis is that work is journaled and replayable.
A workflow authored with human answers currently cannot be regenerated: re-run
`loom author` and it asks again, and CI cannot run it at all. The answers are
*inputs to a build* and belong beside the source.

## 3. Design

### 3.1 One question model, two wire formats

Shaped as the intersection of Claude Code's UX and MCP's restricted schema, so
one model renders to a terminal prompt and to `requestedSchema` without a second
source of truth.

```python
class Choice(BaseModel):
    value: str                     # what the agent receives
    label: str                     # what the person sees
    description: str = ""
    recommended: bool = False      # rendered first, marked

class Question(BaseModel):
    id: str                        # content-derived; the recording key
    header: str                    # <= 12 chars, a chip
    question: str
    kind: Literal["text", "select", "multi_select", "confirm"]
    choices: list[Choice] = []
    default: str | None = None
    allow_other: bool = True       # ON by default — the "Other" rule
    context: str = ""

class Answer(BaseModel):
    action: Literal["accept", "decline", "cancel"]
    values: list[str] = []         # chosen `Choice.value`s
    other: str = ""                # free text, when the person went off-list
```

`action` is MCP's, exactly, because a translation layer between two 3-state
enums is a place for them to drift. `CLIUserInteraction` maps Ctrl-C → `cancel`,
an empty answer on a skippable question → `decline`, timeout → `cancel`.

**`decline` and `cancel` are handled differently and that is the point.**
Decline means *proceed without me*: take the recommended option, note the choice
in a comment, carry on. Cancel means *nobody answered*: fall back to the
`ctx.agent()` form so the decision moves to run time, where a person may be
present. Today both are `skipped` and both do the same thing.

### 3.2 Batch

```python
ask_user(questions: list[Question]) -> list[Answer]     # 1..4
```

Capped at 4, following Claude Code. The gate's budget becomes a *question*
budget rather than a call budget, so batching cannot buy extra questions.

### 3.3 Transports

`UserInteraction` stays one method. Four implementations:

| Implementation | Surface | Notes |
|---|---|---|
| `CLIUserInteraction` | `loom author`, examples | exists; gains batch, three actions, "Other" |
| `ElicitationUserInteraction` | `loom mcp` | **new** — `ctx.elicit`; `Question` → restricted schema |
| `RecordedUserInteraction` | CI, re-runs | **new** — answers from a file; delegates on a miss |
| `CallbackUserInteraction` | tests | exists |

`ElicitationUserInteraction` is what closes gap 6, and it is why the model is
shaped to MCP's subset: a `select` becomes `oneOf` of `{const, title}`, a
`multi_select` becomes an array of enums, `confirm` becomes a boolean, `default`
maps straight across. `allow_other` becomes a second optional string property,
since the subset has no "enum plus free text".

**Capability, not a timeout.** The tool is offered when something can answer:
a TTY for the CLI, the client's declared `elicitation` capability for MCP, a
loaded answer file for a recording. Absent → the tool is omitted entirely, the
rule `observe_target` follows. This replaces the 300-second block (gap 10) with
a question that is never asked.

### 3.4 Recording — the part that is Loom's

```python
class AskedQuestion(BaseModel):
    question: Question
    answer: Answer

# CodingResult.questions: list[AskedQuestion]
```

```bash
loom author "..." --answers answers.json     # replay; unmatched -> ask or fail
loom author "..." --no-ask                   # never ask; recommended/agent fallback
```

Three things this buys, in order of how much they matter:

- **A generation becomes reproducible.** The same spec plus the same answers
  produces the same file, which is what makes `loom author` usable in CI at all.
- **`loom edit` stops re-litigating.** An edit currently has no idea a question
  was ever settled; it can be handed the record.
- **The decision is auditable.** "Why does this workflow filter on `PA-1769`?"
  is answered by the record, not by asking whoever ran it.

Keyed by `Question.id`, a content hash of the question text and choices — the
same technique `make_ask_user_tool` already uses for `ctx.call`, and for the same
reason: a replayed model may ask in a different order, so position is not an
identity. A question whose choices changed is a *different* question and is
re-asked rather than answered from a stale record.

### 3.5 Prompt

`ASK_USER_INSTRUCTIONS` already carries the rung-3 split. Add:

- **The bar**, in Claude Code's words: a decision the answer changes, not one
  with a conventional default or a fact a lookup would settle.
- **Batch**: gather ambiguities and ask once, up to four.
- **Recommend**: put your preferred option first and say why in its
  `description`. A question with no recommendation is a question you have not
  thought about.
- **`decline` vs `cancel`**, and what to do with each.

## 4. Test plan

| Area | Assertion |
|---|---|
| Wiring | `tests/test_ask_user_wiring.py` extends to the MCP surface — a client declaring `elicitation` gets the tool, one that does not gets no tool |
| Schema | every `Question` kind renders to a schema the MCP subset allows — asserted against `ElicitRequestedSchema`, not against a hand-copy |
| Actions | `accept`/`decline`/`cancel` each produce a *different* generated shape; a test that only checks "did not crash" would pass on all three today |
| Batch | 4 questions cost one call; 5 is refused, not truncated |
| Other | a select always admits an off-list answer unless `allow_other=False` |
| Recording | spec + answers replays byte-identical; a changed choice list forces a re-ask |
| Non-interactive | no TTY, no capability, no record ⟹ no tool in the list — asserted on the tool names, not on behaviour |
| Prompt | the instructions appear only when the tool does (exists) |

## 5. Out of scope, deliberately

- **URL-mode elicitation.** Its purpose is secrets, and the agent must never ask
  for one — the spec's own MUST NOT. Recorded so nobody adds it as a
  convenience.
- **Durable authoring.** The LangGraph shape — park the session, resume on an
  answer — is the right end state and is a phase of its own: it needs the
  authoring loop to become a workflow run, which `CodingSession` anticipates and
  does not yet do. Until then a question is a blocking wait, and the capability
  check is what keeps that safe.
- **`preview`.** Claude Code renders side-by-side artifacts for comparison. The
  authoring analogue — two candidate code shapes — is real but needs a rendering
  surface the CLI does not have.

## 6. Review

**Does this make the agent chattier?** It should make it ask *less often and
better*: batching turns four interruptions into one, the bar is stated, and a
recommendation makes most questions one keystroke. The risk is real and the
mitigation is the budget, which is already there.

**Is recording over-engineering?** It is the smallest of the changes and the
only one that is not catch-up. Without it `loom author` cannot run in CI once
the agent starts asking — which this phase makes more likely, so shipping the
asking without the recording makes CI worse.

**Why not just adopt MCP's model wholesale?** Because the CLI is not an MCP
client and never will be, and `header`/`recommended`/`allow_other` have no place
in `requestedSchema`. Shaping Loom's model to the *intersection* keeps one
source of truth; shaping it to MCP alone would lose the UX affordances that make
a question answerable.
