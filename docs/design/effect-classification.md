# Effect classification: read, write, destructive — and the axes that are missing

<!-- docs-illustrative -->

**Status:** plan. Nothing here is implemented yet.
**Scope:** `toolsets/manifest.py`, `runtime/taint.py`, `runtime/effects.py`,
`agents/tool_registry.py`, `toolsets/certify.py`, `nodes/spec.py`, `mcp_server/`.

## The ask, and what is already true

The request was to tag tools and APIs with read / write (send mail) /
destructive (delete, remove). **LOOM already has that**: `EffectClass` in
`toolsets/manifest.py`, declared per operation, enforced at dispatch by the
broker chain. Every one of the **320 operations across 23 shipped toolsets**
carries a class today — 192 read, 101 write, 27 destructive. Nothing is
unclassified.

So this is not a greenfield feature. It is an audit of a system that exists,
and the finding is that the classification is **correct where it is declared
and unreliable everywhere it is derived** — plus one axis the three-value enum
cannot express, which is the one the "send mail" example in the request is
actually pointing at.

Everything below was measured against the code, not estimated. The scripts that
produced each number are reproducible from the repository.

### What consumes an effect class today

| Consumer | What it does with it |
|---|---|
| `runtime/taint.py::TaintBroker` | a READ taints the run; WRITE/DESTRUCTIVE are then refused |
| `security/grants.py` | `GrantSet` narrows what `ctx.agent()` may resolve |
| `runtime/hooks.py` | `@rt.hooks.before_tool(effect=EffectClass.DESTRUCTIVE)` |
| `agents/tool_registry.py` | `resolve_tools(effects={READ})` hands an agent a read-only toolset |
| `toolsets/catalog.py::effect_of` | maps a `@step` function name back to its declared class |
| `toolsets/lock.py` | `effect` and `idempotent` are hashed into `steps.lock`, so drift is detectable |

That is a real enforcement path, and it is the reason this plan **keeps
`EffectClass` exactly as it is** rather than replacing it. Grant strings
(`"jira.issues:read"`), hook filters and taint policy all key on it.

---

## Findings

Nine, ordered by how quietly they fail.

### F1 — Three different defaults for "nobody said", two of them fail open

| Site | Default | Direction |
|---|---|---|
| `runtime/effects.py::EffectCall.effect` | `WRITE` | fail **safe** |
| `toolsets/manifest.py::OperationSpec.effect` | `READ` | fail **open** |
| `agents/tool_registry.py::_guess_effect` | `READ` | fail **open** |

`EffectCall` gets it right and says so in its docstring: *"an operation whose
class nobody declared is not safe to assume is harmless."* The other two
contradict it. A manifest author who omits `effect` gets `READ` — the one class
that is exempt from every write and destructive control, and the one that
*triggers* taint.

### F2 — CERT-04 and CERT-05 cannot fail

`certify.py::check_effect_classification` is documented as *"CERT-04: Every
operation has an explicit effect classification"* and implemented as
`if not op.effect`. `EffectClass.READ` is a truthy `StrEnum`, so the condition
is never true. Reproduced:

```python
op = OperationSpec(id="pages.nuke", summary="permanently delete every page",
                   function="nuke")     # no effect declared
certify(manifest_containing(op))
#   CERT-04  Effect classification present   passed=True
#   CERT-05  Scope mapping complete          passed=True
```

CERT-05 falls to the same default: it only requires `scopes` for write and
destructive operations, so an unclassified operation is exempt from that too.
One missing default disarms both certifications that exist to catch it.

The fix needs no new field. Pydantic already records whether the author
declared it — `"effect" in op.model_fields_set` is `False` above.

### F3 — The name heuristic under-classifies 14% of operations

`_guess_effect` is what classifies any toolset built with `from_steps` /
`from_callables` — i.e. every user-written toolset that does not hand-write a
manifest. Scored against the 320 hand-declared operations as ground truth:

```
correct              273  (85%)
UNDER-classified      46  (14%)   <- privilege escalation
over-classified        1  ( 0%)   <- merely annoying
```

Seven **destructive** operations are guessed READ, because the verb list knows
`delete/remove/drop/purge/revoke` and nothing else:

```
slack_archive_channel       gmail_trash_message        gmail_trash_thread
drive_trash_file            hubspot_archive_object     calendar_unshare_calendar
meet_end_active_conference
```

And 39 writes are guessed READ: `gmail_reply_to_message`,
`gmail_forward_message`, `drive_upload_file`, `drive_share_file`,
`onedrive_invite`, `calendar_move_event`, `outlook_cancel_event`,
`gitlab_close_issue`, `asana_complete_task`, …

The errors are one-directional. A wrong guess is always toward *more* permitted,
never less, because the fallback is the permissive end of the scale.

### F4 — Taint keys on READ, so pure local computation taints the run

`TaintBroker` treats any journal entry with `effect_class == READ` as evidence
the run "has read external data". Nodes attach their declared effect
(`context.py:1632`), and 20 of the 26 built-in nodes are READ — including
`control.filter`, `control.dedupe`, `transform.map_fields`,
`transform.template`, which touch nothing outside the process.

Reproduced end to end: filter a list that arrived with the run, then write.

```
run failed: post_to_slack is write and this run has read external data
            (step:control.filter). Ask for approval first …
```

The claim in that message is false — `control.filter` read nothing external —
and the run is dead. The taint rule is the headline security feature of the
production layer, and it currently fires on a list comprehension.

### F5 — `idempotent` is declared, never enforced, and has drifted

`OperationSpec.idempotent` exists ("Safe to retry without side-effects?") and
all 23 toolsets populate it: 240 of 320 true. It is surfaced in `catalog.py` and
hashed into `steps.lock` — and **nothing reads it to decide anything**. The
actual retry decision is written by hand, separately, in each `tools.py`.

The two have drifted. **26 operations declare `idempotent=False` and are
configured `max_attempts=2`:**

```
jira_create_issue        jira_add_comment          jira_transition_issue
confluence_create_page   confluence_add_comment    confluence_update_page
calendar_create_event    calendar_create_calendar  calendar_quick_add_event
calendar_share_calendar  drive_share_file          gmail_create_label
… and 14 more
```

Some are defensible — a repeated `delete` reaches the same end state, so the
*declaration* is probably what is wrong there. That is exactly the problem:
with two sources of truth and no check between them, there is no way to tell
which half is wrong. `tests/test_manifest_imports.py` already solves this shape
for pagination ("the client is ground truth; that check found six drifts on its
first run"). Nothing equivalent exists for effects.

### F6 — No axis for irreversible-and-external, which is the "send mail" case

The request names *send mail* as the write example, and the three-value enum
cannot express what makes it different. Under `EffectClass`:

- `gmail_create_draft` — WRITE
- `gmail_trash_message` — DESTRUCTIVE
- `gmail_send_message` — WRITE

Ranked by damage that is the wrong order twice over. Trashing is recoverable for
30 days and `gmail_untrash_message` undoes it; LOOM's own docs say so. Sending
reaches people outside the deployment and **nothing undoes it**. A policy that
blocks DESTRUCTIVE and permits WRITE stops the reversible operation and allows
the irreversible one.

This is not a LOOM-specific gap — MCP's four hints do not capture it either
(see prior art). It needs a reversibility axis.

### F7 — No axis for access-control changes

`drive_share_file`, `calendar_share_calendar`, `onedrive_invite` are WRITE;
`drive_remove_permission`, `calendar_unshare_calendar` are DESTRUCTIVE. They are
the same *kind* of act — changing who can reach data — split across two classes
by whether they add or subtract.

For an agent this is the highest-consequence category available: sharing a
folder exfiltrates without writing anything to it, and reads as an ordinary
additive write. AWS separates `Permissions management` from `Write` as its own
access level for exactly this reason.

### F8 — Effect is static per operation, but sometimes lives in the arguments

`io.http_request` is one node with one class (WRITE). `method="GET"` is a read;
`method="DELETE"` is destructive. Same for generic CRUD —
`salesforce_update_record` against a permission object is not the same act as
against a contact.

There is no way to say this today, and `io.http_request` is precisely the node
a generated workflow reaches for when no toolset covers the API.

### F9 — `effect_of` is a flat, process-global map keyed on function name

`catalog.py::effect_of` builds `{function_name: effect}` across every registered
manifest. There are **no collisions today** (checked), but two toolsets shipping
a `search_messages` would silently resolve to whichever registered last — and
`ToolsetRegistry` chains to a process-global parent, so a third-party entry
point can land in that map without the host doing anything.

---

## Prior art

Three sources, chosen because each contributes an axis LOOM lacks.

### MCP `ToolAnnotations` — four orthogonal booleans, all defaulting fail-safe

LOOM serves `loom mcp` to Claude Code, Claude Desktop and Cursor, so this is
not merely comparable — it is a surface LOOM already implements, minus the
annotations.

```typescript
readOnlyHint?:    boolean;  // Default: false — tool does not modify its environment
destructiveHint?: boolean;  // Default: true  — may perform destructive updates
                            // (meaningful only when readOnlyHint == false)
idempotentHint?:  boolean;  // Default: false — repeat calls add no further effect
                            // (meaningful only when readOnlyHint == false)
openWorldHint?:   boolean;  // Default: true  — interacts with an "open world" of
                            // external entities; a web search is open, memory is not
```

Three things to take from it:

1. **Orthogonal, not ordinal.** Read-only, destructive, idempotent and
   open-world are four independent questions. LOOM collapses the first two into
   one ordinal scale and drops the fourth.
2. **`openWorldHint` is exactly the axis F4 needs** — and the spec's own
   example is the distinction LOOM gets wrong: *"the world of a web search tool
   is open, whereas that of a memory tool is not."*
3. **Every default is the cautious one.** Assume not read-only, assume
   destructive, assume not idempotent, assume open world. F1's two fail-open
   defaults are the opposite of the standard LOOM already speaks.

The spec is also explicit that these are **hints**: *"Clients should never make
tool use decisions based on ToolAnnotations received from untrusted servers."*
That settles the trust question below.

`loom mcp` already sets `ToolAnnotations` on **all 24 of its tools**, by hand,
and they are good — `cancel_run` is `destructiveHint=True`, and `retry_run` is
`openWorldHint=True` where `replay_run` is `False`, which is a genuinely subtle
distinction to have got right.

What is missing is not the annotations but the **check**: they are asserted in
`server.py` and asserted again in each manifest, with nothing comparing the two.
That is F5's drift shape one layer up, and at 1,000 toolsets it is the layer
where hand-maintenance stops being viable — the 320 toolset operations have no
projection to MCP at all today.

### AWS IAM access levels — five, not three

`List`, `Read`, `Write`, **`Permissions management`**, `Tagging`. The fourth is
F7's missing axis, promoted to a top-level category by a system that has
classified every action of every service for fifteen years. The fifth is a
reminder that low-consequence metadata writes are worth separating from the
rest — LOOM's `gmail_modify_labels` and `drive_rename_file` sit in the same
WRITE bucket as `hubspot_create_deal`.

### Four-tier agent risk frameworks

The current published guidance converges on: **read-only → reversible →
external (reaches third parties) → irreversible / high-consequence**, with
mandatory human approval at the top tier. That is F6's axis, and it puts *send
mail* in tier three-or-four while leaving *trash a message* in tier two —
the inversion LOOM has today.

---

## Design

**One sentence: keep `EffectClass` as the damage dial, add three orthogonal
facets beside it, and make everything that currently guesses either declare or
fail safe.**

`EffectClass` is load-bearing in the public grant syntax, hook filters and taint
policy. Replacing it with a bag of booleans would be a breaking change to all
three for no gain — the ordinal scale answers "how much damage", which remains a
real question. What it cannot answer is "damage to whom, and can it be undone",
which is what the facets add.

### New fields on `OperationSpec`

```python
class OperationSpec(BaseModel):
    effect: EffectClass = EffectClass.WRITE      # F1: default flips to fail-safe
    idempotent: bool = False                     # exists; starts being enforced

    open_world: bool = True
    """Does this reach outside the deployment's trust boundary?

    False for pure computation and for stores the deployment owns. What
    TaintBroker keys on instead of READ, so filtering a list stops being
    reported as reading external data. Default True: a call nobody classified
    may well have."""

    reversible: bool = False
    """Can the effect be undone, by an operation in this toolset?

    Separates gmail_trash_message (untrash restores it) from
    gmail_send_message (nothing unsends). Default False: assume not."""

    undone_by: str = ""
    """The operation id that reverses this one, when one exists.

    Richer than the boolean and checkable — CERT asserts the id resolves.
    Later: ctx.compensate() can register it automatically, which is the
    saga machinery LOOM already has, wired to a declaration it already needs."""

    access_control: bool = False
    """Does this change who can reach data, rather than the data itself?

    share / unshare / invite / remove_permission. AWS's Permissions
    management, and the category most worth a human for an agent.

    **Declared, not derived.** A scope-based derivation was tried and matches
    zero of the 320 shipped operations: Google covers permissions with the
    broad scope, so `drive_share_file` and `drive_remove_permission` declare
    exactly what an ordinary write declares, and the Microsoft toolsets declare
    no scopes at all."""

    effect_by: dict[str, dict[str, EffectClass]] = {}
    """Argument-dependent effect: {"method": {"GET": READ, "DELETE": DESTRUCTIVE}}.

    Declarative rather than a callable, deliberately — grant validation and
    the catalog read manifest metadata without importing a toolset, and a
    callable would break Layer 1. Resolved at dispatch; the static `effect`
    is the ceiling and the fallback."""
```

All five default to the cautious answer, matching MCP and matching `EffectCall`.

### Where each finding gets fixed

| # | Fix | File |
|---|---|---|
| F1 | `OperationSpec.effect` defaults to `WRITE`; `_guess_effect` falls back to `WRITE` | `manifest.py`, `tool_registry.py` |
| F2 | CERT-04 checks `"effect" in op.model_fields_set`; CERT-05 stops exempting unclassified ops | `certify.py` |
| F3 | `_guess_effect` returns `EffectClass \| None`; caller defaults to WRITE and logs. Verb lists grown from the 46 misses, which become the test corpus | `tool_registry.py` |
| F4 | Taint triggers on `open_world and effect is READ`. The 26 built-in nodes get `open_world=False` except `io.*` and `agent.*` | `taint.py`, `nodes/spec.py` |
| F5 | Conformance test: declared `idempotent` ⟺ the step's retry config. Then derive the retry default from the declaration | `tests/`, `toolsets/*/tools.py` |
| F6 | `reversible` / `undone_by`; approval policy keys on irreversible-and-open-world | `manifest.py`, `taint.py` |
| F7 | `access_control`; `GrantSet` and hooks can select it independently of read/write | `manifest.py`, `grants.py` |
| F8 | `effect_by`, resolved in `ctx.step` / `ctx.node` before the broker sees the call | `context.py` |
| F9 | `effect_of` keyed on `(toolset_id, function)`; collision raises `ConfigurationError` at registration, as duplicate qualified ids already do | `catalog.py` |

### Enforcement stays server-side; annotations are advisory

MCP is explicit that annotations are untrusted hints, and that is the right
split for LOOM too. The facets have two distinct jobs and must not be confused:

- **Outward** (`loom mcp`, `loom toolset <id>`, the coding agent's prompt):
  advisory. They drive a client's consent UI and a model's tool choice.
- **Inward** (`GuardedBroker`, `TaintBroker`, `GrantSet`, hooks): enforcement.
  This happens in LOOM's own process against LOOM's own manifests, after the
  model has chosen. A model that ignores `destructiveHint` still meets the
  broker.

The distinction is what makes it safe to publish the facets at all.

### MCP mapping

Derived, never stored twice:

```python
readOnlyHint    = op.effect is EffectClass.READ
destructiveHint = op.effect is EffectClass.DESTRUCTIVE or not op.reversible
idempotentHint  = op.idempotent
openWorldHint   = op.open_world
```

Note `destructiveHint` folds in reversibility, because MCP's single boolean is
coarser than LOOM's two fields and the honest projection of "irreversible" onto
it is `true`. That puts `gmail_send_message` on the cautious side of a client's
consent prompt, which is the outcome F6 wants.

---

## Does this scale to thousands of toolsets?

**Not as written above.** The design in the previous section adds five declared
fields per operation, and that is the wrong shape at this repository's own
stated target. The numbers:

```
today            23 toolsets ·   320 operations  (mean 13.9/toolset)
at 1,000         ~13,900 operations
at 5,000         ~69,600 operations

5 new facets x 13,900 ops  =  ~70,000 hand decisions at 1,000 toolsets
```

Seventy thousand hand-made judgements, each one an opportunity for the F3
error — and F3 already shows what happens to a hand-maintained classification
at 320: 14% of it is wrong the moment it is derived rather than declared.

### The repository already solved this, three times

| Precedent | The rule it states |
|---|---|
| `toolsets/pagination.py` | *"The return type is the declaration… a toolset author writes it once and nothing is maintained in parallel — **the only version of this that survives a thousand toolsets**."* |
| `nodes/base.py::derive_spec` | *"Fill in everything a node should not have to write twice… A declared value is left alone, so a node can override — but nothing has to."* |
| `tests/test_manifest_imports.py` | *"The client is ground truth"* — the check that found six drifts on its first run. |

All three say: **derive from what the author already wrote; let declaration
override; check the two against each other in CI.** The plan above ignores all
three and asks for declarations instead. That is the defect.

### Effect is derivable, and it was measured

The client already encodes the answer in its HTTP verb. Matching every tool
function to its single-verb client method:

```
matched 91 tool functions to an unambiguous client verb
verb implies the declared effect:  89 / 91  =  97%

disagreements:
   POST   hubspot_search_objects     declared=read
   POST   outlook_get_schedule       declared=read
```

Both misses are the same known case — **POST-as-search** — which is exactly
where "the name lies" and an explicit override earns its keep. GET→READ,
POST/PUT/PATCH→WRITE, DELETE→DESTRUCTIVE covers the rest.

A second, independent signal confirms it. Providers encode read-only in the
OAuth scope, and where a scope says so it has **never** disagreed:

```
119 / 320 operations declare scopes
 31 carry a read-only scope  ->  31 declared READ  (100%, zero mismatches)
```

That one is not even a derivation — it is a **free invariant**. `a read-only
scope implies effect is READ` can run in CI across any number of third-party
manifests at no authoring cost whatsoever.

So the revised rule is: **derive, declare only the exceptions.** At 1,000
toolsets that is roughly 1,400 overrides instead of 70,000 declarations, and
the 98.6% that is derived cannot drift, because it is not stored twice.

### Revised design

Replace the five loose fields with one derived value object:

```python
@dataclass(frozen=True)
class EffectProfile:
    """Everything policy needs to know about one operation's side effect.

    Derived by `derive_effect_profile`; any field the manifest declared
    explicitly is left alone, exactly as `derive_spec` treats a node."""
    effect: EffectClass
    open_world: bool
    reversible: bool
    idempotent: bool
    access_control: bool
    undone_by: str = ""
    source: Literal["declared", "derived", "default"] = "default"


def derive_effect_profile(op: OperationSpec, *, verb: str = "") -> EffectProfile:
    """One place, in this order:
         1. what the author declared          (model_fields_set)
         2. what the client's HTTP verb says  (97% accurate, measured)
         3. what the scopes say               (read-only scope => READ)
         4. the fail-safe default             (WRITE, closed, irreversible)
    """
```

Two consequences worth the change on their own:

- **Consumers depend on one type, not five fields.** `TaintBroker`,
  `GrantSet`, hooks, the catalog and the MCP projection take an
  `EffectProfile`. Adding a sixth facet later touches `derive_effect_profile`
  and nothing else — where the five-field version touches every consumer.
- **`source` makes trust legible.** A profile that says `derived` was computed
  from the client; `declared` was asserted by whoever wrote the manifest. At
  1,000 community toolsets that distinction is the difference between a fact
  and a claim.

### Four scaling defects the original plan did not address

**1 — Third-party manifests are trusted exactly like first-party.**
`loom_toolset` entry points let any installed package register a manifest, and
`ToolsetRegistry` chains to a process-global parent. A community toolset
declaring `effect=read` on a delete is then a supply-chain assertion that
nothing checks. MCP's rule — *never make tool-use decisions on annotations from
untrusted servers* — applies inside the process too.

*Fix:* `loom certify` already exists and already runs per manifest. Give
`CertificationResult` a **tier** — `derived` (checked against its own client),
`declared` (self-asserted, unverified) — and let a deployment require one:
`Runtime(require_effect_tier="derived")`. This is enforcement policy, not a new
subsystem.

**2 — The conformance test only covers what is in this repository.**
`test_effect_conformance.py` as specified imports first-party `tools.py` modules.
At 1,000 toolsets, most are not in this repo and never run that test.

*Fix:* ship the check as a **conformance kit**, the shape
`loom.testing.conformance.verify_event_log` already uses — a function a toolset
author runs against their own manifest in their own CI. The repo already
distributes conformance this way for event logs, checkpoints and event sources.

**3 — The verb list in `_guess_effect` is unmaintainable by construction.**
Growing it from the 46 current misses fixes 46 cases and nothing about the 47th.

*Fix:* derivation demotes it to a last-resort fallback for callable-only
toolsets with no client and no scopes. Keep it, make it fail to WRITE, and
score it in CI against the ground-truth corpus — but stop treating it as the
primary path, because it is only the primary path for toolsets that declared
nothing at all.

**4 — Facet creep has no boundary.**
Someone will want cost, PII-sensitivity, rate-limit tier, data residency.
Each new field on `OperationSpec` is a migration across every manifest in
existence.

*Fix:* a **closed set** for anything enforcement keys on — the six fields on
`EffectProfile` — plus an open `labels: dict[str, str]` for everything else.
Policy may only key on the closed set; labels are for search, docs and
reporting. A facet that graduates to enforcement is a deliberate, versioned act.

### Two smaller things, measured rather than assumed

- **`effect_of` rebuilds its whole map on every registration**
  (`catalog.py:84` sets `self._by_function = None` in `register`). Registering
  1,000 toolsets with interleaved lookups costs **0.43s** — real, bounded, and
  fixed by updating the map incrementally instead of invalidating it. Not a
  redesign; a one-line change filed under F9.
- **`model_fields_set` survives every construction path** — direct
  construction, `model_validate` from a dict, and `model_validate_json`. The F2
  fix therefore works for manifests shipped as JSON by third parties, which is
  the case that matters at scale. Verified.

### One thing that would have mattered, and does not

An earlier draft of this plan required a `schema_version` on `ToolsetManifest`
before anything else could land: `ToolsetManifest.version` is the *toolset's*
version, so flipping `OperationSpec.effect` from READ to WRITE would silently
change the meaning of every manifest already published against the old default.

**The premise was checked and is false.** LOOM is `0.1.0` with no git tags and
an entirely `[Unreleased]` changelog; the `loomflow` name on PyPI belongs to an
unrelated project (`Anurich/LoomFlow`), so nothing this repository produced has
ever been installed from an index. And every one of the 320 shipped operations
declares `effect` explicitly — zero rely on the default — so the flip is a
no-op for every toolset that exists.

The version gate is therefore permanent API surface bought with a hypothetical,
and was removed. It becomes worth adding the first time a real external toolset
exists to migrate; the reasoning above is kept so that decision does not have to
be rediscovered.

**The general rule this leaves:** pre-1.0, with no external consumers, prefer
the direct change and a corpus test over machinery that makes a future change
safe. `tests/test_toolsets.py::TestShippedToolsetsAreClassified` is what would
have to be re-derived; the gate is not.

## Phasing

Each phase ships alone and is useful alone. **A** and **B** are bug fixes with
no new public surface; the request's actual feature is **C**.

### Phase A — make the existing system tell the truth
Add `schema_version` to `ToolsetManifest` and gate on it, so the default flip is
a migration rather than a silent change. Build `derive_effect_profile` and the
`EffectProfile` value object — *before* any facet is added, so no facet is ever
hand-declared 14,000 times. Flip the two fail-open defaults behind the schema
gate (F1). Make CERT-04/05 capable of failing (F2).
Fix `_guess_effect`'s fallback and grow its verb lists (F3). Add the
idempotency conformance test (F5) and reconcile the 26 drifts by hand — each is
a decision about that operation, not a mechanical rewrite.

*Exit:* the 46 under-classifications go to zero against the ground-truth corpus;
an operation with no declared effect fails certification; the conformance test
passes with no `xfail`.

*Risk:* flipping `OperationSpec.effect` to WRITE could reclassify a third-party
manifest that relied on the default. That is the point, and it fails safe — but
it is a minor version bump and a changelog entry.

### Phase B — add `open_world`, fix taint
Add the facet to `EffectProfile`. **Derive it** — a toolset with a client and a
base URL is open-world by construction; a node with no egress is not — and hand-
declare only the exceptions. Switch `TaintBroker` to key on it. Pure `control.*` / `transform.*` / `guard.*` become closed-world;
`io.*`, `agent.*`, and every remote toolset read stay open.

*Exit:* the `control.filter` repro in F4 completes; a workflow that searches the
web and then writes is still refused.

### Phase C — the requested axis
Add `reversible` / `undone_by` and `access_control` to `EffectProfile`.
`access_control` derives from the scope strings that already say so
(`*.permissions`, `*.sharing`, `Directory.*`); `undone_by` is declared, because
naming the inverse operation is genuinely a human judgement — and it is the one
facet CERT can verify by resolving the id. Extend `TaintPolicy` with
`block_irreversible` and `block_access_control`, separate dials for the reason
`block_writes` and `block_destructive` are already separate — different false
positive rates. Populate across the 23 toolsets.

*Exit:* a policy that permits ordinary writes still parks `gmail_send_message`
and `drive_share_file` on a human; `gmail_trash_message` stops being treated as
worse than sending.

### Phase D — surfaces
Replace the 24 hand-written `ToolAnnotations` sets with a projection from
`EffectProfile`, and extend it to toolset-derived tools, which have none.
`loom toolset <id>` gains the facet columns. The coding agent's node/toolset
cards carry them, so generated code can be told *"an irreversible operation gets
`ctx.wait_for_approval` before it"* as a rule with a machine-checkable premise.

*Exit:* the 24 hand-written annotation sets are replaced by a derivation from
`EffectProfile` and a test asserts the two agree; toolset-derived tools gain a
projection they do not have today; a static check in `agents/stages.py` flags an irreversible call with no approval ahead
of it.

### Phase E — argument-dependent effects
`effect_by`, starting with `io.http_request` and the generic CRUD operations in
Salesforce and HubSpot.

*Exit:* `io.http_request(method="DELETE")` reaches the broker as DESTRUCTIVE.

---

## Test plan

Mirrors what the repo already does for pagination and manifest imports —
**a ground-truth corpus and a conformance check**, not example-based tests.

- `tests/test_effect_conformance.py`
  - every operation declares `effect` explicitly (`model_fields_set`)
  - declared `idempotent` ⟺ the step's actual `Retry` config *(F5)*
  - every `undone_by` resolves to a real operation in the same toolset
  - `access_control` implies non-empty `scopes`
- `tests/test_effect_guess.py` — `_guess_effect` scored against all 320
  declared operations; **assert zero under-classifications**, over-classification
  allowed. The 46 current misses are the initial corpus.
- `tests/test_taint.py` — the F4 repro as a regression test: closed-world node
  then write completes; open-world read then write refused.
- `tests/test_mcp_server.py` — annotations present and consistent with the
  manifest for every tool.
- `loom.testing.conformance.verify_effect_profile(manifest, client)` — the
  **conformance kit** a third-party toolset author runs in their own CI, the
  shape `verify_event_log` / `verify_event_source` already use. This is what
  extends the invariant beyond this repository.
- `tests/conformance/` — unchanged; effects are metadata, not store behaviour.

## Non-goals

- **Not building an IAM.** No principals, no resource-level policy. The facets
  describe an *operation*, not an actor's permission over an instance.
- **Not making `EffectClass` a lattice.** Three ordered values plus orthogonal
  booleans is the whole model. A partial order over five levels would break the
  grant syntax to express something no consumer asked for.
- **Not classifying with a model.** The heuristic is a fallback for
  hand-written toolsets and should stay dumb, loud and fail-safe. An LLM
  classifier would be an unauditable third source of truth for a decision that
  gates deletions.
- **Not touching `EffectClass`'s three names.** `read`/`write`/`destructive`
  appear in grant strings in user code.
