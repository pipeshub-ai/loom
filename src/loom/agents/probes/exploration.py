"""Driving a page while writing code against it.

A :class:`~loom.agents.probes.base.Probe` navigates and reads, once. That is the
right contract for looking at an API response or a static page, and it is not
enough for an application: the controls a workflow has to address frequently do
not exist until something has been clicked.

Observed, and the reason this module exists. A reservation URL redirected to a
policy notice carrying two buttons; the whole booking application — party size,
calendar, times, contact fields — sat behind one click on it. The probe reported
the notice, accurately and confidently, and the coding agent wrote a workflow
against controls it had never seen, falling back to the spec's prose for their
names. Twenty-eight observations of the same page could not have found them,
because the limit was not the turn budget. It was that looking cannot click.

**This is the browser twin of ``call_read_operation``.** That tool already lets
the agent execute a real *read* operation against a live API while authoring, so
an account id is resolved once and baked in rather than guessed at. The same
argument applies to a control's accessible name, and the same rule bounds it:
authoring must never change the system it is writing code about.

**Read-only is structural here, not promised.** The guarantee is not that the
agent has been asked to behave — it is handed to a model, so a request is not a
control. It is that:

* ``FILL`` and ``PRESS`` are refused by :meth:`BrowserExploration.act`, so no
  text can be typed and no key can be sent. Every write worth worrying about —
  a booking, a signup, a purchase, a message — requires supplying data. Panels,
  calendars, dropdowns, tabs and disclosure widgets require none. That is the
  load-bearing layer.
* The session starts from **no** ``storage_state``: no cookies, no credentials,
  no signed-in identity, discarded when the job ends. So the one-click path that
  does not need typing — a purchase completed from saved details — has nothing
  saved to complete from.
* An action budget bounds it, because an agent that could not find something
  spent twenty-eight observations looking, and clicks are more expensive than
  looks in every sense.
* Every action is recorded and reported, so what was done is auditable rather
  than inferred.

None of this is journaled or reachable from a workflow body, exactly as no probe
is. What it changes is the quality of the constants the agent writes down.

What it deliberately does *not* claim: a click cannot write. A site can mutate on
GET, and one-click actions exist. The layers above make the realistic cases
unreachable; they do not make the guarantee absolute, and the tool description
says so rather than overclaiming — the lesson of ``SmokeIsolation``, which told a
model there were no credentials while inheriting the entire environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from loom.browser.base import (
    ActionMethod,
    ActionPlan,
    ActResult,
    BrowserPolicy,
    PageSnapshot,
    SessionScope,
    Target,
    TreeNode,
)
from loom.core.exceptions import WorkflowError

__all__ = [
    "EXPLORATION_INSTRUCTIONS",
    "EXPLORATORY_METHODS",
    "BrowserExploration",
    "ExplorationRefused",
    "ExplorationSession",
    "Recording",
    "recording_from_dict",
]


#: What may be done to a page while authoring.
#:
#: The allowlist *is* the read-only guarantee, so it is stated as a set of the
#: methods that are permitted rather than as a check for the ones that are not:
#: a new :class:`ActionMethod` is refused until somebody deliberately adds it,
#: which is the safe direction for a list that decides what a model may do.
#:
#: ``FILL`` and ``PRESS`` are the omissions that matter and they are not
#: negotiable here. Filling is how data reaches a form, and a form that has
#: received no data cannot be submitted into anything that matters.
EXPLORATORY_METHODS: frozenset[ActionMethod] = frozenset({
    ActionMethod.CLICK,
    ActionMethod.SELECT,
    ActionMethod.CHECK,
    ActionMethod.UNCHECK,
    ActionMethod.HOVER,
})


class ExplorationRefused(WorkflowError):  # noqa: N818 - names the refusal
    """An exploratory action was not one this session may perform.

    Its own type rather than ``ActionFailed`` because the two mean opposite
    things to whoever reads them: an action that failed says something about the
    page, and a refused one says nothing about the page at all. Only the second
    is a statement about what authoring is allowed to do.
    """


@runtime_checkable
class ExplorationSession(Protocol):
    """Drive a page while authoring. Cannot type; cannot submit.

    Deliberately **not** an extension of :class:`~loom.agents.probes.base.Probe`.
    That protocol is stateless by contract — ``supports`` then ``observe``, with
    nothing carried between them — and third parties already implement it against
    ``verify_probe``. Exploration is stateful by nature: the panel has to stay
    open between one look and the next. Widening the older contract to fit would
    weaken a guarantee that has already shipped, so this is a separate seam with
    its own conformance kit.
    """

    async def look(self) -> PageSnapshot:
        """What is on the page now."""
        ...

    async def act(self, plan: ActionPlan) -> ActResult:
        """Perform one allowed action.

        Raises :class:`ExplorationRefused` for anything outside
        :data:`EXPLORATORY_METHODS`, and when the action budget is spent.
        """
        ...

    def trace(self) -> tuple[ActionPlan, ...]:
        """Every action performed, in order.

        The audit record, and the fixture: a scripted fake replaying this drives
        a smoke run against the page the agent actually saw, post-interaction
        states included, rather than against one that answers anything.
        """
        ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Recording:
    """What an exploration saw, in the shape a scripted fake can replay.

    The point of keeping it: the smoke sandbox otherwise answers every browser
    action as though it worked, which proves the flow is *wired* and nothing
    about whether a single control exists. A recording turns that into a run
    against the pages the agent actually drove — including the ones that only
    exist after a click, which is the half no census could reach.

    ``transitions`` is keyed by ``Target.describe()`` rather than by position,
    so a replay follows the recording by *what it clicks* rather than by how
    many times it has clicked. A workflow that takes the steps in a different
    order still lands on the right pages; one that takes a step nobody recorded
    simply stays where it was.
    """

    pages: dict[str, PageSnapshot] = field(default_factory=dict)
    """URL to what was there. Feeds ``navigate``."""
    transitions: dict[str, PageSnapshot] = field(default_factory=dict)
    """``Target.describe()`` to the page that followed acting on it."""
    names: frozenset[str] = frozenset()
    """Every accessible name seen anywhere in the recording.

    The disambiguator for the one failure a replay can invent. A target missing
    from the *current* page means either "this control does not exist" — a real
    finding, and the reason to record at all — or "the replay is at a different
    point in the flow than the recording was", which is not. A name seen
    somewhere in the flow is evidence for the second; a name seen nowhere is
    evidence for the first."""

    def __bool__(self) -> bool:
        return bool(self.pages)

    def as_dict(self) -> dict[str, Any]:
        """JSON-able, because the smoke run is a subprocess.

        Screenshots are dropped rather than encoded: tier 0 never reads pixels,
        and a recording that carries them would put megabytes through a pipe to
        be ignored at the other end.
        """
        return {
            "pages": {url: _snapshot_as_dict(p) for url, p in self.pages.items()},
            "transitions": {
                key: _snapshot_as_dict(p) for key, p in self.transitions.items()
            },
            "names": sorted(self.names),
        }


def _snapshot_as_dict(page: PageSnapshot) -> dict[str, Any]:
    return {
        "url": page.url,
        "title": page.title,
        "text": page.text,
        "tree": [_node_as_dict(n) for n in page.tree],
    }


def _node_as_dict(node: TreeNode) -> dict[str, Any]:
    return {
        "role": node.role,
        "name": node.name,
        "value": node.value,
        "disabled": node.disabled,
        "children": [_node_as_dict(c) for c in node.children],
    }


def _node_from_dict(raw: dict[str, Any]) -> TreeNode:
    return TreeNode(
        role=raw.get("role", ""),
        name=raw.get("name", ""),
        value=raw.get("value", ""),
        disabled=bool(raw.get("disabled")),
        children=tuple(_node_from_dict(c) for c in raw.get("children") or ()),
    )


def _snapshot_from_dict(raw: dict[str, Any]) -> PageSnapshot:
    return PageSnapshot(
        url=raw.get("url", ""),
        title=raw.get("title", ""),
        text=raw.get("text", ""),
        tree=tuple(_node_from_dict(n) for n in raw.get("tree") or ()),
    )


def recording_from_dict(raw: dict[str, Any]) -> Recording:
    """Rebuild a :class:`Recording` inside the smoke subprocess."""
    return Recording(
        pages={u: _snapshot_from_dict(p) for u, p in (raw.get("pages") or {}).items()},
        transitions={
            k: _snapshot_from_dict(p)
            for k, p in (raw.get("transitions") or {}).items()
        },
        names=frozenset(raw.get("names") or ()),
    )


@dataclass
class BrowserExploration:
    """Reference :class:`ExplorationSession` over any ``BrowserProvider``.

    Built on the existing port rather than on Playwright directly, so a host's
    Browserbase or Kernel adapter serves authoring as well as running, and the
    capabilities a provider advertises are honoured in both.
    """

    provider: Any
    max_actions: int = 12
    """Clicks are more expensive than looks — in wall clock, in tokens, and in
    what they can do. The budget exists because an agent that cannot find a
    control does not stop looking; it looks differently, repeatedly."""

    _session: Any = field(default=None, init=False, repr=False)
    _trace: list[ActionPlan] = field(default_factory=list, init=False, repr=False)
    _pages: dict[str, PageSnapshot] = field(default_factory=dict, init=False, repr=False)
    _transitions: dict[str, PageSnapshot] = field(
        default_factory=dict, init=False, repr=False
    )

    async def open(self, url: str) -> PageSnapshot:
        """Start a session on *url*, replacing any already open.

        The policy is the whole anonymity argument, and it is set here rather
        than accepted from a caller: ``storage_state=None`` means no cookies and
        no signed-in identity, and ``SessionScope.STEP`` means the browser dies
        with the job rather than outliving it. A caller who could pass their own
        policy could pass an authenticated one, and then the guarantee would be
        a matter of how this is called.
        """
        await self.close()
        self._session = await self.provider.open(
            BrowserPolicy(scope=SessionScope.STEP, storage_state=None)
        )
        snapshot: PageSnapshot = await self._session.navigate(url)
        # Under both the requested URL and the one reached. A workflow written
        # from this will navigate to whichever the agent wrote down, and a
        # recording that only answers one of them fails a replay for a redirect
        # the agent handled correctly.
        self._pages[url] = snapshot
        if snapshot.url:
            self._pages.setdefault(snapshot.url, snapshot)
        return snapshot

    async def look(self) -> PageSnapshot:
        snapshot: PageSnapshot = await self._require().snapshot()
        return snapshot

    async def act(self, plan: ActionPlan) -> ActResult:
        if plan.method not in EXPLORATORY_METHODS:
            allowed = ", ".join(sorted(m.value for m in EXPLORATORY_METHODS))
            raise ExplorationRefused(
                f"exploration cannot {plan.method.value}. Allowed while "
                f"authoring: {allowed}.\n"
                "Typing and key presses are withheld on purpose: supplying data "
                "is what turns a form into a submission, and authoring must not "
                "change the system it is writing code about. Open the control "
                "and read what it offers — the workflow itself does the filling."
            )
        if len(self._trace) >= self.max_actions:
            raise ExplorationRefused(
                f"exploration budget spent ({self.max_actions} actions). What "
                "you have seen is what there is to go on. If the control still "
                "has not appeared, say so in the code rather than continuing to "
                "look — a target resolved from an intent at run time is a "
                "better answer than a name that was guessed at."
            )
        result: ActResult = await self._require().perform(plan)
        # Recorded whatever the outcome. An action that failed still happened,
        # still cost budget, and is still part of what this session did to the
        # page — an audit record that omits the failures is not one.
        self._trace.append(plan)
        if result.ok:
            after = await self._require().snapshot()
            self._transitions[plan.target.describe()] = after
            if after.url:
                self._pages.setdefault(after.url, after)
        return result

    def trace(self) -> tuple[ActionPlan, ...]:
        return tuple(self._trace)

    def recording(self) -> Recording:
        """What was seen, for a scripted fake to replay."""
        names = {
            node.name
            for page in (*self._pages.values(), *self._transitions.values())
            for node in page.controls()
            if (node.name or "").strip()
        }
        return Recording(
            pages=dict(self._pages),
            transitions=dict(self._transitions),
            names=frozenset(names),
        )

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    def _require(self) -> Any:
        if self._session is None:
            raise ExplorationRefused(
                "no page is open — call open(url) before looking or acting."
            )
        return self._session


def click(role: str, name: str, ordinal: int = 0) -> ActionPlan:
    """The common case, spelled once.

    Exists so a caller never assembles an :class:`ActionPlan` with a method of
    its own choosing — the allowlist is checked in :meth:`BrowserExploration.act`
    regardless, but a helper that makes the safe call the easy one is worth more
    than a check nobody reaches.
    """
    return ActionPlan(
        method=ActionMethod.CLICK,
        target=Target(role=role, name=name, ordinal=ordinal, exact=True),
        description=f"click {name!r}",
    )


#: What the agent is told, when a browser is wired.
#:
#: Named in the prompt rather than left to the tool list, for the reason
#: ``ProbeRegistry.prompt_block`` already documents: with ``observe_target``
#: available and a URL in the spec, the agent spent forty turns reading
#: integration docs and reached for the probe once, on the wrong URL, at the
#: last turn. A capability nothing points at is a capability nobody uses.
#:
#: The paragraph that matters is the second. An agent that has been handed a
#: census does not know it is looking at an interstitial — the census is clean —
#: so the instruction has to name the *symptom* it should act on rather than the
#: cause it cannot see.
EXPLORATION_INSTRUCTIONS = (
    "## Driving the page\n"
    "\n"
    "`open_page(url, click_through=[…])` opens a real browser and lists the "
    "controls, with their "
    "roles and accessible names. `interact(role, name, ordinal=0)` clicks one "
    "and lists what appears next. Together they reach controls that do not "
    "exist on first load.\n"
    "\n"
    "Prefer `click_through` over repeated `interact` calls: it gets past a "
    "notice, a consent gate or a landing step in one call and reports every "
    "state on the way. Clicking one at a time spends a turn per layer, and "
    "turns are what you write the code with.\n"
    "\n"
    "**Use them whenever the controls you need are not in what you were "
    "shown.** A page that offers two or three controls, or whose census looks "
    "nothing like the task, is usually a notice, a consent gate or a landing "
    "step in front of the real application — not a simple page. `open_page` "
    "reports where it actually landed, so compare that against the URL you "
    "asked for: a redirect means what you are reading describes somewhere "
    "else.\n"
    "\n"
    "Names read this way are worth writing down. A `Target(role=…, name=…)` "
    "taken from a real census is a fact; one taken from the spec's prose is a "
    "guess that fails by matching nothing.\n"
    "\n"
    "**This cannot type and cannot press keys**, so it cannot fill in or "
    "submit a form — that is what keeps authoring from changing the system it "
    "is writing about. It is not absolute: a badly built page can act on a "
    "click alone. Do not click anything that reads as confirming, sending, "
    "paying, booking or deleting. Reveal controls; leave the acting to the "
    "workflow.\n"
)
