"""The reference provider: a real Chromium, driven by Playwright.

Apache-2.0, three ranged dependencies, no LLM vendor SDK, and already behind the
``[browser]`` extra that ``BrowserProbe`` needed — so this adds nothing to
LOOM's dependency tree. That is not a happy accident; it is the conclusion of
the audit in phase 13 §2.2, which ruled out the AGPL options outright and ruled
out ``browser-use`` as a *dependency* (≈40 ``==`` pins including
``pydantic==2.12.5``, against LOOM's ``pydantic>=2.0``) while keeping it welcome
as a host adapter through the entry point.

``patchright`` is a drop-in Apache-2.0 fork with anti-detection patches;
``engine="patchright"`` swaps it in. LOOM ships no evasion of its own — that is
a treadmill and a posture this project should not take.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from loom.blobs.attachment import Attachment
from loom.browser.base import (
    ActionMethod,
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
    ActionFailed,
    AmbiguousTarget,
    BrowserUnavailable,
    SessionLost,
    TargetNotFound,
)
from loom.browser.resolve import resolve

logger = logging.getLogger(__name__)

__all__ = ["LocalBrowserProvider", "LocalBrowserSession"]

#: Settle sequence, lifted from ``BrowserProbe`` rather than re-derived.
#: ``networkidle`` never fires on some sites, so it is attempted and suppressed,
#: and a fixed settle covers the client-rendered controls that paint after it.
#: That module learned this the expensive way; copying the knowledge is cheaper
#: than rediscovering it.
_NETWORK_IDLE_MS = 8000
_SETTLE_MS = 800

#: One evaluate, returning the accessibility-relevant shape of the page. The
#: browser has already parsed this document, so the walk happens there rather
#: than over serialised HTML — the same reason the corpus capture does its
#: surgery in-page.
_TREE = """
() => {
    const visible = (el) => {
        const s = getComputedStyle(el);
        // Playwright's definition, and deliberately nothing more. Opacity is
        // NOT part of it: `<input class=visually-hidden>` beside a styled
        // label is how every design system builds a custom radio or switch,
        // and those inputs are routinely opacity:0.
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 || r.height > 0;
    };
    const roleOf = (el) => {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit;
        const tag = el.tagName.toLowerCase();
        if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
        if (tag === 'button') return 'button';
        if (tag === 'select') return 'combobox';
        if (tag === 'textarea') return 'textbox';
        if (tag === 'input') {
            const t = (el.getAttribute('type') || 'text').toLowerCase();
            return {checkbox:'checkbox', radio:'radio', search:'searchbox',
                    submit:'button', button:'button', reset:'button',
                    range:'slider', number:'spinbutton'}[t] || 'textbox';
        }
        if (/^h[1-6]$/.test(tag)) return 'heading';
        return 'generic';
    };
    const nameOf = (el) => (
        el.getAttribute('aria-label')
        || (el.labels && el.labels[0] && el.labels[0].innerText.trim())
        || el.getAttribute('placeholder')
        || el.getAttribute('title')
        || (el.innerText || el.value || '').trim().slice(0, 120)
        || ''
    );
    const SEL = 'a[href],button,input,select,textarea,[role],h1,h2,h3';
    const nodes = [...document.querySelectorAll(SEL)].filter(visible).map((el) => ({
        role: roleOf(el),
        name: nameOf(el),
        value: (el.value || '').slice(0, 120),
        disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
    }));
    return {
        url: location.href,
        title: document.title,
        nodes: nodes.slice(0, 400),
        text: (document.body.innerText || '').trim().slice(0, 20000),
    };
}
"""


class LocalBrowserSession:
    """One page in a locally-launched browser."""

    def __init__(self, provider: str, browser: Any, context: Any, page: Any,
                 policy: BrowserPolicy, session_id: str) -> None:
        self._browser = browser
        self._context = context
        self._page = page
        self._policy = policy
        self._closed = False
        self._handle = SessionHandle(session_id=session_id, provider=provider,
                                     reattachable=False)
        page.set_default_timeout(policy.action_timeout_seconds * 1000)

    @property
    def handle(self) -> SessionHandle:
        return self._handle

    def _live(self) -> Any:
        """The page, or a refusal.

        A closed session that keeps working is the mutant the conformance kit
        drives, because it is the failure that looks like success: the caller
        believes it has torn down and the browser is still holding cookies.
        """
        if self._closed:
            raise SessionLost(
                f"session {self._handle.session_id} is closed. Open a new one — "
                "a closed session that still answered would keep credentials "
                "alive past the point the caller believes it released them."
            )
        return self._page

    async def navigate(self, url: str, *, wait: str = "load") -> PageSnapshot:
        page = self._live()
        try:
            await page.goto(url, wait_until=wait,
                            timeout=self._policy.max_wall_seconds * 1000)
        except Exception as exc:
            raise ActionFailed(f"could not open {url}: {exc}", url=url) from exc
        await self._settle()
        return await self.snapshot()

    async def _settle(self) -> None:
        page = self._page
        # Never fires on some sites; the fixed settle below covers it.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=_NETWORK_IDLE_MS)
        await page.wait_for_timeout(_SETTLE_MS)

    async def snapshot(self, *, vision: bool = False) -> PageSnapshot:
        page = self._live()
        raw = await page.evaluate(_TREE)
        shot = None
        if vision:
            shot = Attachment.from_bytes(
                "page.png", await page.screenshot(full_page=False), mime="image/png")
        return PageSnapshot(
            url=raw["url"],
            title=raw["title"],
            tree=tuple(
                TreeNode(role=n["role"], name=n["name"], value=n["value"],
                         disabled=n["disabled"])
                for n in raw["nodes"]
            ),
            text=raw["text"],
            screenshot=shot,
        )

    async def locate(self, target: Target) -> int:
        found = await resolve(self._live(), target)
        return found.matches

    async def perform(self, plan: ActionPlan) -> ActResult:
        page = self._live()
        found = await resolve(page, plan.target)

        if found.matches == 0:
            snapshot = await self.snapshot()
            raise TargetNotFound(
                f"nothing matched {plan.target.describe()}. {snapshot.summary()}",
                target=plan.target.describe(), snapshot=snapshot.summary())
        if found.matches > 1:
            raise AmbiguousTarget(
                f"{found.matches} controls matched {plan.target.describe()}. "
                "Pass Target(ordinal=n) to choose one deliberately — picking "
                "the first would be a guess this refuses to make.",
                target=plan.target.describe(), matches=found.matches)

        locator = found.locator
        try:
            await self._apply(locator, plan)
        except Exception as exc:
            raise ActionFailed(
                f"{plan.method.value} on {plan.target.describe()} failed: {exc}",
                target=plan.target.describe(), url=page.url) from exc

        await self._settle()
        return ActResult(
            ok=True, method=plan.method, target=plan.target.describe(),
            url=page.url, detail=f"resolved by {found.accessor}", matches=1,
        )

    async def _apply(self, locator: Any, plan: ActionPlan) -> None:
        method = plan.method
        if method is ActionMethod.CLICK:
            await locator.click()
        elif method is ActionMethod.FILL:
            await locator.fill(plan.value)
        elif method is ActionMethod.SELECT:
            await locator.select_option(plan.value)
        elif method is ActionMethod.CHECK:
            await locator.check()
        elif method is ActionMethod.UNCHECK:
            await locator.uncheck()
        elif method is ActionMethod.PRESS:
            await locator.press(plan.value or "Enter")
        elif method is ActionMethod.HOVER:
            await locator.hover()
        else:  # pragma: no cover - StrEnum is closed
            raise ActionFailed(f"unsupported method {method!r}")

    async def extract_text(self, target: Target | None = None) -> str:
        page = self._live()
        if target is None:
            return (await self.snapshot()).text
        found = await resolve(page, target)
        if found.matches == 0:
            raise TargetNotFound(f"nothing matched {target.describe()}",
                                 target=target.describe())
        if found.matches > 1 or found.locator is None:
            raise AmbiguousTarget(
                f"{found.matches} controls matched {target.describe()}",
                target=target.describe(), matches=found.matches)
        text: str = await found.locator.inner_text()
        return text.strip()

    async def storage_state(self) -> bytes:
        # `_live()` first, and this was missing until the conformance kit caught
        # it: a closed session leaked Playwright's own TargetClosedError, so a
        # caller could not tell "this session is gone" from "this page
        # misbehaved" without matching on a vendor exception type.
        self._live()
        import json
        state = await self._context.storage_state()
        return json.dumps(state).encode()

    async def live_view_url(self) -> str | None:
        return None  # a local headless browser has nothing to hand a person

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for target in (self._context, self._browser):
            try:
                await target.close()
            except Exception:
                logger.debug("browser teardown raised", exc_info=True)
        stop = getattr(self, "_stop", None)
        if stop is not None:
            try:
                await stop()
            except Exception:
                logger.debug("playwright stop raised", exc_info=True)


class LocalBrowserProvider:
    """Launches a browser in this process.

    What a developer wants and what CI can run. A host executing model-authored
    flows against real credentials wants a provider that isolates them, which is
    what the port is for.
    """

    id = "local"

    def __init__(self, *, engine: str = "playwright",
                 browser: str = "chromium") -> None:
        self._engine = engine
        self._browser_name = browser

    def supports(self) -> frozenset[str]:
        # No `reattach`: this browser dies with the process, and claiming
        # otherwise is the failure that looks like success — a run parked two
        # hours on a human, resumed against a session that silently restarted.
        capabilities = {"vision", "storage_state"}
        if self._engine == "patchright":
            capabilities.add("stealth")
        return frozenset(capabilities)

    def _driver(self) -> Any:
        module = "patchright.async_api" if self._engine == "patchright" else "playwright.async_api"
        try:
            import importlib
            return importlib.import_module(module).async_playwright
        except ImportError as exc:
            extra = "stealth" if self._engine == "patchright" else "browser"
            raise BrowserUnavailable(
                f"the {self._engine} driver is not installed: "
                f"pip install 'loomsdk[{extra}]' && {self._engine} install chromium"
            ) from exc

    async def open(self, policy: BrowserPolicy) -> LocalBrowserSession:
        if policy.scope is SessionScope.DURABLE:
            # Refused, never downgraded. `ExecutionSandbox.enforces`, one layer
            # out: a host told "not here" is better off than one that believes
            # its session outlives the process when it does not.
            raise BrowserUnavailable(
                "LocalBrowserProvider cannot honour SessionScope.DURABLE — this "
                "browser dies with the process. Use SessionScope.STEP, or a "
                "provider whose supports() includes 'reattach'."
            )

        driver = self._driver()
        manager = driver()
        pw = await manager.start()
        try:
            browser = await getattr(pw, self._browser_name).launch(
                headless=policy.headless)
            context_args: dict[str, Any] = {
                "viewport": {"width": policy.viewport[0],
                             "height": policy.viewport[1]},
            }
            if policy.user_agent:
                context_args["user_agent"] = policy.user_agent
            if policy.locale:
                context_args["locale"] = policy.locale
            if policy.timezone:
                context_args["timezone_id"] = policy.timezone
            if policy.storage_state:
                import json
                context_args["storage_state"] = json.loads(policy.storage_state)
            context = await browser.new_context(**context_args)
            page = await context.new_page()
        except Exception:
            await manager.__aexit__(None, None, None)
            raise

        import uuid
        session = LocalBrowserSession(
            self.id, browser, context, page, policy, uuid.uuid4().hex)
        # The driver outlives the browser and has to be stopped with it, or the
        # node process keeps running after the workflow finishes.
        session._stop = lambda: manager.__aexit__(None, None, None)  # type: ignore[attr-defined]
        return session

    async def reattach(self, handle: SessionHandle) -> LocalBrowserSession:
        raise SessionLost(
            f"{self.id} cannot reattach to session {handle.session_id}: a "
            "locally-launched browser does not outlive the process that "
            "started it. This is reported rather than worked around, because "
            "silently opening a fresh session loses the run's state while "
            "appearing to succeed."
        )
