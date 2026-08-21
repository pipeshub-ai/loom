"""``browser.*`` — driving a web page as journaled, catalogued work.

Four nodes in this phase, all **READ** and all ``open_world=True``: they reach
outside the deployment, so a run that uses them is tainted and a later write
needs a person. That falls out of the declarations rather than being enforced
here — ``TaintBroker`` keys on ``EffectClass`` and ``open_world``, and needs no
browser-specific line.

``browser.act`` takes a caller-supplied :class:`~loom.browser.base.Target`.
There is deliberately **no natural-language intent in this phase**: resolving
one needs a model, and 13.1 ships the deterministic half first so it can be
proven on its own. ``tests/corpus`` says tier 0 addresses 76% of real controls,
which is what makes shipping this half alone worth doing.

Everything durable goes through :class:`Context`, so ``ctx.node("browser.act")``
journals exactly what the equivalent hand-written ``ctx.step`` would — asserted
by ``tests/test_browser_nodes.py``, because "a node adds no durability
semantics" is the claim the whole node system rests on.
"""

from __future__ import annotations

import contextlib

from pydantic import BaseModel, Field

from loom.blobs.attachment import Attachment
from loom.browser.base import (
    ActionMethod,
    ActionPlan,
    BrowserPolicy,
    BrowserSession,
    DriftPolicy,
    PageSnapshot,
    SessionScope,
    Target,
    TreeNode,
)
from loom.browser.cache import PlanCache
from loom.browser.errors import (
    AmbiguousTarget,
    SelectorDrift,
    TargetNotFound,
)
from loom.browser.sessions import BrowserSessions
from loom.core.exceptions import ConfigurationError
from loom.core.retry import Retry
from loom.nodes.base import Node, NodeContext
from loom.nodes.registry import register_node
from loom.nodes.spec import (
    EffectClass,
    NodeCategory,
    NodeExample,
    NodeSpec,
)

__all__ = [
    "BrowserActNode",
    "BrowserCloseNode",
    "BrowserExtractNode",
    "BrowserNavigateNode",
    "BrowserObserveNode",
    "BrowserSnapshotNode",
]


class TargetIn(BaseModel):
    """A control, addressed the way a person describes it."""

    role: str = Field(
        description="ARIA role: button, textbox, combobox, checkbox, radio, link, "
                    "searchbox, switch, tab.")
    name: str = Field(
        default="",
        description="The label a person reads on screen. Matched against the "
                    "accessible name, then placeholder, then label — a page "
                    "often names a control differently from the text it shows.")
    ordinal: int = Field(
        default=0,
        description="Which match, 1-based, when a page legitimately repeats a "
                    "control. 0 means there must be exactly one — several "
                    "matches raise rather than guessing.")
    exact: bool = Field(default=False, description="Match the whole name.")
    css: str = Field(
        default="",
        description="Escape hatch, tried last. A selector is right on the "
                    "render it was read from and silently wrong later, so "
                    "prefer role and name.")

    def to_target(self) -> Target:
        return Target(role=self.role, name=self.name, ordinal=self.ordinal,
                      exact=self.exact, css=self.css)


class SessionRef(BaseModel):
    """A durable session, as a value a workflow can carry.

    Threaded through the workflow rather than hidden in the Runtime, and that
    is the design rather than an inconvenience. It comes back from
    ``browser.navigate``, so on re-entry it is served **from the journal** like
    any other recorded value — which is exactly how a resumed run learns which
    browser it was using without the engine keeping a side table.

    It also makes the dependency visible: code that acts on a session it did
    not navigate reads as wrong, because it is.
    """

    session_id: str
    provider: str
    reattachable: bool = False
    live_view_url: str = Field(
        default="",
        description="Where a person can watch or take over this browser, when "
                    "the provider offers one. Put it in front of whoever is "
                    "being asked to finish a 2FA prompt.")


class ControlOut(BaseModel):
    role: str
    name: str = ""
    value: str = ""
    disabled: bool = False


class PageOut(BaseModel):
    """What a page looks like to tier 0."""

    url: str
    title: str = ""
    controls: list[ControlOut] = Field(default_factory=list)
    session: SessionRef | None = Field(
        default=None,
        description="The browser this page is open in. Pass it to later "
                    "browser.* calls so a run that resumes after an "
                    "interruption reattaches instead of refusing.")
    text: str = ""
    summary: str = Field(
        default="",
        description="One line. Says when nothing on the page carries an "
                    "accessible name, which is the reason a target will not "
                    "resolve rather than a description of the page.")
    screenshot: Attachment | None = Field(
        default=None,
        description="The page as an image, when `vision` was asked for. None "
                    "otherwise — pixels are expensive to journal and tier 0 "
                    "never reads them.")
    """Evidence, for a person or a run trace rather than for the resolver.

    The provider has always captured this under `vision`; until now `_page_out`
    dropped it on the way out, so a caller paid for the pixels and received
    nothing. An `Attachment` rather than a path: it journals losslessly, and
    with `Runtime(blobs=…)` it offloads by content hash instead of putting a
    quarter-megabyte of PNG in a journal row.
    """


async def _session_ref(session: BrowserSession) -> SessionRef:
    handle = session.handle
    live = ""
    if handle.reattachable:
        live = await session.live_view_url() or ""
    return SessionRef(session_id=handle.session_id, provider=handle.provider,
                      reattachable=handle.reattachable, live_view_url=live)


def _page_out(snapshot: PageSnapshot, session: SessionRef | None = None) -> PageOut:
    return PageOut(
        session=session,
        url=snapshot.url,
        title=snapshot.title,
        controls=[
            ControlOut(role=n.role, name=n.name, value=n.value,
                       disabled=n.disabled)
            for n in snapshot.controls()
        ],
        text=snapshot.text,
        summary=snapshot.summary(),
        screenshot=snapshot.screenshot,
    )


async def _session_for(ctx: NodeContext, sessions: BrowserSessions, node: str,
                       ref: SessionRef | None) -> BrowserSession:
    """The live session, reattaching from *ref* when there is none.

    The recovery path for an interrupted flow: the earlier calls were served
    from the journal, this process holds no browser, and *ref* — itself a
    journaled value — says which one to go back to. Without a ref, or with one
    the provider cannot honour, this refuses rather than opening a fresh
    browser somewhere else on the site.
    """
    if sessions.has(ctx.run_id):
        return sessions.current(ctx.run_id, node=node)
    if ref is None:
        return sessions.current(ctx.run_id, node=node)  # raises, and explains
    from loom.browser.base import SessionHandle

    return await sessions.attach(
        ctx.run_id,
        SessionHandle(session_id=ref.session_id, provider=ref.provider,
                      reattachable=ref.reattachable),
    )


# ---------------------------------------------------------------------------


class NavigateIn(BaseModel):
    url: str = Field(description="Absolute URL to open.")
    wait: str = Field(
        default="load",
        description="load | domcontentloaded | networkidle. The provider also "
                    "settles for client-rendered controls after this.")
    headless: bool = Field(default=True)
    viewport_width: int = Field(default=1280)
    viewport_height: int = Field(default=900)
    locale: str = Field(default="")
    timezone: str = Field(default="")
    max_wall_seconds: float = Field(
        default=120.0,
        description="Bounds the whole session. Without it a hung page holds a "
                    "provider slot until the run's lease expires.")
    vision: bool = Field(
        default=False,
        description="Also capture a screenshot of the page that opened. For a "
                    "person or a trace — tier 0 resolves targets from the "
                    "accessibility tree and never reads pixels.")
    scope: SessionScope = Field(
        default=SessionScope.STEP,
        description=(
            "STEP: the browser lives for one execution of the workflow body, "
            "and a run interrupted mid-flow refuses to continue rather than "
            "driving a fresh page. DURABLE: the session outlives the process, "
            "so the run can park on a person and reattach afterwards — needed "
            "for any approval, 2FA or takeover in the middle of a flow. "
            "Refused outright by a provider whose supports() omits 'reattach'."
        ),
    )


@register_node
class BrowserNavigateNode(Node[NavigateIn, PageOut]):
    """Open a page in a browser, and report the controls on it."""

    spec = NodeSpec(
        id="browser.navigate",
        category=NodeCategory.BROWSER,
        import_module="loom.nodes.browser",
        summary="Open a URL in a real browser and list the controls it shows.",
        description=(
            "Starts a browser flow. The session lives until the workflow body "
            "finishes, and the other browser.* nodes act on it. A run that "
            "resumes after an interruption cannot continue a flow it started "
            "in a previous process — browser.act says so rather than driving "
            "the wrong page."
        ),
        effect=EffectClass.READ,
        open_world=True,
        deterministic=False,
        requires=["browser"],
        tags=["browser", "web", "page", "navigate", "open", "url"],
        examples=[NodeExample(payload={"url": "https://example.com/book"})],
    )
    Input, Output = NavigateIn, PageOut

    async def run(self, ctx: NodeContext, payload: NavigateIn) -> PageOut:
        sessions = ctx.capability("browser_sessions")
        policy = BrowserPolicy(
            headless=payload.headless,
            scope=payload.scope,
            viewport=(payload.viewport_width, payload.viewport_height),
            locale=payload.locale,
            timezone=payload.timezone,
            max_wall_seconds=payload.max_wall_seconds,
        )

        async def go() -> PageOut:
            session = await sessions.open(ctx.run_id, policy)
            page = await session.navigate(payload.url, wait=payload.wait)
            if payload.vision:
                # `navigate` has no vision flag of its own on the provider, so
                # re-read once. One extra round trip against a page already
                # open, rather than a second node call the author has to know
                # to make.
                page = await session.snapshot(vision=True)
            return _page_out(page, await _session_ref(session))

        produced: PageOut = await ctx.call("browser:navigate", go,
                              retry=Retry(max_attempts=1))
        return produced


# ---------------------------------------------------------------------------


class SnapshotIn(BaseModel):
    session: SessionRef | None = Field(
        default=None,
        description="The session returned by browser.navigate. Supplying it "
                    "lets a run that resumed after an interruption reattach "
                    "instead of refusing — required for any flow that parks "
                    "on a person partway through.")
    vision: bool = Field(
        default=False,
        description="Also capture a screenshot. Tier 0 never reads pixels, so "
                    "this is for a person looking at a trace.")


@register_node
class BrowserSnapshotNode(Node[SnapshotIn, PageOut]):
    """Re-read the current page after it has changed."""

    spec = NodeSpec(
        id="browser.snapshot",
        category=NodeCategory.BROWSER,
        import_module="loom.nodes.browser",
        summary="Re-read the current page's controls after an action.",
        effect=EffectClass.READ,
        open_world=True,
        deterministic=False,
        requires=["browser"],
        tags=["browser", "page", "snapshot", "controls", "read"],
        examples=[NodeExample(payload={})],
    )
    Input, Output = SnapshotIn, PageOut

    async def run(self, ctx: NodeContext, payload: SnapshotIn) -> PageOut:
        sessions = ctx.capability("browser_sessions")

        async def read() -> PageOut:
            session = await _session_for(
                ctx, sessions, "browser.snapshot", payload.session)
            return _page_out(await session.snapshot(vision=payload.vision),
                             await _session_ref(session))

        produced: PageOut = await ctx.call("browser:snapshot", read,
                              retry=Retry(max_attempts=1))
        return produced


# ---------------------------------------------------------------------------


class ActIn(BaseModel):
    method: ActionMethod = Field(
        description="click | fill | select | check | uncheck | press | hover")
    target: TargetIn
    session: SessionRef | None = Field(
        default=None,
        description="The session returned by browser.navigate. Supplying it "
                    "lets a run that resumed after an interruption reattach "
                    "instead of refusing — required for any flow that parks "
                    "on a person partway through.")
    value: str = Field(
        default="",
        description="Text for fill, option for select, key for press.")
    effect: EffectClass = Field(
        default=EffectClass.WRITE,
        description=(
            "What this action does to the world: read | write | destructive. "
            "DECLARE IT. Filling a field and moving between steps are reads; "
            "submitting an order, sending a message or confirming a booking "
            "are writes; cancelling or deleting is destructive. Left "
            "unspecified this is WRITE, which is a fail-safe backstop and not "
            "a classification — a read left at the default is merely refused "
            "more often than it needs to be, while a write mistaken for a read "
            "is a booking nobody approved."
        ),
    )
    intent: str = Field(
        default="",
        description=(
            "How to describe this control if the target stops resolving. "
            "Only used to repair — the target is always tried first, so "
            "supplying an intent never costs a model call on the happy path."
        ),
    )
    if_drifted: DriftPolicy = Field(
        default=DriftPolicy.AUTO,
        description=(
            "What to do when the target no longer resolves and an intent was "
            "supplied. AUTO repairs a read and refuses a write; REPAIR always "
            "re-resolves; REFUSE always raises."
        ),
    )


class ActOut(BaseModel):
    ok: bool
    method: str
    target: str
    url: str = ""
    detail: str = ""
    tier: int = Field(
        default=0,
        description="0 when the caller's target resolved as given, 1 when it "
                    "had to be repaired from `intent`.")


@register_node
class BrowserActNode(Node[ActIn, ActOut]):
    """Act on one control, addressed by role and name."""

    spec = NodeSpec(
        id="browser.act",
        category=NodeCategory.BROWSER,
        import_module="loom.nodes.browser",
        summary="Click, fill, or select one control on the current page.",
        description=(
            "The target is resolved by accessible role and name, then by "
            "placeholder and label. Several matches RAISE rather than picking "
            "the first — pass ordinal to choose deliberately. Retries are off: "
            "an action that timed out after the page acted is "
            "indistinguishable from one that did not, and a retry repeats it."
        ),
        # Declared by the caller, never inferred. `click "Next"` and `click
        # "Confirm booking"` are the same shape, and a keyword list over the
        # target name is the guess DEFAULT_SYSTEM_PROMPT names as the tell for
        # a rule nobody should write.
        #
        # WRITE is the fail-safe default, the position OperationSpec.effect
        # takes and for the same reason: the default is a backstop, not a
        # classification. Phase 12's audit found the *name guess* under-
        # classifying 14% of LOOM's own operations including seven destructive
        # ones; here the seven would be card charges.
        effect=EffectClass.WRITE,
        effect_by={
            "effect": {
                "READ": EffectClass.READ,
                "WRITE": EffectClass.WRITE,
                "DESTRUCTIVE": EffectClass.DESTRUCTIVE,
            }
        },
        open_world=True,
        deterministic=False,
        requires=["browser"],
        tags=["browser", "click", "fill", "type", "select", "form", "act"],
        examples=[
            NodeExample(payload={
                "method": "fill",
                "target": {"role": "textbox", "name": "Email"},
                "value": "someone@example.com",
            }),
            NodeExample(payload={
                "method": "click",
                "target": {"role": "button", "name": "Search"},
            }),
        ],
    )
    Input, Output = ActIn, ActOut

    async def run(self, ctx: NodeContext, payload: ActIn) -> ActOut:
        sessions = ctx.capability("browser_sessions")

        async def act() -> ActOut:
            session = await _session_for(
                ctx, sessions, "browser.act", payload.session)
            target = payload.target.to_target()
            tier = 0
            try:
                result = await session.perform(
                    ActionPlan(method=payload.method, target=target,
                               value=payload.value, tier=0))
            except (TargetNotFound, AmbiguousTarget) as drift:
                target = await _repair(ctx, session, payload, drift)
                tier = 1
                result = await session.perform(
                    ActionPlan(method=payload.method, target=target,
                               value=payload.value, tier=1))
            return ActOut(ok=result.ok, method=result.method.value,
                          target=result.target, url=result.url,
                          detail=result.detail, tier=tier)

        # max_attempts=1, and the reason is `io.http_request`'s verbatim: a
        # timeout after the page acted looks exactly like one before it, and a
        # retry performs the action twice.
        performed: ActOut = await ctx.call(
            f"browser:{payload.method.value}", act, retry=Retry(max_attempts=1))
        return performed


async def _repair(ctx: NodeContext, session: BrowserSession, payload: ActIn,
                  drift: Exception) -> Target:
    """Re-aim a target that stopped resolving, or refuse to.

    **The rule that separates this from every self-healing browser agent.**
    They all repair a broken selector and carry on, which is correct for
    navigation and dangerous for a submit: a plan that silently changed under
    an effectful action is how an agent confirms the wrong reservation, and it
    fails the way that leaves no error behind — it succeeds, and the answer is
    wrong.

    So repair is allowed for a read and refused for a write, and the caller
    does not have to think about it: ``AUTO`` keys on the effect they already
    declared.
    """
    policy = payload.if_drifted
    effectful = payload.effect is not EffectClass.READ

    if not payload.intent:
        raise drift  # nothing to re-aim towards; the original error is better

    if policy is DriftPolicy.REFUSE or (policy is DriftPolicy.AUTO and effectful):
        raise SelectorDrift(
            f"{payload.target.to_target().describe()} no longer resolves, and "
            f"this action is declared {payload.effect.value} — it will not be "
            "re-aimed automatically. A control that moved under a write is a "
            "different control until somebody says otherwise. Re-observe and "
            "act on the result explicitly, or pass if_drifted='repair' if this "
            "action is genuinely safe to re-aim.",
            recorded=payload.target.to_target().describe(),
            found=str(drift)[:200],
        ) from drift

    page = await session.snapshot()
    named = [c for c in page.controls() if c.name]
    if not named:
        raise drift
    choice = await _choose(ctx, payload.intent, named)
    picked = next((c for c in named if c.name == choice), None)
    if picked is None:
        raise drift
    return Target(role=picked.role, name=picked.name, exact=True)


# ---------------------------------------------------------------------------


class CloseIn(BaseModel):
    session: SessionRef | None = Field(
        default=None,
        description="The session to end. Omit to close whichever one this run "
                    "is holding.")


class CloseOut(BaseModel):
    closed: bool = True


@register_node
class BrowserCloseNode(Node[CloseIn, CloseOut]):
    """End a browser session deliberately."""

    spec = NodeSpec(
        id="browser.close",
        category=NodeCategory.BROWSER,
        import_module="loom.nodes.browser",
        summary="End a durable browser session when the flow is finished.",
        description=(
            "Only needed for scope='durable'. A STEP session is closed by the "
            "engine when the body exits, however it exits — a durable one is "
            "deliberately not, because a run parked on a person must still "
            "have its browser when they answer. That makes ending it the "
            "workflow's decision. Providers also expire sessions on their own "
            "TTL, so a forgotten close is a cost rather than a leak."
        ),
        effect=EffectClass.READ,
        open_world=True,
        deterministic=False,
        requires=["browser"],
        tags=["browser", "close", "session", "cleanup", "finish"],
        examples=[NodeExample(payload={})],
    )
    Input, Output = CloseIn, CloseOut

    async def run(self, ctx: NodeContext, payload: CloseIn) -> CloseOut:
        sessions = ctx.capability("browser_sessions")

        async def shut() -> CloseOut:
            # Idempotent by intent: closing a session that has already gone is
            # what a retry, a replay and a tidy `finally` all do, and none of
            # them should fail for it.
            if not sessions.has(ctx.run_id) and payload.session is not None:
                from loom.browser.base import SessionHandle

                with contextlib.suppress(Exception):
                    await sessions.attach(
                        ctx.run_id,
                        SessionHandle(session_id=payload.session.session_id,
                                      provider=payload.session.provider,
                                      reattachable=payload.session.reattachable),
                    )
            await sessions.close_run(ctx.run_id)
            return CloseOut(closed=True)

        produced: CloseOut = await ctx.call(
            "browser:close", shut, retry=Retry(max_attempts=1))
        return produced


# ---------------------------------------------------------------------------


class ObserveIn(BaseModel):
    session: SessionRef | None = Field(
        default=None,
        description="The session returned by browser.navigate. Supplying it "
                    "lets a run that resumed after an interruption reattach "
                    "instead of refusing — required for any flow that parks "
                    "on a person partway through.")
    intent: str = Field(
        description="What you are looking for, in the words a person would "
                    "use: 'the email field', 'the button that confirms the "
                    "booking'.")
    role_hint: str = Field(
        default="",
        description="Narrow the candidates to one ARIA role when you know it.")


class ObserveOut(BaseModel):
    found: bool = False
    target: TargetIn | None = None
    tier: int = Field(
        default=1,
        description="Which tier resolved this: 0 deterministic, 1 model. "
                    "Recorded so 'how often did tier 0 suffice' is read off "
                    "the journal rather than estimated.")
    candidates: list[ControlOut] = Field(default_factory=list)
    reason: str = ""


@register_node
class BrowserObserveNode(Node[ObserveIn, ObserveOut]):
    """Find a control from a description, without acting on it."""

    spec = NodeSpec(
        id="browser.observe",
        category=NodeCategory.BROWSER,
        import_module="loom.nodes.browser",
        summary="Turn 'the confirm button' into a target, without clicking it.",
        description=(
            "Tier 0 first: if exactly one visible control carries that "
            "accessible name, no model is called at all. Only an ambiguous or "
            "empty result reaches tier 1, where a model picks from the page's "
            "own controls — it chooses among what is there and cannot invent a "
            "target. Observing never acts, so the result is safe to inspect "
            "before deciding, and it is journaled: a replay serves the target "
            "back with no model call."
        ),
        effect=EffectClass.READ,
        open_world=True,
        deterministic=False,
        requires=["browser"],
        tags=["browser", "observe", "find", "locate", "element", "control"],
        examples=[NodeExample(payload={"intent": "the button that confirms the booking"})],
    )
    Input, Output = ObserveIn, ObserveOut

    async def run(self, ctx: NodeContext, payload: ObserveIn) -> ObserveOut:
        sessions = ctx.capability("browser_sessions")
        session = await _session_for(
            ctx, sessions, "browser.observe", payload.session)
        page = await session.snapshot()

        controls = [c for c in page.controls()
                    if not payload.role_hint or c.role == payload.role_hint]
        named = [c for c in controls if c.name]

        # Tier 0 first, and this is the whole point of the ordering: an exact
        # accessible-name match costs nothing and is reproducible, so a model
        # is only paid for when the deterministic answer is absent or
        # ambiguous. `ActionPlan.tier` records which one answered.
        wanted = payload.intent.strip().lower()
        exact = [c for c in named if c.name.strip().lower() == wanted]
        if len(exact) == 1:
            return ObserveOut(
                found=True, tier=0,
                target=TargetIn(role=exact[0].role, name=exact[0].name, exact=True),
                reason="exact accessible-name match; no model call")

        if not named:
            return ObserveOut(
                found=False, tier=0,
                candidates=[ControlOut(role=c.role, name=c.name) for c in controls[:20]],
                reason=("no control on this page carries an accessible name, so "
                        "there is nothing for a description to match"))

        # A previous run may already have paid a model to answer this. A hit is
        # **verified against the live page before it is used** — that is what
        # makes a cross-run cache safe here: a stale entry costs a wasted lookup,
        # never a wrong click.
        cache = _plan_cache(ctx)
        if cache is not None:
            remembered = await cache.get(page.url, payload.intent, payload.role_hint)
            if remembered is not None:
                if await session.locate(remembered) == 1:
                    return ObserveOut(
                        found=True, tier=0,
                        target=TargetIn(role=remembered.role, name=remembered.name,
                                        ordinal=remembered.ordinal,
                                        exact=remembered.exact),
                        reason="from the plan cache; no model call")
                # It no longer resolves. Drop it rather than let every later run
                # re-verify the same dead entry before falling through.
                await cache.forget(page.url, payload.intent, payload.role_hint)

        choice = await _choose(ctx, payload.intent, named)
        picked = next((c for c in named if c.name == choice), None)
        if picked is None:
            return ObserveOut(
                found=False, tier=1,
                candidates=[ControlOut(role=c.role, name=c.name) for c in named[:20]],
                reason=f"no control matched {choice!r}")
        resolved = Target(role=picked.role, name=picked.name, exact=True)
        if cache is not None:
            await cache.put(page.url, payload.intent, resolved, payload.role_hint)
        return ObserveOut(
            found=True, tier=1,
            target=TargetIn(role=picked.role, name=picked.name, exact=True),
            reason=f"chosen from {len(named)} named controls")


async def _choose(ctx: NodeContext, intent: str,
                  controls: list[TreeNode]) -> str:
    """Ask a model which control the intent means.

    It picks from a list the page supplied, never from open text — so the worst
    outcome is the wrong control from the page rather than a target that does
    not exist. The answer still goes back through tier-0 resolution before
    anything is clicked, which is what stops a hallucinated name from becoming
    an action.
    """
    listed = "\n".join(f"- [{c.role}] {c.name}" for c in controls[:60])
    try:
        return await _ask_model(ctx, intent, listed)
    except ConfigurationError as exc:
        # `requires=["agent_backend"]` would be the obvious declaration and is
        # the wrong one: it is checked before *every* call, and tier 0 answers
        # most of them with no model at all. So a Runtime with no backend keeps
        # working for exact matches and explains itself only when a description
        # actually needs resolving — the shape `io.http_request` uses for a
        # credential it does not always need.
        raise ConfigurationError(
            f"browser.observe could not resolve {intent!r} without a model. No "
            "control on the page carries that exact accessible name, so tier 0 "
            "could not answer and tier 1 needs a model this Runtime does not "
            "have.\n"
            "Either pass Runtime(agent_backend=...), or address the control "
            "directly with browser.act and a Target(role=..., name=...) read "
            "off browser.navigate's output."
        ) from exc


async def _ask_model(ctx: NodeContext, intent: str, listed: str) -> str:
    answer = await ctx.agent(
        "Which single control below does this description refer to?\n"
        f"Description: {intent}\n\n"
        f"Controls on the page:\n{listed}\n\n"
        "Reply with the control's name exactly as written above, and nothing "
        "else. If none of them matches, reply NONE."
    )
    return str(getattr(answer, "output", answer) or "").strip()


def _plan_cache(ctx: NodeContext) -> PlanCache | None:
    """The Runtime's plan cache, or ``None`` when there is nothing to cache in.

    Opportunistic, never required: a node that *demanded* a cache would refuse
    to run somewhere it would otherwise work perfectly, just more expensively.
    The same shape ``io.http_request`` uses for a ``ConnectionBroker`` it only
    sometimes needs.
    """
    try:
        store = ctx.capability("cache")
    except ConfigurationError:
        return None
    return PlanCache(store, workflow=ctx.workflow)


# ---------------------------------------------------------------------------


class ExtractIn(BaseModel):
    session: SessionRef | None = Field(
        default=None,
        description="The session returned by browser.navigate. Supplying it "
                    "lets a run that resumed after an interruption reattach "
                    "instead of refusing — required for any flow that parks "
                    "on a person partway through.")
    target: TargetIn | None = Field(
        default=None,
        description="Read one control's text. Omit for the whole page.")


class ExtractOut(BaseModel):
    text: str


@register_node
class BrowserExtractNode(Node[ExtractIn, ExtractOut]):
    """Read text off the current page."""

    spec = NodeSpec(
        id="browser.extract",
        category=NodeCategory.BROWSER,
        import_module="loom.nodes.browser",
        summary="Read text from the current page, or from one control on it.",
        description=(
            "Returns text, not judgement. Structuring it is a @step if a rule "
            "can do it and ctx.agent() if it needs reading — the same split "
            "everything else follows."
        ),
        effect=EffectClass.READ,
        open_world=True,
        deterministic=False,
        requires=["browser"],
        tags=["browser", "extract", "read", "text", "scrape"],
        examples=[NodeExample(payload={})],
    )
    Input, Output = ExtractIn, ExtractOut

    async def run(self, ctx: NodeContext, payload: ExtractIn) -> ExtractOut:
        sessions = ctx.capability("browser_sessions")
        target = payload.target.to_target() if payload.target else None

        async def read() -> ExtractOut:
            session = await _session_for(
                ctx, sessions, "browser.extract", payload.session)
            return ExtractOut(text=await session.extract_text(target))

        produced: ExtractOut = await ctx.call("browser:extract", read,
                              retry=Retry(max_attempts=1))
        return produced
