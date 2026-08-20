"""Driving a web page: the ports, and what travels over them.

**LOOM ships no browser infrastructure.** It ships two Protocols, one reference
provider over Playwright, a fake for offline tests, and a conformance kit
(``loom.testing.conformance.verify_browser_session``) so a host proves its own
Browserbase, Kernel, or Anchor adapter correct. That is the position
``loom/knowledge/`` takes about vector databases and ``loom/events/`` about
brokers, for the same reason: every adapter shipped is one that must be tested
against a live service forever.

    rt = Runtime(store=store, browser=LocalBrowserProvider())

``Runtime(browser=…)`` defaults to ``None`` and nothing is enforced unless a
host composes one in. The ``browser.*`` nodes are how a workflow reaches it.

**Nothing in this package calls a model.** A session resolves controls by
accessible role and name through Playwright's own locators — tier 0 — so the
whole package is importable, testable and shippable without an API key.

Tier 1 lives one layer up, in the ``browser.observe`` node, deliberately: it
reaches a model through ``ctx.agent``, which already carries journaling, the
turn budget, hooks and backend-independence. Putting it on the session instead
would have meant a second path to a model with none of that. A provider that
resolves server-side (Stagehand's CDP-native path, Browserbase's) is tier 2 and
would extend this Protocol when one is actually adapted — not before, because a
method no implementation provides is a seam that reads as supported and is not.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from loom.blobs.attachment import Attachment
from loom.core.ids import stable_hash

__all__ = [
    "ActResult",
    "ActionMethod",
    "ActionPlan",
    "BrowserPolicy",
    "BrowserProvider",
    "BrowserSession",
    "DriftPolicy",
    "PageSnapshot",
    "SessionHandle",
    "SessionScope",
    "Target",
    "TreeNode",
]


class ActionMethod(StrEnum):
    """What to do with a control once it is found.

    Kept separate from :class:`Target` because finding and acting fail
    differently and are worth reading apart in a journal: "no such control" and
    "the control refused the value" are different problems for whoever is
    looking.
    """

    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    UNCHECK = "uncheck"
    PRESS = "press"
    HOVER = "hover"


class DriftPolicy(StrEnum):
    """What to do when a page no longer matches a plan that was recorded.

    Every product in this space self-heals: the cached selector breaks,
    re-query the model, carry on. That is right for navigation and dangerous
    for a submit — a plan that silently changed under an effectful action is
    how an agent confirms the wrong reservation, and it fails the way this
    codebase keeps naming: it succeeds, and the answer is wrong.

    ``AUTO`` therefore keys on the declared effect rather than on a separate
    dial, so the safe behaviour is what a caller gets by not thinking about it.
    """

    AUTO = "auto"
    """Repair a READ silently; refuse a WRITE or DESTRUCTIVE."""

    REPAIR = "repair"
    """Always re-resolve. For a caller who knows the page is volatile and the
    action is safe to re-aim."""

    REFUSE = "refuse"
    """Always raise on drift, even for a read."""


class SessionScope(StrEnum):
    """How long a browser session lives relative to the journal.

    ``STEP`` is the only scope this phase implements, and it is the honest
    default: the whole browser flow is the body of one durable call, so a crash
    re-runs it exactly as ``Retry`` already means everywhere else in LOOM and
    there is no browser state to reconstruct. ``DURABLE`` — a session that
    outlives the process, journaled by handle and reattached — needs a provider
    that keeps sessions, and arrives in 13.3.
    """

    STEP = "step"
    DURABLE = "durable"


@dataclass(frozen=True)
class Target:
    """How to find one control. Role and name are the address, not a hint.

    This is a Playwright locator spec, a Stagehand ``ObserveResult`` and an
    accessibility-tree node at once, because all three agree on role plus
    accessible name. Resolving through it is tier 0: deterministic,
    auto-waiting, and **strict** — an ambiguous match raises rather than
    picking the first, which is the whole reason to prefer it over a selector.

    ``css`` is an escape hatch and deliberately not the primary. A selector
    harvested from one render is right on that render and silently wrong later,
    and it fails by matching nothing rather than by erroring.
    """

    role: str
    """ARIA role: ``button``, ``textbox``, ``combobox``, ``checkbox``, ``link``…"""

    name: str = ""
    """The accessible name — in practice, the label a person reads."""

    ordinal: int = 0
    """Which match, when a page legitimately repeats a control.

    Zero means "there must be exactly one". Anything else is a caller saying
    they know there are several and which they want, and it is the only way
    past :class:`AmbiguousTarget` without a selector.
    """

    css: str = ""
    """Provider-native escape hatch. Tried only when role and name find nothing."""

    exact: bool = False
    """Match the whole accessible name rather than a substring.

    Substring is the default because it is Playwright's, and because a page's
    name often carries more than the visible label. It is also why ``ordinal``
    exists: ``name="Continue"`` matches "Continue with Google" too.
    """

    @property
    def fingerprint(self) -> str:
        """What a drift check compares. Excludes ``css`` deliberately.

        A CSS path changing under a stable labelled button is not drift, and
        treating it as drift would make every redeploy of a site re-approve.
        """
        return stable_hash({"role": self.role, "name": self.name,
                            "ordinal": self.ordinal})

    def describe(self) -> str:
        base = f"{self.role} named {self.name!r}" if self.name else self.role
        return f"{base} (#{self.ordinal})" if self.ordinal else base


@dataclass(frozen=True)
class ActionPlan:
    """One resolved, replayable action. The unit the journal caches.

    Journaled whole, so a replay serves the plan back without resolving
    anything and without a model call — the property Stagehand and Skyvern both
    build a bespoke cache to get, and which falls out of LOOM's engine.
    """

    method: ActionMethod
    target: Target
    value: str = ""
    """Text for ``FILL``, option for ``SELECT``, key for ``PRESS``."""

    description: str = ""
    """What a person reading a trace needs. Never load-bearing."""

    tier: int = 0
    """Which resolution tier produced this: 0 deterministic, 1 model, 2 provider.

    Recorded rather than inferred, so "how often did tier 0 suffice" is read off
    the journal instead of estimated. That is the measurement
    ``tests/corpus`` makes at authoring time, continued into production.
    """


@dataclass(frozen=True)
class ActResult:
    """What performing a plan did."""

    ok: bool
    method: ActionMethod
    target: str
    """``Target.describe()`` — a sentence, because this is read by people."""

    url: str = ""
    """Where the page ended up. An action that navigates is the common case."""

    detail: str = ""
    matches: int = 1
    """How many controls the target found. Always 1 on success."""


@dataclass(frozen=True)
class TreeNode:
    """One node of the accessibility tree.

    The a11y tree rather than the DOM, and rather than pixels, because it is
    what the tier-0 locators address and because it is two orders of magnitude
    cheaper to journal than a screenshot.
    """

    role: str
    name: str = ""
    value: str = ""
    disabled: bool = False
    children: tuple[TreeNode, ...] = ()

    def walk(self) -> Iterator[TreeNode]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class PageSnapshot:
    """A page as tier 0 sees it."""

    url: str
    title: str = ""
    tree: tuple[TreeNode, ...] = ()
    text: str = ""
    screenshot: Attachment | None = None
    """Only when vision was asked for. Pixels are expensive to journal and
    tier 0 never reads them."""

    def controls(self) -> list[TreeNode]:
        """Every interactive node, flattened. What an author wants to see."""
        interactive = {
            "button", "link", "textbox", "searchbox", "combobox", "listbox",
            "checkbox", "radio", "switch", "slider", "spinbutton", "tab",
            "menuitem", "menuitemcheckbox", "menuitemradio", "option",
        }
        return [n for root in self.tree for n in root.walk()
                if n.role in interactive]

    def summary(self) -> str:
        """One line, in the shape ``BrowserProbe`` already established.

        "0 named, 4 unnamed" is not a description of a page, it is the reason
        an intent written against it will not resolve.
        """
        found = self.controls()
        named = sum(1 for n in found if n.name)
        line = f"{self.title or self.url!r}: {len(found)} control(s), {named} named"
        if found and not named:
            line += (
                ". None carry an accessible name — a role-and-name target will "
                "find nothing here."
            )
        return line


@dataclass(frozen=True)
class SessionHandle:
    """What a journal records about a session. Never the session itself."""

    session_id: str
    provider: str
    reattachable: bool = False
    storage_ref: str = ""
    """Blob reference for ``storage_state``. Never the cookie jar inline: it is
    a live credential, and the journal is the last place it should land."""


@dataclass(frozen=True)
class BrowserPolicy:
    """What a session may do. The host decides; the provider enforces."""

    headless: bool = True
    scope: SessionScope = SessionScope.STEP
    viewport: tuple[int, int] = (1280, 900)
    user_agent: str = ""
    locale: str = ""
    timezone: str = ""
    max_wall_seconds: float = 120.0
    """Bounds the session. Without it a hung page holds a provider slot until
    the run's lease expires, which is a much later and much less obvious
    failure."""

    action_timeout_seconds: float = 15.0
    storage_state: bytes | None = None
    """Cookies and local storage to start from — a prior authenticated session.
    Supplied by the host, never journaled."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Provider-specific settings. A proxy, a stealth mode, a region. Deliberately
    opaque: LOOM cannot enumerate what six vendors offer, and pretending to
    would date badly."""


@runtime_checkable
class BrowserSession(Protocol):
    """A live browser. Everything here is I/O; nothing here is journaled.

    Journaling happens one layer up, in the ``browser.*`` nodes, so that this
    port stays a thin description of a browser and a host implementing it does
    not have to know what a journal is.
    """

    @property
    def handle(self) -> SessionHandle: ...

    async def navigate(self, url: str, *, wait: str = "load") -> PageSnapshot: ...

    async def snapshot(self, *, vision: bool = False) -> PageSnapshot: ...

    async def locate(self, target: Target) -> int:
        """How many **visible** controls *target* matches. Tier 0, no model.

        A count rather than a handle, because the caller's next question is
        always "exactly one?" and because a handle would tempt a provider into
        keeping state between calls. Raises nothing for zero or many — deciding
        what those mean belongs to the node, which has the run's context.
        """
        ...

    async def perform(self, plan: ActionPlan) -> ActResult: ...

    async def extract_text(self, target: Target | None = None) -> str: ...

    async def storage_state(self) -> bytes: ...

    async def live_view_url(self) -> str | None:
        """A URL a person can watch or take over. ``None`` when unsupported.

        Present in this phase and unused by it: a host that has one should not
        have to wait for 13.3's human takeover to expose it.
        """
        ...

    async def close(self) -> None: ...


@runtime_checkable
class BrowserProvider(Protocol):
    """Opens sessions. The thing a host swaps."""

    id: str

    def supports(self) -> frozenset[str]:
        """Capabilities this provider actually honours.

        Known names: ``reattach``, ``live_view``, ``vision``, ``storage_state``,
        ``stealth``, ``proxy``, ``captcha``.

        Reported rather than assumed, exactly as ``ExecutionSandbox.enforces``
        reports which ``SandboxPolicy`` fields a platform can honour — and for
        the same reason. A host told "not here" is better off than one that
        believes its two-hour approval will survive a deploy because the
        session is durable, when it is not.
        """
        ...

    async def open(self, policy: BrowserPolicy) -> BrowserSession: ...

    async def reattach(self, handle: SessionHandle) -> BrowserSession:
        """Re-acquire a session that outlived this process.

        Raises :class:`~loom.browser.errors.SessionLost` when it cannot. A
        provider without ``reattach`` in :meth:`supports` should raise
        unconditionally rather than quietly opening a *fresh* session — that
        failure looks like success and loses the run's state silently, which is
        why the conformance kit drives it as a mutant.
        """
        ...
