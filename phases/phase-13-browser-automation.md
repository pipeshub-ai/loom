# Phase 13 — Browser Automation

**Status:** design
**Depends on:** Phase 1 (nodes, Context), Phase 3 (connections, effect classes),
Phase 5 (blobs, compensation), Phase 7 (validator, stages), Phase 12 (taint,
`open_world`)

---

## 0. The gap, stated precisely

LOOM can reach a service that publishes an API. It cannot reach a service that
publishes only a web page — and that is most of the world a workflow automation
system is asked to touch. Today the only outbound nodes are `io.http_request`
and `io.wait_for_webhook`; `grep -rln playwright src/loom/` returns two files,
both under `agents/probes/`, neither reachable from a workflow body.

`BrowserProbe` renders a page and reports what is on it. That is the *authoring*
half and it is finished. What is missing is the *running* half: a workflow that
navigates, fills, and submits — durably, with the effect visible to the broker
chain, and with a person in the loop where one belongs.

Three properties make this different from adding another toolset, and each one
drives a decision below:

1. **A browser is a stateful conversation.** `io.http_request` is
   self-contained: replay serves the recorded response and nothing is lost. Step
   7 of a browser flow is meaningless without steps 1–6 having happened *to that
   browser*. The journal replays values; it cannot replay a browser.
2. **The effect of an action is not derivable from the action.** `click "Next"`
   navigates; `click "Confirm booking"` charges a card. The strings are the same
   shape. Nothing short of the author saying so distinguishes them.
3. **A page is a snapshot, not a contract.** A selector harvested today is right
   today. There is no `OperationSpec` to validate against, no version to pin.

---

## 1. Exit Criteria & Success Metrics

A criterion that passes on day one whether or not the thing works is not a
criterion. CERT-04 "spent its whole life in that state" — it claimed to require
an explicit effect class, was `if not op.effect` against a truthy `StrEnum`, and
could never fail. This phase is at maximum risk of the same thing, because every
provider and every page fixture shipped with it will be correct on day one.

So each criterion below names **the defect that must make it fail**, and §7
drives that defect through it.

### 1.1 Mechanical invariants — binary, PR CI, cannot drift

| # | Criterion | Made to fail by |
|---|---|---|
| E1 | The same flow written as `ctx.node("browser.*")` and as hand-written `ctx.step` produces **entry-for-entry identical journals** | a node that journals its own wrapper entry |
| E2 | Replay of a settled run issues **zero** model calls and **zero** CDP commands | serving `observe` from the cache instead of the journal |
| E3 | An act declared `WRITE` after an open-world read is refused; the same act after `wait_for_approval` succeeds | `open_world=False` on `browser.navigate` |
| E4 | `SessionScope.DURABLE` against a provider without `reattach` raises **at open**, not at first act | `supports()` consulted lazily |
| E5 | No credential, cookie jar, or `storage_state` appears anywhere in a store dump | `storage_state` returned inline instead of by blob ref |
| E6 | `pip install loomsdk[browser]` adds Playwright and nothing else; no `==` pin, no LLM-vendor SDK, no AGPL package enters the tree | adding `browser-use` as an extra |

E6 is a **CI gate, not a review checklist**: resolve the dependency set, assert
every new distribution's licence is in `{MIT, Apache-2.0, BSD-*, PSF}` and every
new specifier is a range. The audit in §2.2 is worthless the first time somebody
adds a convenient dependency without repeating it.

### 1.2 Behavioural — proven by the kit, and the kit proven against mutants

| # | Criterion | Made to fail by |
|---|---|---|
| E7 | `verify_browser_session` **rejects** a provider that reopens a closed session, mutates page state during `observe`, returns a non-strict `locate`, or leaks a session across `close()` | any of those four, as a mutant provider |
| E8 | `SelectorStage` / `BrowserEffectStage` reject the defect **and stay silent on correct code** | a correct workflow, asserted clean |
| E9 | Smoke passes offline **and fails without the fakes installed** | removing `FakeBrowserProvider`, which must produce a connection error, not a pass |

E8's second half is the one usually skipped and the one that matters: a stage
that fires on everything is worse than no stage, because the repair loop acts
on `report.errors` and will rewrite correct code to silence it.

### 1.3 Empirical — the criterion the design actually rests on

| # | Criterion | Corpus |
|---|---|---|
| E10 | **Tier-0 resolution ≥70%** of interactive controls, measured per control, read off `ActionPlan.tier` | §7.3, frozen pages, offline, committed |

**MEASURED — 2026-08-19. 10 pages, 114 controls, `tests/corpus`.**

| Scoring | Rate |
|---|---|
| role + name exact | 62% |
| name under any role | 74% |
| **+ placeholder / label accessors** | **76%** |

Per page: gitlab 100%, heroku 100%, substack 100%, wikipedia 87%, eventbrite
82%, kayak 71%, zendesk 50%, fastmail 50%, resy 47%, proton 100%. **Floor 47%**
(role-exact floor 29%). Identical across four consecutive runs.

**Verdict: ≥70% — a11y-first holds; build 13.1 as designed.** With two
qualifications that are part of the result, not footnotes to it: N is small
enough to kill a bad premise and too small to defend the precise figure, and the
per-page spread (47%–100%) carries more information than the mean.

Everything above is engineering LOOM controls. E10 is a claim about **the web**,
and it is the load-bearing one: if the a11y tree does not address real
transactional forms, tier 0 is decoration and tier 1 is the product — which
changes the dependency conclusion in §2.2, because carrying a model loop
in-house is defensible as a fallback and much less so as the main path.

**Falsification is declared in advance, with the action:**

| Tier-0 rate | Reading | Action |
|---|---|---|
| **≥70%** | a11y-first holds | ship as designed |
| **40–70%** | tier 1 is the product | reorder 13.1/13.2; re-open whether a specialist adapter should be the default rather than an entry point |
| **<40%** | premise is wrong | stop; redesign around tier 2, and §2.2's "Playwright and nothing else" no longer follows |

Stating this now is the point. Measured after the code exists, any number
becomes the number we hoped for.

## 2. HLD

### 2.1 LOOM ships a port and one reference implementation

Settled by precedent, not preference. `loom/knowledge/` ships two ports, a
reference store over capabilities every LOOM store already has, and a
conformance kit — because "every adapter shipped is one that must be tested
against a real server forever." `loom/events/` takes the same position about
brokers. Browser infrastructure is a market with at least six live vendors
(Browserbase, Kernel, Anchor, Browserless, Steel, Bright Data); shipping
adapters for them is the same trap one layer out.

So: `BrowserProvider` and `BrowserSession` as Protocols, `LocalBrowserProvider`
over playwright behind the existing `[browser]` extra, `FakeBrowserProvider` for
smoke, and `verify_browser_session` so a host proves its Browserbase adapter
correct. `Runtime(browser=…)` defaults to `None` and nothing is enforced unless
a host composes one in.

### 2.2 Build vs adopt — the audit that decides the shape

LOOM is **MIT** (`pyproject.toml:10`) and declares exactly two runtime
dependencies: `pydantic>=2.0` and `pydantic-settings>=2.0`. Both facts are
constraints, and together they eliminate most of the field.

| Library | License | Python | Dependency surface | Verdict |
|---|---|---|---|---|
| `playwright` | Apache-2.0 | native | 3, ranged | **engine** |
| `patchright` | Apache-2.0 | drop-in fork | ≈playwright | **stealth engine** |
| `stagehand-python` | MIT | official port | **6, all ranged**, no LLM SDKs | adapter |
| `browser-use` | MIT | native | **~40, `==` pinned**, + telemetry | adapter only |
| `Skyvern` | **AGPL-3.0** | — | — | **disqualified** |
| `workflow-use` | **AGPL-3.0** | — | — | **disqualified** |

Three findings drive everything below.

**AGPL is a hard stop, and it catches the two record-and-replay tools.** Skyvern
and `workflow-use` are both AGPL-3.0 — and note that `workflow-use` is AGPL
*while `browser-use` itself is MIT*, so the org's licence is not a guide. AGPL's
network clause reaches any host that offers LOOM over a network, which is what
`loom serve` is for. Vendoring, forking or importing either is out.

**`browser-use` is MIT, 110k stars, pushed daily — and unusable as a
dependency.** Its `pyproject.toml` pins ~40 packages with `==`, including
`pydantic==2.12.5`, `httpx==0.28.1`, `openai==2.16.0`, `anthropic==0.76.0`,
`google-genai`, `groq`, `ollama`, `mcp`, `reportlab`, `pypdf`, and `posthog`
(telemetry, opt-out). `pydantic==2.12.5` alone is disqualifying: LOOM declares
`pydantic>=2.0`, so `pip install loomsdk[browser-use]` would hand a user an
unresolvable conflict, or silently pin their whole application's pydantic. An
SDK cannot do that to a host. It is an excellent **adapter** and a bad
**dependency**, and the distinction is the design.

**`stagehand-python` is the clean one and the immature one.** Six ranged deps,
no vendor SDKs, no telemetry — exemplary. But 514 stars against the TypeScript
repo's 24k, and last pushed a month ago while TS ships daily. A second-class
port is not what "rock solid" rests on.

**Conclusion: LOOM depends on Playwright and nothing else.** The mechanical
primitives — navigate, snapshot, perform, `storage_state` — are Apache-2.0,
boring, and deterministic. Everything smarter is reached through the port, so
Stagehand and browser-use are **host adapters discovered by entry point**
(`loom_browser_provider`, the fifth of a pattern `loom_toolset`, `loom_node`,
`loom_probe` and `loom_event_source` already establish). LOOM ships zero
adapters, takes zero LLM-vendor dependencies, and proves third-party ones with
`verify_browser_session`.

### 2.3 Element resolution is a dial, and tier 0 has no model in it

This is the correction that matters most for reliability. An earlier draft had
LOOM implementing observe as "a11y tree in, model out." That is the hardest and
most-iterated component in this entire space, and hand-rolling it is where rock
solid goes to die.

It is also, for a large fraction of real pages, **unnecessary**. Playwright's
own locators resolve by accessible role and name, auto-wait, and are **strict**
— an ambiguous locator raises rather than picking the first match. Raising on
ambiguity is precisely the behaviour you want when the alternative is silently
clicking the wrong control.

So resolution escalates, and each tier is a dial position, not a fallback path:

| Tier | Mechanism | Model calls | When |
|---|---|---|---|
| **0** | `get_by_role(role, name)` → any role → `get_by_placeholder` → `get_by_label` | **none** | always tried first |
| **1** | LOOM's `ModelProvider` over the a11y tree | one | tier 0 empty or ambiguous |
| **2** | a specialist provider (Stagehand, browser-use) | provider's | host installed one |

**That tier-0 chain is the corpus's finding, not a guess.** The dominant real
failure is not a control the tree fails to name — it is a control the tree names
*differently from the text on screen*, because `aria-label` overrides
`placeholder` in accessible-name computation:

| Page | Placeholder — what a person reads | `aria-label` — what the tree says |
|---|---|---|
| substack | `Your email` | `Email` |
| kayak | `To?` | `Destination location` |
| heroku | `First name` | `First name`, plus a real `<label>` |

Heroku agrees on all three and resolves 100%. The other two are unreachable by
what anyone would actually write — until tier 0 also tries the placeholder and
label accessors, which cost nothing and no model call. Measuring before building
the resolver is what surfaced that; it would have shipped as a silent 26-point
hole otherwise.

Tier 0 is the default and it is deterministic, reproducible, and free. Tier 1
exists for the pages tier 0 cannot address. Tier 2 exists because a host that
has already bought Browserbase should not be made to re-solve this.

The consequence for `ActionPlan` is a simplification: **role + accessible name +
ordinal is not merely the drift fingerprint, it is the locator.** A CSS path is
carried only as a provider-native escape hatch, never as the primary. That kills
the failure §2.8 was written to contain, one layer earlier — you cannot bake in
a brittle selector that was never the addressing mechanism.

### 2.4 The primitive set is the converged one

### 2.5 The journal is the action cache

This is the load-bearing insight, and it is the one thing LOOM has that no
product in this space does.

Stagehand's headline feature is the observe-then-act cache: discover an action
once with a model, get back an `ObserveResult` (`description`, `method`,
`selector`, `arguments`), cache it, and replay it deterministically for free.
Skyvern's is code caching: record what the agent did, compile it to Playwright,
execute the cache on later runs and skip inference. Both built a bespoke
persistence layer to hold it.

LOOM already has that layer. `browser.observe` is an ordinary journaled call
that returns an `ActionPlan`; `DurableCall._resolve` serves a completed entry
from the journal before the broker is ever reached. So on replay the plan comes
back with no model call — and the `act` that consumed it comes back with no
click. **The cache is not a feature to build. It is what the engine already
does**, and E2 is a test rather than an implementation.

What the journal does *not* give is caching across **runs**, which is where
Stagehand's saving actually lands. That is `PlanCache` over `ctx.state` — the KV
space shared by every run of one workflow — keyed by `(url_shape, intent)`.
Consequence, and it is the documented one: state is deliberately not journaled,
so an `act` whose plan came from cache legitimately replays with different
arguments. That is precisely the case `VerifyMode.WARN` exists to tolerate and
the reason `STRICT` is not the default. No new machinery; a known seam, used as
intended.

```
run 1   observe ──model──> ActionPlan ──> journal + PlanCache(state)
run 2   observe ──cache──> ActionPlan ──> journal
replay  observe ──journal─> ActionPlan          (no model, no cache read)
        act     ──journal─> ActResult           (no click)
```

### 2.6 Session durability: one honest default, one opt-in

The hard problem. A crash at action 7 of 10 leaves the journal able to serve
1–6 and a step 7 that needs a live browser positioned where 6 left it. Nothing
in the journal can restore that.

Two scopes, author-declared:

**`SessionScope.STEP` — the default.** The whole browser flow is the body of one
durable call. Crash means the call re-runs, which is exactly what `Retry`
already means everywhere else in LOOM. Journal granularity equals session
lifetime, so there is no state to reconstruct and **no new durability semantics
at all**. The cost is real and stated plainly: a re-run repeats whatever the
first attempt did, so a flow with a non-idempotent submit needs an idempotency
key or a `ctx.compensate()` handler — the same answer LOOM already gives for any
step that must not run twice.

**`SessionScope.DURABLE` — opt-in, provider-gated.** The session outlives the
process because it lives in a provider that keeps it (this is what Browserbase
and Kernel sell). LOOM journals a `SessionHandle`, reattaches on re-entry, and
each `act` becomes its own journal entry with fine-grained resumption. A
reattach that fails raises **`SessionLost`**, a typed exception a workflow can
branch on — the rule `EffectDenied` established when replay was widened to
reconstruct failure *types* and not only messages.

**A scope the provider cannot honour is refused, not downgraded.**
`BrowserProvider.supports` reports what the adapter actually does, exactly as
`ExecutionSandbox.enforces` reports which `SandboxPolicy` fields a platform
honours and `run` refuses a policy outside it. A host told "not here" is better
off than one that believes its two-hour approval flow will survive a deploy when
the session dies with the pod.

**Human takeover requires `DURABLE`, and that falls out rather than being bolted
on.** A run parked on `ctx.wait_for_approval` costs nothing — but the *browser*
must still be there when the person finishes the 2FA. That is a provider
property, so the constraint is checked where every other requirement is:
`NodeSpec.requires`, before the run parks. The failure this prevents is the
worst one available in the human path and is already named in `nodes/human/`: a
run parked with nobody listening is indistinguishable from patience.

### 2.7 The effect of an act is declared, never inferred

`ActIn.effect` is required in practice and defaults to `WRITE`.

Precedent is exact. `OperationSpec.effect` defaults to `WRITE` and CERT-04
requires an explicit declaration, "because the default is a fail-safe backstop,
not a classification." The live trap documented beside it is the *name guess* in
`from_steps`, which under-classifies 14% of LOOM's own operations by reading
verbs — and a natural-language click intent is that same guess with less to go
on. `if "confirm" in intent.lower()` is a keyword list: the tell
`DEFAULT_SYSTEM_PROMPT` already names for a rule that should not be written.

What this buys is the whole safety story, for free:

```python
await ctx.node("browser.navigate", {"url": restaurant_url})       # READ, open-world → taints
plan = await ctx.node("browser.observe", {"intent": "the 7pm slot"})
await ctx.node("browser.act", {"plan": plan, "effect": "read"})    # select — fine
await ctx.wait_for_approval("booking", detail=summary)             # clears the taint
await ctx.node("browser.act", {"intent": "click Confirm",
                               "effect": "write"})                 # allowed only now
```

Without the approval, `TaintBroker` refuses the submit and names the page the
run read. That is the property you want when a model wrote the body, and
**`runtime/taint.py` needs no browser-specific line** — the rule keys on
`EffectClass` and `open_world`, both of which `browser.*` declares like any
other node.

### 2.8 Self-healing is allowed to navigate and not to commit

Every product in this space self-heals: cached selector breaks, re-query the
model, carry on. That is correct for navigation and dangerous for a submit — a
plan that silently changed under an effectful action is how an agent confirms
the wrong reservation, and it fails exactly the way this codebase keeps naming:
it succeeds, and the answer is wrong.

The rule follows from §2.7 and needs no new concept:

- `effect=READ` → drift is repaired silently. Re-observe, re-act, record both.
- `effect=WRITE`/`DESTRUCTIVE` → a plan that no longer matches raises
  `SelectorDrift`. If the run holds an approval, the approval was for the plan
  that was shown; a materially different plan invalidates it.

`ActionPlan.fingerprint` (role + accessible name + ordinal, deliberately *not*
the raw selector) is what "materially different" is measured against. A CSS path
changing under a stable labelled button is drift the tree does not see, which is
the point of fingerprinting the tree rather than the DOM.

### 2.9 Where the browser runs

**Never in the sandbox.** `DockerSandbox` is `--network none` with a read-only
root by construction, and a browser needs the network and a writable profile
dir. A sandboxed body calling `ctx.node("browser.act", …)` sends a line of JSON
to the parent, and the parent drives the browser — which is the documented model
already: *untrusted orchestration over trusted effects.* The body decides what
to click; the parent decides whether, does it, and records it.

This is strictly better than the alternative and worth stating: it means a
model-authored booking flow can run under `--network none` and still book,
because the only thing that ever touches the network is code the host wrote.

---

## 3. LLD

### 3.1 `src/loom/browser/base.py`

```python
class ActionMethod(StrEnum):
    CLICK = "click"; FILL = "fill"; SELECT = "select"; CHECK = "check"
    PRESS = "press"; HOVER = "hover"; UPLOAD = "upload"; SCROLL = "scroll"

@dataclass(frozen=True)
class Target:
    """How to find one control. Role and name are the address, not a hint.

    This is a Playwright locator spec, a Stagehand ObserveResult and an a11y
    tree node all at once, because all three agree on role + accessible name.
    Resolving through it is tier 0 (§2.3): `get_by_role(role, name=name)`,
    deterministic, auto-waiting, and strict — an ambiguous match raises rather
    than picking the first, which is the whole reason to prefer it.
    """
    role: str                       # "button", "textbox", "combobox"
    name: str = ""                  # accessible name — the visible label
    ordinal: int = 0                # nth match, when a page repeats a control
    native: str = ""                # provider-native CSS/XPath ESCAPE HATCH ONLY

    @property
    def fingerprint(self) -> str:
        """What §2.8 measures drift against. Deliberately excludes `native`:
        a CSS path changing under a stable labelled button is not drift, and
        treating it as drift would make every redeploy of a site re-approve."""
        return stable_hash({"role": self.role, "name": self.name,
                            "ordinal": self.ordinal})

@dataclass(frozen=True)
class ActionPlan:
    """One resolved, replayable action. The unit the journal caches."""
    method: ActionMethod
    target: Target
    arguments: dict[str, Any] = field(default_factory=dict)
    description: str = ""           # what a person reading a trace needs
    tier: int = 0                   # which tier resolved it — recorded, not guessed

@dataclass(frozen=True)
class PageSnapshot:
    url: str
    title: str
    tree: list[TreeNode]            # a11y tree: role, name, value, ref
    screenshot: Attachment | None = None   # only when vision was asked for
    trace: Attachment | None = None        # playwright trace.zip, on failure

@dataclass(frozen=True)
class SessionHandle:
    """What a journal records about a session. Never the session itself."""
    session_id: str
    provider: str
    reattachable: bool = False
    storage_ref: str = ""           # blob ref for storage_state; never inline

class BrowserProvider(Protocol):
    id: str
    def supports(self) -> frozenset[str]:
        """Honoured capabilities: 'reattach', 'live_view', 'vision',
        'storage_state', 'captcha', 'proxy', 'stealth'. A scope asking for one
        that is absent is refused — `ExecutionSandbox.enforces`, one layer out."""
    async def open(self, policy: BrowserPolicy) -> BrowserSession: ...
    async def reattach(self, handle: SessionHandle) -> BrowserSession: ...

class BrowserSession(Protocol):
    handle: SessionHandle
    async def navigate(self, url: str, *, wait: str = "load") -> PageSnapshot: ...
    async def snapshot(self, *, vision: bool = False) -> PageSnapshot: ...
    async def locate(self, target: Target) -> Located | None:
        """Tier 0. No model, ever. `None` when nothing matches; raises
        `AmbiguousTarget` when several do."""
    async def observe(self, intent: str, *, snapshot: PageSnapshot) -> list[ActionPlan]:
        """Tier 1/2. Reached only when `locate` returned None or raised."""
    async def perform(self, plan: ActionPlan) -> ActResult: ...
    async def extract(self, instruction: str, schema: dict[str, Any]) -> Any: ...
    async def storage_state(self) -> bytes: ...
    async def live_view_url(self) -> str | None: ...
    async def close(self) -> None: ...
```

**`locate` and `observe` are two methods, not one, and the split is the tiering
made structural.** A single `resolve(intent)` would let a provider quietly spend
a model call on a control `get_by_role` would have found for nothing — and
nothing in the result would say it had. Same reasoning as `Probe.supports` /
`Probe.observe`: a capability that can decline without being asked to guess.
`ActionPlan.tier` records which one answered, so the metric in §1 is read off the
journal rather than inferred.

`observe` lives on the **session** rather than on a planner LOOM owns, because a
provider that resolves server-side (Stagehand's CDP-native path, Browserbase's)
must be able to. `LocalBrowserProvider.observe` is the tier-1 implementation
over `Runtime.agent_backend`'s `ModelProvider`: a11y tree in, `ActionPlan` out,
no pixels unless asked.

### 3.1b Reliability engineering — the part that decides whether this works

Browser automation fails in ways HTTP does not, and each one needs an answer in
the design rather than in a runbook.

| Failure | Answer |
|---|---|
| Racing a page that is still painting | Playwright auto-waiting only. LOOM adds **no polling of its own** — and reuses `BrowserProbe`'s hard-won settle sequence verbatim (`domcontentloaded` → `networkidle` under `suppress` → fixed settle), which already encodes that `networkidle` never fires on some sites |
| Ambiguous control | Tier 0 is **strict**: two matches raise `AmbiguousTarget`, which escalates to tier 1 with the candidates attached. Never "first match wins" |
| A retried `act` clicks twice | `Retry(max_attempts=1)` for any act not declared READ — the `io.http_request` rule verbatim: "a timeout after the server acted is indistinguishable from a failure" |
| `STEP` scope re-runs a completed booking | The cookbook shows **check-before-act**: observe for the confirmation state first, return early if present. A generated flow without one trips `browser-effect` |
| Debugging a failure nobody saw | On any act failure the entry carries a **screenshot, the a11y tree, and Playwright's `trace.zip`** as `Attachment`s. Blobs make this cheap; a trace is the difference between a bug report and a shrug |
| Bot detection | Not LOOM's. `patchright` is a drop-in Apache-2.0 swap (`BrowserPolicy.engine="patchright"`), and stealth beyond that is a provider capability. Shipping evasion is a treadmill and a posture this project should not take |
| A volatile a11y tree defeating fingerprints | `TreeNode` normalisation strips generated ids, timestamps and cache-busting query strings before hashing. Asserted by a test with a page that regenerates ids per load |

**One wall clock governs everything.** `BrowserPolicy.max_wall_seconds` bounds
the session; `ctx.node(..., timeout=)` bounds an action. Both already exist as
concepts; the session-level one is what stops a hung page from holding a
provider slot until the lease expires.

### 3.2 `src/loom/nodes/browser/nodes.py`

Six nodes, new `NodeCategory.BROWSER`. The category costs one line in the
O(categories) prompt block and buys the property that makes the block work at
all — "which node do I want *is* a category." A browser act is not `io` because
it needs a **session**, and the statefulness is the whole difference.

```python
class ActIn(BaseModel):
    plan: ActionPlan | None = None
    intent: str = ""                    # when no plan: observe, then act
    value: str = ""
    connection: str = ""                # resolved OUTSIDE the journaled call
    effect: EffectClass = EffectClass.WRITE
    """What this action does to the world. **Declare it.**

    WRITE is a fail-safe backstop, not a classification — the same position
    OperationSpec.effect takes. It cannot be inferred: `click "Next"` and
    `click "Confirm booking"` are the same shape, and a keyword list over the
    intent is the guess DEFAULT_SYSTEM_PROMPT names as the tell for a rule
    nobody should write."""
    if_drifted: DriftPolicy = DriftPolicy.AUTO
    """AUTO: re-observe and repair for a READ; raise for a WRITE. See §2.8."""
```

`spec.effect_by = {"effect": {...identity...}}` — the argument-dependent hook
`io.http_request` already uses for `method`, so the broker reads the declared
class off the call without a browser-shaped special case anywhere in
`runtime/effects.py`.

`browser.session` is the scope node and the one that suspends:

```python
class SessionIn(BaseModel):
    scope: SessionScope = SessionScope.STEP
    start_url: str = ""
    connection: str = ""
    reuse_storage: str = ""    # artifact name holding a prior storage_state
```

### 3.3 Credentials and cookies

Verbatim reuse of `io.http_request::_with_credential`, whose reasoning transfers
unchanged: the field is `connection` and not `credential` because it holds an
**id**, and a field named `credential` is redacted out of the journal by the
name denylist — erasing the one part worth recording.

`storage_state` is a live cookie jar and therefore a credential. It goes to
**blobs**, referenced by `storage_ref`, never inline. Reused across runs via the
artifact API (`ctx.put_artifact` / `ctx.get_artifact`), which already gives
immutability, versioning and retention.

`DEFAULT_REDACT_KEYS` gains `storage_state`, `cookie`, `cookies`, and the
existing whole-word rule handles them correctly without a special case:
`storage_state` is multi-word so it matches a consecutive run anywhere, while
single-word `cookie` must be the last word — catching `session_cookie` and
leaving `cookie_banner_text` alone. `tests/test_redaction.py` gains a case for
each.

### 3.4 Smoke and fakes

A generated browser workflow must run in the smoke subprocess with no network,
or the only reachable outcome is a connection error — which "proves nothing
about the code and tempts a repair loop into deleting the integration to make
the error go away."

`FakeBrowserProvider` serves recorded `PageSnapshot`s. **The recordings are what
the authoring probe already captured.** `BrowserProbe.observe` returns an
`Observation` carrying a census and a full-page screenshot as an `Attachment`;
extended once to also carry the a11y tree, it *is* the fixture. The page the
agent looked at while writing the code is the page the code is tested against —
no parallel fixture set, and nothing to keep in sync. Same discipline as
toolset fakes derived from `output_schema`.

`act` against the fake asserts the plan resolves in the recorded tree and
returns success. It cannot prove the click works; it proves the flow is
addressed at controls that exist, which is exactly the failure `BrowserProbe`
was built for.

### 3.5 Authoring

`BrowserProbe` is **unchanged**. Read-only stays structural: it is handed to a
model, so "please do not write" is not a control, and `verify_probe`'s
`methods_seen` check stays the proof.

The agent writes **intents, not selectors** — `DEFAULT_SYSTEM_PROMPT` gains a
short block saying so, and saying why: a selector read off a page during
authoring is right on the render you saw and silently wrong later, and it fails
by matching nothing rather than by erroring.

Two new stages, both non-blocking **errors** (the repair loop reads
`report.errors`; a warning there is a finding nobody sees, and unchanged code
ends the repair — so escalation is safe):

| Stage | Cost | Finds |
|---|---|---|
| `selectors` | 18 | a CSS/XPath literal in generated code — the `IdentifierStage` analogue: an identifier nothing looked up, evidenced from the agent's own tool calls |
| `browser-effect` | 19 | an `act` left at the default `effect`, or a WRITE act on a tainted path with no `wait_for_approval` before it |

### 3.6 Compensation

Browser flows are where sagas earn their keep, because the effect is a
reservation somebody holds. A booked table with a failed run afterwards is the
motivating case:

```python
booking = await ctx.node("browser.act", {"intent": "confirm", "effect": "write"})
await ctx.compensate(cancel_booking, booking.confirmation_id)
```

Nothing new — `ctx.compensate` unwinds LIFO on failure and cancellation already.
The cookbook example must show it, because a generated flow that books without
one is the failure mode a reader will copy.

---

## 4. Files

**New**

```
src/loom/browser/{__init__,base,errors,local,fake,resolve,sessions,cache,registry}.py
src/loom/nodes/browser/{__init__,nodes}.py
tests/test_browser_nodes.py                 # nodes, lifecycle, journal shape
tests/test_browser_effects.py               # 13.2 — declared effects, taint, tier 1, drift
tests/test_browser_harness.py               # §7.2 — the kit driven by 7 mutant providers
tests/test_browser_integration.py           # §7.1 — the acid test, one run
tests/test_browser_local.py                 # the shipped provider, against the corpus
tests/test_browser_authoring.py             # 13.4 — the two stages, both directions
tests/test_browser_corpus.py                # §7.3 — tier-0 rate, per page and floor
tests/test_dependency_licences.py           # E6 — licence + specifier gate
tests/test_effect_arguments.py              # the effect_by regression
tests/corpus/{pages,README.md,targets.json,outcomes.json}
examples/cookbook/30_browser_automation.py
examples/cookbook/31_browser_approval.py
docs/guides/browser-automation.md    # snippets execute in CI
```

**Changed**

| File | Change |
|---|---|
| `runtime/engine.py` | `Runtime(browser=…)`, default `None` |
| `nodes/base.py` | `_CAPABILITY_ATTRS["browser"] = "browser"` |
| `nodes/spec.py` | `NodeCategory.BROWSER` + one `CATEGORY_BLURBS` line |
| `agents/probes/browser.py` | census also returns the a11y tree (the smoke fixture) |
| `agents/coding_agent.py` | intents-not-selectors block in the prompt |
| `agents/stages.py` | `SelectorStage`, `BrowserEffectStage` |
| `agents/smoke.py` | install `FakeBrowserProvider` alongside toolset fakes |
| `core/redaction.py` | `storage_state`, `cookie`, `cookies` |
| `testing/conformance.py` | `verify_browser_session` |
| `pyproject.toml` | `[browser]` gains nothing; new `[stealth] = ["patchright"]` (Apache-2.0) |
| `.github/workflows/ci.yml` | a `browser` job — see below |

**A CI job, because otherwise none of this runs there.** `test` installs
`[dev]`, under which every test that drives a page calls
`importorskip("playwright")` and disappears — *silently*, so the suite goes
green having run neither `verify_browser_session` against the shipped provider
nor the tier-0 corpus, which are the two things that say the layer works. The
`browser` job installs `[dev,browser]` plus `playwright install --with-deps
chromium`, runs with `-rs` so a skip reads differently from a pass, and prints
the corpus rate: that number is a design gate, not a metric nobody reads.

Note the second-to-last row. The extra the read-only probe already needed is the
extra the runtime needs — **this phase adds no new dependency to LOOM's core,
and `[all]` gains one Apache-2.0 package.** `browser/registry.py` is the
`loom_browser_provider` entry point loader, ~40 lines copied in shape from
`probes/registry.py`; it is how Stagehand and browser-use adapters arrive
without LOOM importing either.

**Adapters LOOM does not ship**, documented in the guide with the conformance
call that proves them:

```python
# loom-browser-stagehand — a separate package, MIT deps, host-installed
class StagehandProvider:
    id = "stagehand"
    def supports(self): return frozenset({"reattach", "live_view", "vision"})
    ...
await verify_browser_session(StagehandProvider, target=fixture_url)
```

---

## 5. Implementation Steps

**13.1 — Ports, reads, and tier 0. — BUILT, 2026-08-19.**

`src/loom/browser/` (base, errors, resolve, local, fake, sessions, registry),
`src/loom/nodes/browser/`, `verify_browser_session`, `Runtime(browser=…)`,
`NodeCategory.BROWSER`, two new seam pages, `examples/cookbook/30_browser_automation.py`.
56 tests across five files, green on three consecutive runs.

Two defects the discipline caught rather than the code avoiding:
`verify_browser_session` found `LocalBrowserSession.storage_state()` leaking
Playwright's `TargetClosedError` instead of `SessionLost` on a closed session —
on its first run against the provider LOOM ships. And the mutant harness found
its own ordering wrong: "does `locate` ever return zero?" ran *after* the
target-specific checks, so a locator matching everything passed them for the
wrong reason.

**13.1 — as designed.** `base.py`, `errors.py`,
`LocalBrowserProvider` over Playwright, `registry.py`, `verify_browser_session`.
Nodes `navigate`, `snapshot`, `extract`, and `act` **restricted to a caller-
supplied `Target`** — tier 0 only, no model anywhere in the package. All READ,
`SessionScope.STEP` only. *Exit:* **E1, E6, E7, E10** — journals match
hand-written code, the dependency gate holds, the kit rejects all five mutants,
and the corpus reports a tier-0 rate against the §1.3 decision table.

This ordering is deliberate: **the deterministic engine ships and is proven
before any model touches it.** If 13.1 is not solid, nothing above it can be,
and the tier-0 hit rate measured here is what says whether tier 1 is a fallback
or the actual product.

**13.2 — Tier 1 and the safety surface. — BUILT, 2026-08-19.**

`ActIn.effect` declared with `effect_by`, `browser.observe` (tier 0 first, tier 1
on ambiguity), `DriftPolicy`, drift repair on `act`. 19 tests in
`tests/test_browser_effects.py`, including the full taint matrix — two dials
× three effect classes — with nothing performed on a refusal.

**Two shipped bugs surfaced, both outside the browser package**, and the second
was hidden by the first:

- `_effect_arguments` returned `{}` for anything that was not a `dict`, and
  `ctx.node` passes the validated **Pydantic model**. So `effect_by` was dead
  for *every* node: `io.http_request(method="DELETE")` reached the broker
  classified WRITE, and a deployment on the narrow taint dial
  (`block_writes=False, block_destructive=True`) permitted exactly the calls it
  was configured to refuse. Its one shipped user's table was dead code from the
  day it was written.
- An inline `ctx.call` carried no classification, so a node's own I/O reached
  the broker as an unclassified WRITE — `io.http_request(method="GET")`
  dispatched the node as READ and its inner `http:GET` as WRITE. The node's
  careful classification was defeated one level down by itself. `call` now
  inherits the enclosing node's resolved class and target; `step` deliberately
  does not, since it names a function that may carry its own class from a
  manifest.

**One limitation is now asserted rather than latent.** `ctx.wait_for_approval`
is what clears a taint, and it **parks the run** — which under
`SessionScope.STEP` ends the browser session. So the approval clears the taint
and the next browser call finds no browser. Both mechanisms are behaving
correctly; they simply do not compose until 13.3. Under `block_writes=True` a
browser write is therefore unreachable, and the usable configuration today is
the narrow dial. `TestApprovalNeedsADurableSession` pins this so it is visible
in the suite rather than in somebody's production run.

**`PlanCache` — built, and the objection that deferred it was answered rather
than overruled.** Caching wanted `ctx.state`, which `NodeContext` excludes
because state is *semantic*: a workflow branches on it, and a node writing to it
can change a run nobody reviewed. A plan cache is none of those things —
deleting it changes no outcome, nothing branches on it, and **a hit is verified
against the live page before it is used**, so a stale entry costs a wasted
lookup rather than a wrong click. It also needed no new capability: `Runtime.cache`
already exists and defaults to the store.

Keyed on the page *shape* (scheme, host, path — never the query), so two rows of
one form share a plan instead of filling the cache with an entry per record, and
scoped per workflow, because the same words can mean different controls to
different workflows. A stale entry is replaced rather than dropped, so the run
that finds it also fixes it.

**13.2 — as designed.** `observe`, `ActionPlan.tier`,
`PlanCache`, declared act effects, drift policy. *Exit:* **E2 and E3** — a
replayed flow is inference-free and click-free, and a WRITE act after a page
read is refused without an approval.

**13.3 — Durable sessions. — BUILT, 2026-08-19.**

`SessionScope.DURABLE`, `SessionRef` threaded through the workflow as an
ordinary journaled value, `BrowserSessions.attach`, release-without-closing at
body exit, `browser.close`, and `HumanRequest.live_view_url`. The §7.1 acid test
runs end to end: navigate durable → tier 0 fills → **taint refuses the submit**
→ park on a person **holding a live-view URL** → answered over the host's
channel → **reattach the same session** → submit → replay with no clicks, no
model calls, no new session, and an identical journal.

**A deadlock in the shipped taint rule, found by the acid test.** Every
`human.*` node is `WRITE` and `open_world` — both accurate — so a tainted run
could not reach the person whose approval was *the only thing that clears the
taint*. `approval_clears` is documented as "the escape hatch that keeps the rule
usable"; the rule was blocking its own exit. `EffectCall.asks_human` now carries
it, derived structurally from `requires=["human_channel"]` rather than a name
match, so a project's own approval node is covered too.

It had two halves and fixing one was indistinguishable from fixing neither: the
node call *and* the `deliver:` call inside it, which inherits the node's
classification. `TestAskingAPersonIsNeverRefused` asserts both, and asserts that
exempting the ask did not exempt the acting.

**Why `SessionRef` is a value the workflow carries rather than Runtime state.**
A resumed run has to learn which browser it was using, and the journal is
already the mechanism for that — the ref comes back from `browser.navigate`, so
on re-entry it is served from the journal like any other recorded value, with no
side table in the engine. It also makes the dependency visible: code that acts
on a session it did not navigate reads as wrong, because it is.

**A durable session is released at body exit, never closed.** That is the whole
scope: a run parked two hours on a person must still have its browser. Its
lifetime belongs to the provider — every hosted vendor expires sessions on a TTL
— and `browser.close` is how a workflow ends one deliberately. A `STEP` session
is still closed however the body exits.

**13.3 — as designed.** `SessionHandle`, `reattach`, `supports`,
`SessionLost`, `live_view_url` on `HumanRequest`. *Exit:* **E4 and the §7.1
acid test end to end** — including the reattach-opens-a-fresh-session mutant,
which is the failure that looks like success.

**13.4 — Authoring. — BUILT, 2026-08-20.**

`SelectorStage`, `BrowserEffectStage`, the a11y tree in `BrowserProbe`,
`FakeBrowserProvider` in the smoke sandbox, a browser block in
`DEFAULT_SYSTEM_PROMPT`, and `docs/guides/browser-automation.md` whose every
snippet compiles and resolves in CI. 41 tests, each stage asserted **in both
directions**.

**The second direction is the one that mattered.** A stage that fires on
correct code is worse than no stage, because the repair loop acts on
`report.errors` and will rewrite working code to silence it — the reasoning the
redaction denylist already follows: "a denylist that redacts ordinary data is
one people switch off". `SelectorStage`'s patterns are pinned against ten
ordinary strings (`"Party size"`, `"#urgent #billing"`, `"Mr. Smith and Ms.
Jones"`, `"name > 5"`) as well as seven real selectors.

Writing them caught two of my own defects. The first combinator pattern
required the `>` to follow a *bare* tag, so `div.card > button.primary` — the
single most ordinary selector there is — went unreported. And the whitespace
descendant combinator is genuinely ambiguous with prose, so it is keyed on a
`.class` sigil: `#a #b` is two hashtags and stays silent, `#main .row` does not.

**The probe's census now carries role and accessible name** in the same shape
`PageSnapshot.tree` uses, so an authoring observation is directly usable as a
smoke fixture — the page the agent *looked at* is the page its code is tested
against, with no parallel fixture set to drift. Its summary gained the line that
decides whether the approach works at all: `15/18 addressable by name`, and an
explicit sentence when nothing is.

**Smoke installs `FakeBrowserProvider(permissive=True)`**, for the reason
`AutoRespondChannel` exists: without it a generated browser workflow can only
reach a connection error, and the cheapest repair a model can find for an error
it cannot fix is to delete the browser work. Both halves of E9 are asserted —
the flow runs offline *and* the same flow fails with no provider, because a
smoke pass proves nothing if the code would have passed anyway.

**The prompt block cost 2235 characters and shipped at 681**, because
`test_the_prompt_stays_lean` caught it. That test holds `DEFAULT_SYSTEM_PROMPT`
to a budget whose margin is "about one sentence wide on purpose, so the next
addition has to run this search too" — and the search worked exactly as
designed. Two cuts came out of it. The opening line said to use a browser only
when nothing else covers the service, which is step 1's DISCOVER exit said
twice; merged into that step, where it has a subject — and step 1 was *wrong*
without it, naming plain Python as the only answer when no toolset matches. And
the worked example went entirely: `node_contract` renders the call from the
node's own models on demand, so an example in the prompt is a second copy that
can drift from the contract while the tool cannot.

What survived is what nothing else says, including the one thing no tool can
tell the agent: that an approval parks the run, so a browser flow containing one
has to be opened durable or the session is gone when the person answers. The
budget was then raised 10400 → 11100 and the reasoning written into the
docstring, which is what every previous raise did.

**A gate I did not run.** `python scripts/typecheck.py` is blocking CI, and I
shipped 13.1–13.4 having run ruff and pytest but never it — 16 mypy errors, all
in this phase's code, found by someone else reading the tree. Two were real
rather than cosmetic: `browser.close` had lost its `return` to a bulk edit and
would have returned `None` for a declared `CloseOut`, and
`BrowserEffectStage` passed an `ast.AST` where a `Call` was required. Both were
invisible to the test suite. The lesson is not "run mypy" — it is that
"the tests pass" was doing work in my head that it cannot do, and the repo
already said so: the typecheck job is blocking *because* `mypy || true` once let
42 real errors merge.

**13.4 — as designed.** a11y tree in the probe, `FakeBrowserProvider`, the two
stages, prompt block, cookbook, guide. *Exit:* **E8 and E9** — `loom author` on
a browser spec produces code that smoke-runs offline, fails *without* the fakes,
and trips both stages on the defect while staying silent on correct code.

---

## 6. Multi-Angle Review

**"Why not record-and-compile, like workflow-use and Skyvern?"** Because the
recording pass performs the effects. There is no rehearsal of a booking: you
learn what the submit does by submitting. Worse, a compiled trajectory is a
snapshot presented as a contract, and the failure mode is the one this codebase
keeps naming — it runs, completes, and is wrong. Note that neither product
actually solved drift by improving the recording; both solved it by keeping a
model in the loop as the recovery path. **The compiled artifact is a cache, not
a contract.** LOOM's version of that cache is §2.5, and it is warm after run 1
with no recording ceremony and no separate artifact to invalidate.
`loom browser record` remains available as a later warm-start for the plan
cache. It is not the foundation.

**"Why not put the browser in the sandbox?"** `--network none` and a read-only
root are the sandbox's whole value. Punching a hole for a browser would trade
the isolation property for the thing the parent can do anyway. §2.9.

**"`SessionScope.STEP` re-runs effects on retry — isn't that the bug the journal
exists to prevent?"** It is the same exposure every `@step` has, made visible
rather than hidden. The alternative — replaying a recorded trajectory to rebuild
browser state — re-performs the effects *silently*, which is strictly worse. A
flow whose submit must not repeat gets an idempotency key or a compensation
handler, and `browser-effect` is where the agent is told so.

**"Declaring `effect` on every act is friction."** It is one field, and it is
the field that makes taint, grants, dry-run and the drift rule work. The
alternative is inferring it from an English string, which is the
under-classification trap Phase 12 documented — there fail-open on 14% of
operations including seven destructive ones. Here the seven are card charges.

**"A new node category for six nodes?"** One line in an O(categories) block.
The alternative is `io.browser_act`, which puts a session-scoped stateful node
in a category whose blurb is "typed external effects" and whose other members
are stateless. Discoverability is the category axis's only job.

**"Why not just depend on browser-use? It is MIT and has 110k stars."**
Because a dependency is not a library you like, it is a constraint you impose on
every host. `browser-use` pins ~40 packages with `==` — `pydantic==2.12.5`,
`httpx==0.28.1`, plus `openai`, `anthropic`, `google-genai`, `groq`, `ollama`
and `posthog` — against LOOM's own two ranged dependencies. Taking it would
mean an SDK that dictates a host's pydantic patch version and installs four LLM
vendor SDKs to click a button. Through the port it is excellent and costs
nothing, which is where it belongs. The same reasoning admits `stagehand-python`
(six ranged deps) as an adapter while declining it as the default, on maturity:
514 stars and a month stale against a TypeScript sibling shipping daily.

**"Isn't tier 0 naive? Real pages don't have clean accessible names."**
Measured rather than argued, which was the point of declaring the bands first:
**76% across 114 controls on 10 real pages**, floor 47%. Tiers 1 and 2 exist for
the rest. Two things the measurement changed that argument would not have:
tier 0 gained the placeholder/label accessors (§2.3), worth 26 points on two
pages; and the corpus's own control fixture caught a capture defect that was
suppressing every custom radio, checkbox and switch on every page at once — a
47% score for Wikipedia, which is not a statement anyone should believe about
Wikipedia. Both were found before a line of the resolver was written.

**"Isn't 76% on 10 pages too thin to decide anything?"** For the decision it is
asked to make, no — the §1.3 table separates ≥70% from 40–70%, and the result is
not near that boundary on the metric the table names. For the *number*, yes, and
the doc says so where the number appears. What would change the decision is the
per-page floor moving, not the mean: `resy` at 47% and `zendesk` at 50% are both
list-and-duplicate pages where role+name is structurally insufficient, and if
that class turns out to dominate real workflows, tier 1 carries more than this
design assumes.

**"Does this make LOOM a browser-agent product?"** No — and the position is the
same one taken about vector stores and event brokers. LOOM ships the port, the
durability, the effect classification, the human seam, and one reference
implementation. Browserbase and Kernel remain host adapters. What LOOM adds that
none of them has is the journal underneath: a booking flow that survives a
deploy, replays without re-clicking, refuses to submit after reading the open
web without a person, and unwinds a confirmed reservation when a later step
fails.

---

## 7. Verification

Five layers, weakest evidence last. Each answers a different question, and none
of them substitutes for another.

### 7.1 The acid test — one run, end to end

`tests/test_browser_integration.py`, modelled on `tests/test_host_integration.py`
("the phase's acid test rather than a demo"). **One** run must do all of this,
because the value is in the links between the parts, not the parts:

```
navigate a frozen multi-step form
  → tier 0 resolves every field with no model call
  → fill, across three pages
  → TaintBroker REFUSES the submit                    (read → write, no approval)
  → run parks on a human; HumanRequest carries live_view_url
  → answered over the host's own channel
  → resumes on the SAME session                       (DURABLE, reattached)
  → submits
  → process killed mid-flow, reclaimed by another node
  → replays: zero clicks, zero inference
  → journal identical, entry for entry, to the inline run
```

A file-level grep completes it, as the host test does for `runtime._…`: **no
test in this suite may reach a live network**. A browser suite that quietly
starts depending on the internet is one that goes red for somebody else's
outage and gets muted.

### 7.2 Conformance, and the harness checked against itself

`verify_browser_session` runs against `LocalBrowserProvider` and
`FakeBrowserProvider`. That proves nothing on its own — both are correct on day
one. So `tests/test_browser_harness.py` drives **deliberately-broken providers**
through the same kit and asserts each is caught, the discipline
`tests/conformance/test_harness.py` and `tests/test_effect_gates_can_fail.py`
already establish:

| Mutant | Must be caught by |
|---|---|
| `locate` returns the first of several matches | the strictness check |
| `locate` matches a control that cannot exist | the impossible-target probe |
| `snapshot` navigates while reading | position stability |
| `close()` leaves the session usable | lifecycle |
| `reattach` silently opens a *fresh* session | handle identity — the worst one, because the flow appears to work and has lost its state |
| `supports()` claims `reattach` it does not honour | declaration honesty |
| `reattach` works but `supports()` omits it | the same, in the other direction |
| a provider with no id, or `supports()` returning a list | shape |

Eight rather than the five first sketched, and the ordering of the checks
turned out to matter as much as their number: the impossible-target probe ran
*after* the caller's own targets, so a locator matching everything passed those
for the wrong reason. Moved first, because "does `locate` ever return zero?" is
the cheapest question here and the one that invalidates the rest.

The fourth is the reason this layer exists. A provider that silently reopens
turns a two-hour approval into a form filled twice, and every happy-path test
still passes.

### 7.3 The corpus — how E10 is actually measured

**Built. `tests/corpus/` (method in its README), `tests/test_browser_corpus.py`
(six gates), `scripts/capture_corpus.py` + `capture_batch.py` (capture).**
9.4 MB, 10 pages, offline, deterministic.

The hardest part, and the one most likely to be fudged. Rules:

**Frozen, committed, served locally.** `tests/corpus/pages/` holds N real
transactional pages captured as self-contained snapshots and served by a local
HTTP server. This is not a preference — it is `verify_probe`'s own rule: *"point
it at a fixture you control, not at a third party's site, so a red test means
your probe broke rather than someone else's server did."*

**Chosen before the resolver is written, and by a rule, not by taste.** Fixtures
picked after the fact select for what already works. The rule: the top booking,
checkout, signup and support-form pages by traffic across the categories LOOM's
reference workflows target, capped at two per vendor so one framework cannot
dominate the score. Committed with the capture date and the URL.

**Labelled by hand, once.** Each page ships a manifest naming its interactive
controls and their intended role+name. The hit rate is `tier 0 resolved / labelled
controls` — not "did the flow succeed", which conflates resolution with everything
else and is the metric that flatters.

**Reported per page, never only as a mean.** One framework resolving at 100% and
another at 10% averages to a healthy-looking 55% and hides that a whole class of
site is unreachable. CI prints the table; the gate is on the *floor* as well as
the mean.

Known limit, stated rather than discovered: **a frozen page cannot test dynamic
behaviour** — a date picker that fetches slots, a field that appears after
another is filled. §7.4 is the only thing that covers those, and it is
deliberately not a merge gate.

### 7.4 Live checks — nightly, non-blocking, named when skipped

A small set against real sites, on a schedule, **never in PR CI**. They exist to
catch what a frozen page cannot: real latency, real client rendering, real
anti-bot. They may go red without blocking a merge, and the rule is the one the
store matrix already follows — *"a backend that cannot be reached is SKIPPED and
named, never dropped."* A suite that quietly shrinks reports green for coverage
it did not have.

### 7.5 Adversarial

| Scenario | Expected |
|---|---|
| Page changes between the approval and the submit | `SelectorDrift` raised, approval invalidated, **not** silently re-resolved |
| Tree regenerates element ids per load | fingerprints stable — normalisation strips them |
| Session dies mid-flow under `DURABLE` | `SessionLost`, typed, and still typed after a replay |
| `STEP` scope retried after a completed submit | check-before-act returns early; no second booking |
| Provider returns a plan for a control that no longer exists | act fails with a screenshot, tree and `trace.zip` attached |
| Two runs share a provider concurrently | session isolation; neither sees the other's cookies |

### 7.6 What none of this proves

That a workflow books the right table. Every layer above tests **mechanism**.
Whether the flow does what the spec meant is the coding agent's `OutcomeStage`
and a person reading the result — and on a real booking, the first execution is
the first test, which §9 states and this phase does not remove.

## 8. Rollback

Every piece is opt-in and absent by default. `Runtime(browser=None)` is the
current runtime exactly. The `browser.*` nodes are unresolvable without a
provider and report it as a missing requirement before any body runs. The two
stages are registration, not surgery — dropping them from the pipeline restores
the prior arrangement. `BrowserProbe` is unchanged throughout, so authoring
degrades to what ships today.

---

## 9. Known limits, stated rather than discovered

- **No rehearsal of a terminal effect.** Nothing here makes a booking dry-runnable.
  `AutoRespondChannel`'s reasoning applies: the smoke sandbox must not turn a flow
  containing an approval into one whose cheapest repair is deleting the approval.
- **CAPTCHA and bot detection are the provider's.** LOOM declares `captcha` as a
  `supports` capability and does nothing about it. Shipping evasion is both a
  maintenance treadmill and a posture this project should not take.
- **`extract` is a model call and prices like one.** `JudgementStage` applies
  unchanged: a workflow whose *answer* comes out of `extract` when the spec asked
  for data is the mistake it already reports.
- **The a11y tree is not universal.** Canvas apps, `<video>`, and untagged custom
  widgets are invisible to it. `vision=True` is the escalation, and the same
  honesty `BrowserProbe._summarise` already shows — "0 native, 4 role-based" —
  belongs on a snapshot that found nothing addressable.
