"""One browser per run, for as long as the body is executing.

**The hard part of this phase, and the reason `SessionScope.STEP` is the
default.** A browser is a stateful conversation: step 7 of a flow is meaningless
unless steps 1-6 happened *to that browser*. The journal replays values; it
cannot replay a browser.

So a session lives exactly as long as one execution of the workflow body, keyed
by run id, opened by the first ``browser.navigate`` and closed when the body
exits however it exits. Within one execution, the ``browser.*`` nodes compose
normally and each journals its own entry.

**What happens after a crash is the interesting case, and it is refused rather
than guessed.** Re-entering the body serves the already-journaled calls from the
journal without touching a browser — correct, and free. The first call that was
*not* journaled then needs a live browser positioned where the last one left
off, and there is no such browser: this process just started. Options were:

1. Silently open a fresh session and carry on. The flow would run against
   ``about:blank``, every target would miss, and the run would fail somewhere
   downstream of the actual problem — or worse, succeed against the wrong page.
2. Re-execute the recorded navigations to rebuild position. That re-performs
   effects the journal exists to prevent repeating, silently.
3. Refuse, and say exactly what to do instead.

This module does (3). :class:`~loom.browser.errors.SessionLost` names the run,
the node, and the two ways out: put the flow inside a single ``ctx.step`` so it
re-runs whole, or use a provider whose ``supports()`` includes ``reattach``
(13.3). A limitation a caller is told about beats one they discover from a
wrong answer.
"""

from __future__ import annotations

import logging

from loom.browser.base import (
    BrowserPolicy,
    BrowserProvider,
    BrowserSession,
    SessionHandle,
    SessionScope,
)
from loom.browser.errors import SessionLost

logger = logging.getLogger(__name__)

__all__ = ["BrowserSessions"]


class BrowserSessions:
    """Per-run browser sessions over one provider."""

    def __init__(self, provider: BrowserProvider,
                 policy: BrowserPolicy | None = None) -> None:
        self._provider = provider
        self._policy = policy or BrowserPolicy()
        self._sessions: dict[str, BrowserSession] = {}
        self._scopes: dict[str, SessionScope] = {}
        """How each run's session is scoped, so ending a body knows whether to
        close it or let go of it."""

    @property
    def provider(self) -> BrowserProvider:
        return self._provider

    @property
    def policy(self) -> BrowserPolicy:
        return self._policy

    async def open(self, run_id: str, policy: BrowserPolicy | None = None,
                   ) -> BrowserSession:
        """Start a flow. Replaces any session this run already holds.

        Replacing rather than reusing is deliberate: a second ``navigate`` at
        the top of a run is a new flow, and inheriting the previous flow's
        cookies and history would make the second one behave differently from
        the same code run first — a difference nothing in the workflow explains.
        """
        await self.close_run(run_id)
        active = policy or self._policy
        session = await self._provider.open(active)
        self._sessions[run_id] = session
        self._scopes[run_id] = active.scope
        return session

    async def attach(self, run_id: str, handle: SessionHandle,
                     scope: SessionScope = SessionScope.DURABLE) -> BrowserSession:
        """Re-acquire a session this run opened in an earlier process.

        Raises :class:`SessionLost` when the provider cannot — and a provider
        that cannot **must** raise rather than open a fresh one. That failure
        looks like success: the flow continues against a browser that never saw
        its earlier steps, and every assertion downstream still passes.
        """
        if not handle.reattachable:
            raise SessionLost(
                f"session {handle.session_id} was opened by {handle.provider!r} "
                "with no reattach support, so it did not outlive the process "
                "that created it. Open the flow with scope='durable' on a "
                "provider whose supports() includes 'reattach'."
            )
        session = await self._provider.reattach(handle)
        self._sessions[run_id] = session
        self._scopes[run_id] = scope
        return session

    def current(self, run_id: str, *, node: str = "browser") -> BrowserSession:
        """The session this run is mid-flow on, or a refusal that explains itself."""
        session = self._sessions.get(run_id)
        if session is not None:
            return session
        raise SessionLost(
            f"{node} was called on run {run_id} with no live browser.\n"
            "\n"
            "Either this run never called browser.navigate, or it is resuming "
            "after an interruption: the earlier browser calls were served from "
            "the journal, and the browser they ran against died with the "
            "process that started it.\n"
            "\n"
            "A fresh browser is NOT where the flow left off, so continuing "
            "would drive the wrong page. Two ways forward:\n"
            "  - put the whole browser flow inside one `ctx.step(...)`, so an "
            "interruption re-runs it whole; or\n"
            "  - use a provider whose supports() includes 'reattach' and "
            "SessionScope.DURABLE, so the session outlives the process."
        )

    def has(self, run_id: str) -> bool:
        return run_id in self._sessions

    async def release(self, run_id: str) -> None:
        """End this body execution's hold on the run's session.

        A ``STEP`` session is closed: it exists for one execution of the body
        and a leaked Chromium outlives the run that opened it.

        A ``DURABLE`` session is **let go of, not closed** — that is the whole
        point of the scope. The run may be parked on a person for two hours,
        and the browser has to still be there when they finish. Its lifetime
        belongs to the provider, which is how every hosted browser vendor
        already works: sessions carry their own TTL. ``browser.close`` is how a
        workflow ends one deliberately.
        """
        if self._scopes.get(run_id) is SessionScope.DURABLE:
            self._sessions.pop(run_id, None)
            self._scopes.pop(run_id, None)
            return
        await self.close_run(run_id)

    async def close_run(self, run_id: str) -> None:
        """Close this run's session outright. Safe to call when there is none."""
        self._scopes.pop(run_id, None)
        session = self._sessions.pop(run_id, None)
        if session is None:
            return
        try:
            await session.close()
        except Exception:
            logger.debug("closing browser session for %s raised", run_id,
                         exc_info=True)

    async def close_all(self) -> None:
        for run_id in list(self._sessions):
            await self.close_run(run_id)

    def __len__(self) -> int:
        return len(self._sessions)
