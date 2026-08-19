# Phase 12 — Effect Classification

**Goal:** Make every operation's side effect *derived and verified* rather than
declared and trusted, and add the two axes the three-value `EffectClass` cannot
express — reversibility and access-control change — so a policy can park
`gmail_send_message` on a human while letting ordinary writes through.

**Prerequisites:** Phase 3 (toolsets, manifests, `loom certify`), Phase 5
(`EffectBroker`, `TaintBroker`, grants). Touches Phase 9 (MCP annotations).

**Design reference:** `docs/design/effect-classification.md` — the audit, the
nine findings, the prior art, and why the first draft was rejected as
unscalable.

**Scope note.** This is a *correctness and scale* phase, not a feature phase.
Steps 1–4 fix defects in a system that already exists and ships no new public
surface. The requested capability lands in step 6.

---

## 1. Exit Criteria & Success Metrics

| Metric | Gate | Target |
|---|---|---|
| Operations with an explicitly declared or derived effect | 320/320 | 320/320 |
| `_guess_effect` under-classifications vs the corpus | 0 | 0 |
| Declared `idempotent` vs actual retry config | 0 disagreements | 0 |
| An unclassified destructive op fails `loom certify` | Yes | Yes |
| Closed-world node followed by a write | completes | completes |
| Open-world read followed by a write | still refused | still refused |
| MCP annotations derived, not hand-written | 24/24 | 24/24 |
| Hand-declared facets per operation | ≤ 2 mean | ≤ 1 mean |
| Third-party toolset can self-verify | conformance kit ships | + documented |

**"Done" means:** a toolset author writes a client and a manifest, runs
`loom toolsets derive-effects --check` in their own CI, and is told which
operations disagree with their own client — while a deployment can set
`Runtime(require_effect_tier="derived")` and be certain no self-asserted
`effect=read` on a delete is reachable.

---

## 2. HLD

```
                        AUTHORING / CI                          RUNTIME
                 (imports clients — expensive)          (manifests only — Layer 1)
   +---------------------------------------------+   +---------------------------+
   |  loom toolsets derive-effects [--check]      |   |  ToolsetCatalog           |
   |                                              |   |    .profile_of(ts, fn)    |
   |   client.py --AST--> HTTP verb  --+          |   |         |                 |
   |   manifest.scopes ---------------+           |   |         v                 |
   |   manifest declarations ---------+           |   |   EffectProfile (frozen)  |
   |                                  |           |   |         |                 |
   |                                  v           |   |    +----+----+----+       |
   |                    derive_effect_profile()   |   |    |    |    |    |       |
   |                                  |           |   |  taint grants hooks MCP   |
   |                    writes/verifies manifest  |   |                           |
   +---------------------------------------------+   +---------------------------+
                          |                                        ^
                          +--------- manifest is still the ---------+
                                     single runtime source
```

**The one structural decision.** Derivation runs at **authoring/CI time**, not
at dispatch. It reads `client.py` by AST and writes the answer *into the
manifest*, exactly as `loom check` writes `order.graph.json`. Runtime keeps
reading manifest metadata only, so Layer 1 stays Layer 1 and no toolset is
imported to answer "what does this operation do".

Deriving at runtime would import every client to classify a call — the precise
cost the three-layer lazy system exists to avoid.

---

## 3. LLD

### 3.1 `toolsets/effects.py` (NEW)

```python
"""What one operation does to the world, derived once and checked in CI."""

from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Literal

from loom.toolsets.manifest import EffectClass, OperationSpec

Source = Literal["declared", "derived", "default"]

_VERB_EFFECT = {
    "GET": EffectClass.READ, "HEAD": EffectClass.READ,
    "POST": EffectClass.WRITE, "PUT": EffectClass.WRITE, "PATCH": EffectClass.WRITE,
    "DELETE": EffectClass.DESTRUCTIVE,
}

#: Scope fragments that mean read-only. Measured: where one of these appears the
#: operation has *never* been anything but READ (31/31, zero mismatches).
_READONLY_SCOPE = ("readonly", ".read", "/read", "Read.All")

#: Scope fragments that mean the operation changes who can reach data.
_ACL_SCOPE = ("permission", "sharing", "Directory.", "acl")


@dataclass(frozen=True, slots=True)
class EffectProfile:
    """Everything policy needs to know about one operation's side effect.

    One value object rather than six loose fields on ``OperationSpec``:
    ``TaintBroker``, ``GrantSet``, the hook registry, the catalog and the MCP
    projection all take *this*, so adding a seventh facet later touches
    :func:`derive_effect_profile` and nothing else.
    """

    effect: EffectClass = EffectClass.WRITE
    open_world: bool = True
    reversible: bool = False
    idempotent: bool = False
    access_control: bool = False
    undone_by: str = ""
    source: Source = "default"

    @property
    def irreversible(self) -> bool:
        """The 'send mail' predicate: it happened, outside, and nothing undoes it."""
        return self.open_world and not self.reversible


def derive_effect_profile(
    op: OperationSpec, *, verb: str = "", has_client: bool = True
) -> EffectProfile:
    """Resolve one operation's profile. Precedence, highest first:

    1. what the author declared   — ``op.model_fields_set`` (survives JSON)
    2. what the client's verb says — 97% accurate, measured over 91 operations
    3. what the scopes say         — a read-only scope has never lied (31/31)
    4. the fail-safe default       — WRITE, open, irreversible, non-idempotent

    Never *lowers* a declared class. A derivation that disagrees is reported by
    ``loom toolsets derive-effects --check``, not silently applied — an author
    who wrote ``DESTRUCTIVE`` over a GET knows something the verb does not.
    """
    declared = op.model_fields_set
    scopes = " ".join(op.scopes)

    if "effect" in declared:
        effect, source = op.effect, "declared"
    elif verb and verb.upper() in _VERB_EFFECT:
        effect, source = _VERB_EFFECT[verb.upper()], "derived"
    elif any(frag in scopes for frag in _READONLY_SCOPE):
        effect, source = EffectClass.READ, "derived"
    else:
        effect, source = EffectClass.WRITE, "default"

    return EffectProfile(
        effect=effect,
        open_world=op.open_world if "open_world" in declared else has_client,
        reversible=op.reversible if "reversible" in declared else bool(op.undone_by),
        idempotent=op.idempotent if "idempotent" in declared
                   else effect is EffectClass.READ,
        access_control=op.access_control if "access_control" in declared
                       else any(frag in scopes for frag in _ACL_SCOPE),
        undone_by=op.undone_by,
        source=source,
    )
```

### 3.2 `toolsets/derive.py` (NEW) — the AST pass

```python
"""Recover each client method's HTTP verb without importing the client.

Importing would pull httpx and the vendor's models into a lint run, and
`loom certify` is meant to be cheap enough for a pre-commit hook.

Coverage is partial and that is stated rather than hidden: 91 of 320 shipped
operations resolve to a single-verb client method today, because clients use
four helper shapes (`_request`, `request`, `_call`, `_data`) and some — Slack,
where every call is POST — carry no signal at all. An operation with no
recoverable verb falls through to scopes, then to the fail-safe default.
"""

VERB_CALLS = ("_request", "request", "_call", "_data", "_get", "_post")

def verbs_for_module(path: Path) -> dict[str, str]:
    """{client method name: HTTP verb} for methods with exactly one verb.

    Exactly one, deliberately. A method that issues a GET and then a DELETE is
    a destructive method whose verb cannot be read off a single literal, and
    guessing between them is how `drive_trash_file` became a read.
    """
```

### 3.3 `toolsets/manifest.py` (CHANGED)

```python
class ToolsetManifest(BaseModel):
    schema_version: int = 1
    """Which effect-classification contract this manifest was written against.

    1 — ``effect`` defaults to READ (pre-Phase-12; what is published today).
    2 — ``effect`` defaults to WRITE and the facets exist.

    Gating on this is the only safe way to flip a default in an ecosystem: a
    manifest published against v1 keeps v1 meaning, and `loom certify` reports
    it as un-migrated rather than silently reclassifying somebody's toolset.
    """

class OperationSpec(BaseModel):
    effect: EffectClass = EffectClass.READ   # v1 default; v2 resolves to WRITE
    idempotent: bool = False                 # unchanged, now enforced

    open_world: bool = True
    reversible: bool = False
    undone_by: str = ""
    access_control: bool = False
    effect_by: dict[str, dict[str, EffectClass]] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    """Open extension point. Policy may **not** key on these — that is what
    keeps the enforced set closed and stops the next six facets landing here
    as a migration across every manifest in existence."""
```

### 3.4 `toolsets/catalog.py` (CHANGED)

```python
def profile_of(self, toolset_id: str, function: str) -> EffectProfile | None:
    """Replaces ``effect_of``, which is kept as a deprecated shim.

    Keyed on ``(toolset_id, function)`` — the flat function-name map silently
    resolved a collision to whichever manifest registered last, and entry
    points let any installed package land in it (F9).
    """

def register(self, manifest: ToolsetManifest, /) -> None:
    # Update the index incrementally instead of invalidating it. Measured:
    # full invalidation costs 0.43s to register 1000 toolsets with interleaved
    # lookups. Bounded, but there is no reason to pay it.
    self._profiles.update(self._index(manifest))
```

### 3.5 `runtime/taint.py` (CHANGED)

```python
# BEFORE — any READ taints, so control.filter reports "read external data"
if effect is None or EffectClass(effect) is not EffectClass.READ:
    continue

# AFTER — only an open-world read is evidence the run touched the world
profile = self._profile_from_entry(entry)
if profile is None or not (profile.open_world and profile.effect is EffectClass.READ):
    continue
```

```python
@dataclass(frozen=True)
class TaintPolicy:
    block_writes: bool = True
    block_destructive: bool = True
    block_irreversible: bool = False   # NEW, off by default
    block_access_control: bool = False # NEW, off by default
```

Both new dials default **off**. Turning them on for existing deployments would
newly refuse operations those workflows already perform — the facets have to be
populated and observed before they gate anything.

### 3.6 `mcp_server/annotations.py` (NEW)

```python
def annotations_for(profile: EffectProfile) -> ToolAnnotations:
    """Project an EffectProfile onto MCP's four hints.

    `destructiveHint` folds in irreversibility: MCP's boolean is coarser than
    LOOM's two fields, and the honest projection of "nothing unsends this" is
    `true`. That puts gmail_send_message on the cautious side of a consent
    prompt, which is the outcome F6 wants.
    """
    return ToolAnnotations(
        readOnlyHint=profile.effect is EffectClass.READ,
        destructiveHint=profile.effect is EffectClass.DESTRUCTIVE or profile.irreversible,
        idempotentHint=profile.idempotent,
        openWorldHint=profile.open_world,
    )
```

`server.py` today hand-writes 24 correct annotation sets. They are **not**
deleted in the same commit that adds this: step 7 asserts the projection
reproduces all 24 first, and only then removes them.

---

## 4. Files Requiring Changes

| File | Change | Finding |
|---|---|---|
| `toolsets/effects.py` | NEW — `EffectProfile`, `derive_effect_profile` | F1 F6 F7 |
| `toolsets/derive.py` | NEW — AST verb recovery | scale |
| `toolsets/manifest.py` | `schema_version`, facets, `labels` | F1 F6 F7 F8 |
| `toolsets/catalog.py` | `profile_of`, keyed index, incremental register | F9 |
| `toolsets/certify.py` | CERT-04 via `model_fields_set`; CERT-05; CERT-13/14 | F2 |
| `agents/tool_registry.py` | `_guess_effect` → `\| None`, fail to WRITE | F1 F3 |
| `runtime/taint.py` | key on `open_world`; two new dials | F4 F6 F7 |
| `runtime/effects.py` | `EffectCall.profile` beside `.effect` | — |
| `runtime/context.py` | attach profile; resolve `effect_by` | F8 |
| `nodes/spec.py` | `open_world` on `NodeSpec`; classify 26 nodes | F4 |
| `mcp_server/annotations.py` | NEW — projection | Phase D |
| `mcp_server/server.py` | use the projection, drop hand-written sets | Phase D |
| `cli/commands.py` | `loom toolsets derive-effects [--check]` | scale |
| `testing/conformance.py` | `verify_effect_profile` kit | scale |
| `toolsets/*/manifest.py` | overrides only, where derivation disagrees | — |

---

## 5. Implementation Steps

Each step is one PR, independently revertible, and leaves the suite green.

**Step 1 — close the fail-open defaults. — DONE**

*Originally* this step added `schema_version` to gate a later default flip,
because flipping `OperationSpec.effect` from READ to WRITE would silently
reinterpret manifests published against the old contract. **That premise did
not hold and the step was rewritten.** LOOM is at `0.1.0`, has no git tags, and
everything in `CHANGELOG.md` is `[Unreleased]` — the `loomflow` name on PyPI
belongs to an unrelated project. There are no manifests in the wild to migrate,
so a version gate protected nothing and would have been permanent API surface
bought with a hypothetical.

Two measurements made the direct change free:

```
320 / 320 shipped operations declare `effect` explicitly
  0 / 320 rely on the default
```

So flipping the default is a **no-op for every shipped toolset**, and CERT-04
as a hard error passes on all 23 immediately. No migration, no warning
severity, no staged rollout.

*Shipped:* `OperationSpec.effect` defaults to `EffectClass.WRITE`, which also
makes the manifest agree with `EffectCall.effect` — the two had always
disagreed about the same question. CERT-04 asks `"effect" in
op.model_fields_set` rather than `if not op.effect`, which could never fire.
CERT-05 no longer exempts unclassified operations, since they are now writes.
Nine tests including a **corpus guard** asserting every shipped operation
declares its own effect, so a new toolset that forgets is caught at CI rather
than by whatever it is later granted.

*Not shipped, deliberately:* `schema_version`, `CertWarning`, and the
two-severity certification model. All three existed only to make the migration
safe, and there is no migration.

**Step 2 — `EffectProfile` + `derive_effect_profile`, unused. — DONE**
Add `toolsets/effects.py` and its unit tests. Nothing consumes it yet, so this
is pure addition — the step where the shape is reviewed before anything depends
on it.

*Shipped:* `EffectProfile` (frozen, six facets + `source`), the `irreversible`
predicate, `derive_effect_profile` with the four-level precedence, and
`verb_disagreement` — which reports the direction that is a likely mistake
(declared class weaker than the verb implies) and stays silent on the direction
that is a judgement (stricter than the verb implies). 25 tests, two of them run
against the shipped manifests rather than fixtures.

*Corrected while implementing:* the plan claimed `access_control` derives from
scope strings. It does not — measured across all 320 operations it matches
**zero**, because Google covers permissions with the broad scope and the
Microsoft toolsets declare none. It is a declared facet, and the module records
why rather than shipping a derivation that reads as computed while only ever
being absent.

*Still inert by design:* `reversible` / `undone_by` / `access_control` need
manifest fields that arrive in step 6, so every profile today reports
`irreversible=True`. That is the safe direction, and it is why the two
`TaintPolicy` dials that read it ship off.

**Step 3 — the AST pass and the CI gate. — DONE**
Add `toolsets/derive.py` and wire the check into CI.
*This is the step that makes the whole thing scale: after it, a wrong effect on
any toolset with a recoverable verb is a failing build rather than a silent
grant.*

*Shipped:* `toolsets/derive.py` — `verbs_in_client` and `wiring_in_tools` read
`client.py` and `tools.py` with `ast`, joined by `verbs_for_manifest`. No
imports, no credentials, cheap enough for a pre-commit hook, and Layer 1 stays
Layer 1. **68 tool functions** resolve to a verb across six toolsets. The gate
is `tests/test_effect_derivation.py`, 13 tests.

*Proved rather than assumed:* seeding `clickup tasks.delete` from DESTRUCTIVE
to READ fails the gate with the operation named. The first seed attempt used
`drive_delete_file` and **passed** — Drive routes through `GoogleSession` and
issues no verb literal — which is exactly why coverage is now pinned by two
tests rather than described in a docstring.

*Design change made while implementing:* `POST` no longer contradicts a
declaration. Both disagreements across all 320 operations were the same shape —
`hubspot_search_objects` and `outlook_get_schedule`, searches whose parameters
are too complex for a URL. GET means read and DELETE means destroy in
essentially every REST API; POST means create *and* query-by-body, so treating
it as contradicting a READ would report every search-by-body endpoint as a
defect until somebody suppressed the check. POST still *derives* WRITE where
nothing is declared — guessing cautiously costs an approval, and the exclusion
is only about overruling a human.

*Not shipped:* the `loom toolsets derive-effects --check` CLI command. The gate
runs in the test suite, which is what CI executes; a second entry point to the
same check would be a surface to keep in sync for no coverage gain. Worth adding
when a third-party toolset author needs it outside this repo — that is step 9's
conformance kit, where it belongs.

**Step 4 — close the fail-open holes. — DONE**
`_guess_effect` returns `EffectClass | None`; callers default to WRITE and log
at `INFO` naming the operation. Add `tests/test_effect_guess.py` with the
320-operation corpus and the **zero-under-classification** assertion.

*(CERT-04 and CERT-05 moved into step 1; the `schema_version` resolution was
dropped with the gate itself.)*

*Shipped:* `_guess_effect` can now abstain — `None` means *the name carries no
signal*, which is not the same as "this is a read", and conflating the two is
what made the old fallback dangerous. `_resolve_guess` turns an abstention into
`WRITE` and logs the operation name, because a toolset where every operation
defaults to WRITE is safe **and useless** — `resolve_tools(effects={READ})`
hands the agent nothing — and the log is what turns that from a mystery into a
one-line `effects={...}`. A `_READ_WORDS` list was added so READ is now a
positive classification rather than the absence of one.

Both verb lists were grown from the corpus's own misses rather than from
imagination — `archive`, `trash`, `unshare`, `end` for destructive; `upload`,
`reply`, `move`, `share`, `invite`, `copy`, `rename`, `forward` and a dozen
more for write.

```
                       before      after
exact                 273 (85%)   295 (92%)
UNDER-classified       46 (14%)     0
over-classified         1           25
abstained -> WRITE      -            5
```

The 25 over-classifications are deliberate and left alone: over-classifying
costs an approval, under-classifying costs a deletion, so the corpus test
asserts **zero** of the second and permits the first. 30 tests.

**Step 5 — `open_world` and the taint fix. — DONE**
Add the facet, classify the built-in nodes, switch `TaintBroker`. Land the F4
repro as a regression test. *User-visible: workflows that were failing on
`control.filter` start passing.*

*Shipped:* `open_world` on `OperationSpec` (default `True` — a toolset is a
network call), on `NodeSpec` (default `False` — a node is usually computation),
and on `EffectCall` (default `True`, the direction that adds a refusal rather
than removes one). 11 of 27 built-in nodes marked open: `io.*`, `agent.*` and
`human.*` — the last because a person's answer enters the run from outside.
`ToolsetCatalog.open_world_of` is the Layer-1 lookup; `ctx.node` and `ctx.step`
carry the facet into journal metadata. 5 regression tests.

*Found while implementing:* taint is applied on **two** paths, not one.
`observe_run` derives it from the journal at re-entry, and `dispatch` adds to it
after a successful call. Fixing only the first left the repro still failing —
the live path had no scope to key on — which is why `EffectCall` gained the
field and not just the journal metadata. Worth knowing for step 6, whose dials
read the same two paths.

*Backward compatibility:* a journal entry without `open_world` is read as open.
It cannot say, and assuming open preserves every refusal that run would already
have seen — the direction that grants nothing new.

**Step 6 — reversibility and access control. — DONE**
Add `reversible` / `undone_by` / `access_control` and the two `TaintPolicy`
dials, off by default. Populate across the 23 toolsets — `undone_by` by hand
(it is a genuine judgement, and CERT-14 verifies the id resolves),
`access_control` derived from scopes. *This is the requested capability.*

*Shipped:* `reversible`, `undone_by` and `access_control` on `OperationSpec`
and on `EffectCall` — the latter because step 5 proved taint runs on two paths
and the dispatch path needs the facets too. `TaintPolicy.block_irreversible`
and `block_access_control`, both **off by default**. `ToolsetCatalog.profile_of`
replaces the per-facet lookups, so a seventh facet edits one function rather
than every call site. CERT-14 checks that `undone_by` resolves and agrees with
`reversible`. 23/23 toolsets still certify.

*The policy this makes expressible*, which is the point of the whole phase:

```python
TaintPolicy(block_writes=False, block_irreversible=True)
```

After reading the open web you may update a record; you may not send the email.
Under `EffectClass` alone that sentence could not be written, because
`gmail_trash_message` is DESTRUCTIVE and recoverable for thirty days while
`gmail_send_message` is WRITE and is not — so blocking DESTRUCTIVE and
permitting WRITE stops the recoverable operation and allows the irreversible
one. Pinned by `test_the_gmail_inversion_is_resolved`.

*Declared, not derived, and deliberately sparse.* Four operations are marked
reversible and eight access-control. The naive pairing — `issues.create` undone
by `issues.delete` — was rejected: deleting an issue you created does not undo
the create, it consumes a key and loses the comments. Only a genuine restore
counts, which is trash/untrash, files.restore, and share/unshare.

*Two bugs found by testing against real manifests rather than fixtures.*
`derive_effect_profile` still hardcoded `reversible=False` from step 2, so every
declaration was ignored — visible only because the check printed real Gmail
profiles. And the dials would have refused **reads**: every read is
"irreversible" under the literal definition, so the dial would have blocked the
very call that taints and stopped the run at step one. Both fixed; the read
exemption has its own test.

**Step 7 — carry the facets to agent tool calls. — DONE (rescoped)**

*The planned work was an MCP projection, and investigating it found no
consumer.* `loom mcp` does not expose toolset operations as MCP tools — it
exposes `search_toolsets`/`show_toolset` for discovery, and operations are
called from generated workflow code. The 24 run-management tools are already
annotated by hand and correctly. `Tool` has no annotation field, and the agent
backends convert to framework-native tools that would not read one. Building
`annotations_for` would have been a third thing shipped with nothing to consume
it, after the `schema_version` gate and the scope-based ACL derivation.

*What the investigation found instead is a real hole.* An agent's tool call
**does** reach the broker — `runner.py` dispatches `kind="tool"` — but carried
only `effect`. So step 6's dials did not work where an agent calls a tool,
which is the case they exist for: `block_irreversible` saw every agent tool
call as irreversible and would have refused all of them.

*Shipped:* `Toolset.resolve` stamps `open_world`, `reversible` and
`access_control` beside `effect`; the runner carries all four into the
`EffectCall`. An agent that has read the web can now update a record and cannot
send the email. Two tests.

*Also found, and left alone:* `Tool.requires_approval` is defined, documented,
and called by nothing in src, tests, examples or docs. Wiring it would create a
second mechanism for what the effect hooks and the broker already do —
CLAUDE.md is explicit that "may this tool call run?" is already an effect hook
on `kind="tool"`. Recorded here rather than removed, since deleting public
surface is the user's call.

**Step 8 — enforce idempotency, retire the drift. — DONE**

All 26 reconciled, per operation, by which half was wrong:

* **18 declarations were wrong.** delete, update, archive, share/unshare,
  transition, end — naming the same resource twice reaches the same end state,
  so the retry was right. This is the rule LOOM's own docs already state.
  `idempotent=True` now declared.
* **8 retries were wrong.** `jira_create_issue`, `jira_add_comment`,
  `confluence_create_page`, `confluence_add_comment`, `calendar_create_event`,
  `calendar_create_calendar`, `calendar_quick_add_event`, `gmail_create_label`
  — no idempotency key, so a timeout after the service accepted the request is
  indistinguishable from a failure and the retry files it twice. Retry disabled,
  following the `_SEND` pattern gmail already established.

*Drift is now zero,* and `tests/test_effect_conformance.py` is the gate that
keeps it there — the shape `test_manifest_imports.py` already uses for
pagination.

**Step 9 — third-party surface. — PARTLY DONE**

*Shipped:* `loom.testing.conformance.verify_effect_profile` — the kit a toolset
author runs in their own CI, the shape `verify_event_log` and
`verify_event_source` already use. Checks explicit declaration, idempotence
against the real retry policy, that `undone_by` resolves, and — when handed
`client_source` — that no declaration contradicts its own client. The verb
check is a separate argument rather than a default, so omitting it *skips*
rather than silently passes. Verified both ways: gmail passes, a manifest with
a dangling inverse is refused with the reason.

*Not shipped:* certification tiers and `Runtime(require_effect_tier=…)`. That
is a policy surface for an ecosystem of third-party toolsets, and there is not
one yet — the same reasoning that removed `schema_version`. The kit is what a
third-party author needs today; the tier is what a *host* needs once it is
installing toolsets it did not write.

**Step 10 — `effect_by`. — DONE**

`effect_by` on both `OperationSpec` and `NodeSpec`, resolved per call by
`resolve_effect` in the dispatch path. Declared on `io.http_request`, which is
the node this exists for: one node, one class, and it is what a generated
workflow reaches for when no toolset covers the API — so `method="DELETE"`
reading as a write is a real hole.

A matched rule wins in **either** direction (`GET` lowers, `DELETE` raises);
the declared class is the fallback whenever the argument was absent or its
value is not in the table, so an unrecognised method keeps the cautious class
rather than falling to a read. 6 tests.

*Not applied to the generic CRUD operations.* `salesforce_delete_record` and
`hubspot_archive_object` already declare DESTRUCTIVE outright, so a table would
restate what the class says. It earns its place where one entry point spans
several classes, and `io.http_request` is the only such case shipped. Last because it is the only step that touches the dispatch path.

---

## 5b. Toolset certification — completed alongside steps 1–2

Not in the original plan. Surfaced while auditing what was pending in
`toolsets/`: **no shipped toolset passed `loom certify`** — 0 of 23 — and two of
the three failing checks were defects in the checks themselves, masking the two
that were real.

```
        CERT-05  CERT-08  CERT-10      clean
before       12       23       20      0/23
after         0        0        0     23/23
```

**CERT-08 demanded a hand-written `fakes_module`.** `agents/fakes.py` generates
stand-ins from `output_schema` precisely so no parallel set of fakes can drift
— *"there is only one contract"* — and no shipped toolset declares the module.
The real precondition is `tools_module`: without it `install_fakes` returns an
empty list and the smoke sandbox runs against the live service, producing the
401 whose cheapest repair is deleting the integration. The check now tests
that, plus a `function` on each operation.

**CERT-05 demanded OAuth scopes from toolsets that have none.** An Exa or
Tavily API key carries no scopes, Jira and Confluence authenticate with an
email and an API token, DuckDuckGo has no credential at all. The check now
keys on the declared auth model, which every manifest already carries. That is
CERT-08's mistake in a second place: failing a correct toolset for omitting
something its API does not define teaches people to ignore the check.

**The two real gaps were then filled.** Scopes for the seven OAuth2 toolsets
that lacked them — OneDrive and SharePoint from the least-privilege table
already researched in `docs/design/onedrive-sharepoint-toolsets.md`, Teams,
OneNote and both Outlook toolsets from Microsoft's permissions reference,
Salesforce from its own coarse `api` scope. Rate limits for the twenty that
omitted them, recorded as the vendor documents them: a Graph or Atlassian
figure would be an invented constant, so those say *dynamic, honour
Retry-After* and *points-based per-hour quota* instead of a number.

**One bug this surfaced in step 2's own code.** `READONLY_SCOPE_FRAGMENTS`
matched by substring, and `.read` is a prefix of `.readwrite` — so
`Sites.ReadWrite.All` read as read-only the moment real Graph scopes landed,
which would have classified every SharePoint write as a read. Replaced with
`scope_is_readonly`, which excludes write markers and requires *every* scope in
the set to qualify, since one broad scope alongside a narrow one grants what
the broad one grants. The corpus test caught it, which is what it is for.

## 6. Multi-Angle Review

**Correctness.** The precedence order is the whole contract, and the rule that
derivation never *lowers* a declared class is what keeps it safe: an author who
wrote DESTRUCTIVE over a GET is telling you something the verb cannot. The
inverse — derivation raising a class the author set too low — is reported, not
applied, because silently overriding a declaration makes the manifest stop
meaning what it says.

**Security.** Every default moves toward *more* restriction, so the failure
mode of a bug in this phase is a refused call, not a permitted one — the
opposite of today's F1/F3 behaviour. `require_effect_tier` is the first control
that distinguishes a verified classification from an asserted one, which is
what the `loom_toolset` entry point makes necessary. The two new taint dials
ship off: a security control that breaks running workflows on upgrade gets
turned off wholesale, which is worse than not shipping it.

**Performance.** Runtime cost is one frozen dataclass lookup per dispatch,
replacing a dict lookup — unmeasurable next to the I/O it gates. The AST pass
never runs at runtime. `register` becomes O(ops) incremental instead of
O(total) invalidating, removing the measured 0.43s/1000-toolset rebuild.

**Edge cases.** A client with no recoverable verb (Slack: everything is POST)
falls through to scopes and then to the fail-safe default — partial coverage,
stated. A manifest at `schema_version: 1` keeps old defaults forever, so old
toolsets never break; `loom certify` reports them un-migrated. `undone_by`
pointing at a deleted operation is CERT-14. Two toolsets sharing a function
name now raise at registration instead of silently overwriting.

**Maintainability.** The count that matters: hand-declared facets per
operation. The rejected draft needed five; this needs `undone_by` where one
exists, plus an override where derivation is wrong — measured at 2 of 91.
Consumers depend on one frozen type, so facet seven is a one-function change.
`labels` gives the next request somewhere to go that is not a migration.

**Testing.** The 320 shipped operations are the corpus, and the assertion is
directional — zero under-classifications, over-classification allowed. That
is the right asymmetry: over-classifying costs an approval, under-classifying
costs a deletion.

**User perspective.** Steps 1–4 are invisible. Step 5 *unbreaks* workflows.
Step 6 is the feature. Step 8 is the only one that can change retry behaviour
in production, which is why it is late and per-operation.

---

## 7. Test Plan

### Unit (11)
1. `derive_effect_profile` precedence — declared beats verb beats scope beats default
2. Declared class is never lowered by a derivation
3. `model_fields_set` detection across construct / `model_validate` / `model_validate_json`
4. `schema_version` 1 vs 2 resolve `effect` differently for an undeclared op
5. `irreversible` is `open_world and not reversible`
6. `annotations_for` — the four hints, including irreversible ⇒ `destructiveHint`
7. `verbs_for_module` — single verb recovered; two verbs ⇒ no answer
8. `_guess_effect` returns `None` rather than READ for an unknown verb
9. `profile_of` keyed by toolset; collision raises `ConfigurationError`
10. `effect_by` resolves from an argument; static `effect` is the ceiling
11. `labels` cannot be read by any policy object (attribute absent from `EffectProfile`)

### Corpus (3) — the ones that scale
12. `test_effect_guess.py` — **zero under-classifications** over all 320
13. `test_effect_conformance.py` — declared `idempotent` ⟺ actual retry config
14. `test_effect_conformance.py` — a read-only scope implies READ (31/31 today)

### Integration (5)
15. F4 repro — closed-world node then write **completes**
16. Open-world read then write is **still refused**
17. `block_irreversible=True` parks `gmail_send_message`, permits `gmail_create_draft`
18. `require_effect_tier="derived"` refuses a self-asserted manifest
19. Projected MCP annotations equal the 24 hand-written sets (then they are deleted)

### E2E (2)
20. `loom toolsets derive-effects --check` exits non-zero on a seeded wrong effect
21. A third-party toolset passes `verify_effect_profile` in its own CI

---

## 8. Rollback

Steps 1–3 add code nothing consumes and revert cleanly. Step 4 is the first
behaviour change and is gated on `schema_version`, so reverting is setting the
gate back. Step 5 changes a refusal into an allowance — reverting re-breaks the
F4 workflows, so it should be reverted only with the repro test. Steps 6–7 are
additive and off by default. Step 8 is the one to watch in production: it
changes retry counts, and its blast radius is per-operation by construction.
