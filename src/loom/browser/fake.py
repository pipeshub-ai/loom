"""A browser that is not one, for tests and for the smoke sandbox.

Two modes, and the difference matters more than it looks.

**Recorded** (``pages={...}``) serves ``PageSnapshot``s captured from real
pages and enforces the full contract: strict matching, visibility, a closed
session that refuses. This is what unit tests and the conformance kit use, and
what ``tests/corpus`` snapshots feed.

**Permissive** (the default) answers every action as though it worked. That is
not laziness, it is the same reasoning ``AutoRespondChannel`` is built on: the
smoke sandbox has no browser and no network, so a generated workflow that
drives a page can otherwise only reach a connection error — and the cheapest
repair a model can find for an error it cannot fix is to delete the browser
work, shipping a workflow that passes every check having removed the thing the
spec asked for.

A permissive result says so in ``detail``, so nothing downstream mistakes it for
evidence that a selector is right.
"""

from __future__ import annotations

from loom.browser.base import (
    ActionPlan,
    ActResult,
    BrowserPolicy,
    PageSnapshot,
    SessionHandle,
    SessionScope,
    Target,
    TreeNode,
)
from loom.browser.errors import (
    AmbiguousTarget,
    BrowserUnavailable,
    SessionLost,
    TargetNotFound,
)

__all__ = ["FakeBrowserProvider", "FakeBrowserSession"]

_FAKED = "faked: no browser in this environment"


class FakeBrowserSession:
    """A scripted session over recorded snapshots."""

    def __init__(self, provider: str, pages: dict[str, PageSnapshot],
                 permissive: bool, session_id: str, *,
                 reattachable: bool = False, live_view: str | None = None) -> None:
        self._pages = pages
        self._permissive = permissive
        self._closed = False
        self._live_view = live_view
        self._current = PageSnapshot(url="about:blank")
        self._handle = SessionHandle(session_id=session_id, provider=provider,
                                     reattachable=reattachable)
        self.performed: list[ActionPlan] = []
        """Every plan this session was asked to run. What a test asserts on."""

    @property
    def handle(self) -> SessionHandle:
        return self._handle

    def _live(self) -> None:
        if self._closed:
            raise SessionLost(f"session {self._handle.session_id} is closed")

    async def navigate(self, url: str, *, wait: str = "load") -> PageSnapshot:
        self._live()
        page = self._pages.get(url)
        if page is None and self._permissive:
            page = PageSnapshot(url=url, title="(faked)", text=_FAKED)
        if page is None:
            raise TargetNotFound(
                f"no recorded page for {url!r}. FakeBrowserProvider(pages=…) "
                "serves what it was given; pass permissive=True to answer "
                "anything.")
        self._current = page
        return page

    async def snapshot(self, *, vision: bool = False) -> PageSnapshot:
        self._live()
        return self._current

    async def locate(self, target: Target) -> int:
        self._live()
        if self._permissive and not self._current.tree:
            return 1
        return len(self._matches(target))

    def _matches(self, target: Target) -> list[TreeNode]:
        """Role and name, with the same substring default the real chain uses.

        Deliberately not a second resolution algorithm: the fake exists to
        exercise the *contract* — strictness, visibility, refusal — not to
        reproduce Playwright. A fake that resolved more cleverly than the real
        provider would hide exactly the failures it is meant to surface.
        """
        found = []
        for node in self._current.controls():
            if node.role != target.role:
                continue
            if not target.name:
                found.append(node)
            elif target.exact:
                if node.name == target.name:
                    found.append(node)
            elif target.name.lower() in node.name.lower():
                found.append(node)
        return found

    async def perform(self, plan: ActionPlan) -> ActResult:
        self._live()
        found = self._matches(plan.target)

        if self._permissive and not self._current.tree:
            self.performed.append(plan)
            return ActResult(ok=True, method=plan.method,
                             target=plan.target.describe(),
                             url=self._current.url, detail=_FAKED)

        if not found:
            raise TargetNotFound(
                f"nothing matched {plan.target.describe()}. "
                f"{self._current.summary()}",
                target=plan.target.describe())
        if len(found) > 1 and not plan.target.ordinal:
            raise AmbiguousTarget(
                f"{len(found)} controls matched {plan.target.describe()}",
                target=plan.target.describe(), matches=len(found))

        self.performed.append(plan)
        return ActResult(ok=True, method=plan.method,
                         target=plan.target.describe(), url=self._current.url,
                         detail="recorded page", matches=1)

    async def extract_text(self, target: Target | None = None) -> str:
        self._live()
        if target is None:
            return self._current.text
        found = self._matches(target)
        if not found:
            raise TargetNotFound(f"nothing matched {target.describe()}",
                                 target=target.describe())
        if len(found) > 1 and not target.ordinal:
            raise AmbiguousTarget(
                f"{len(found)} controls matched {target.describe()}",
                target=target.describe(), matches=len(found))
        node = found[target.ordinal - 1 if target.ordinal else 0]
        return node.value or node.name

    async def storage_state(self) -> bytes:
        self._live()
        return b"{}"

    async def live_view_url(self) -> str | None:
        return self._live_view

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True


class FakeBrowserProvider:
    """Serves recorded pages. Needs no browser, no network, no extra."""

    id = "fake"

    def __init__(self, pages: dict[str, PageSnapshot] | None = None, *,
                 permissive: bool = True, durable: bool = False) -> None:
        self._pages = dict(pages or {})
        self._permissive = permissive
        self._durable = durable
        self.sessions: list[FakeBrowserSession] = []
        self._by_id: dict[str, FakeBrowserSession] = {}
        """Sessions this provider is still holding. Stands in for what a hosted
        browser keeps between one process and the next."""

    def supports(self) -> frozenset[str]:
        capabilities = {"storage_state"}
        if self._durable:
            capabilities |= {"reattach", "live_view"}
        return frozenset(capabilities)

    async def open(self, policy: BrowserPolicy) -> FakeBrowserSession:
        if policy.scope is SessionScope.DURABLE and not self._durable:
            raise BrowserUnavailable(
                f"{self.id} cannot honour SessionScope.DURABLE. Construct it "
                "with durable=True, or use SessionScope.STEP."
            )
        session = FakeBrowserSession(
            self.id, self._pages, self._permissive, f"fake-{len(self.sessions)}",
            reattachable=self._durable,
            live_view=f"https://fake.test/live/{len(self.sessions)}"
            if self._durable else None,
        )
        self.sessions.append(session)
        self._by_id[session.handle.session_id] = session
        return session

    async def reattach(self, handle: SessionHandle) -> FakeBrowserSession:
        if not self._durable:
            raise SessionLost(f"{self.id} does not reattach")
        session = self._by_id.get(handle.session_id)
        if session is None or session.closed:
            raise SessionLost(
                f"{self.id} no longer holds session {handle.session_id}")
        # The *same* session, deliberately. Returning a fresh one is the mutant
        # `verify_browser_session` exists to reject: it looks like success and
        # silently loses everything the run had done.
        return session

    @classmethod
    def from_snapshots(cls, *snapshots: PageSnapshot,
                       permissive: bool = False) -> FakeBrowserProvider:
        """Build from snapshots, keyed by their own URLs."""
        return cls({s.url: s for s in snapshots}, permissive=permissive)
