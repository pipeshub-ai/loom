"""Keeping stored OAuth credentials fresh, before anything needs them.

:meth:`~loom.connectors.credentials.CredentialStore.get` already renews a
credential that is close to expiry — but only when something asks for it. That
covers a CLI command, and does not cover the case this module exists for: a
process that stays up for days and touches a credential rarely, where "renew on
next use" means the next use is the one that discovers the refresh token was
revoked a week ago.

:class:`CredentialRefreshService` sweeps the store on a timer and asks for
anything due. **It contains no refresh logic of its own** — it calls
``store.refresh(name)``, so it inherits the store's per-name lock, the
cross-process lease, refresh-token rotation handling, and the write-back. A
second implementation of renewal is exactly the thing that would drift from the
first.

It asks through ``refresh()`` rather than ``get()`` because the two answer
different questions. ``get()`` fails *soft* while a token is still valid —
right for a caller who wants a working token, wrong for a sweep, which would
then be unable to tell a successful renewal from a failed one and would report
every broken credential as healthy.

Deliberately *not* built on the cron machinery, though the shape looks similar.
``TriggerDispatcher`` fires ``WorkflowDefinition``s through ``Runtime.submit()``,
which means an ``ExecutionRecord`` and a journal per occurrence — thousands of
run records a year to keep one token alive, in a store this must not require at
all (``loom login`` works with no ``$LOOM_STORE``). Cron's guarantee is
exactly-once-per-occurrence, which exists to stop double-firing side effects; a
refresh is idempotent and self-healing, so a missed one should simply be
retried, never backfilled. What *is* reused is the surrounding machinery:
``Runtime.supervise()`` for shutdown, the :class:`~loom.runtime.clock.Clock`
port so the whole thing is testable in milliseconds, and ``LockProvider`` —
through the store's own refresher — for multi-process safety.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loom.connectors.credentials import RefreshPolicy, StoredCredential
from loom.runtime.clock import Clock, SystemClock

if TYPE_CHECKING:
    from loom.connectors.credentials import CredentialStore

__all__ = [
    "CredentialRefreshService",
    "RefreshOutcome",
    "RefreshReport",
    "service_for",
]

logger = logging.getLogger("workflow.credentials")

#: How often to sweep. Generous on purpose: the sweep is a safety net behind
#: the store's own on-use renewal, and every pass decrypts the credential file.
DEFAULT_INTERVAL = 60.0

#: How long to wait before retrying a credential whose refresh just failed,
#: and the ceiling that backoff climbs to. Without this a permanently dead
#: refresh token is retried every sweep forever — which is a self-inflicted
#: request flood against the authorization server, and the surest way to get
#: the *working* credentials rate-limited too.
INITIAL_BACKOFF = 60.0
MAX_BACKOFF = 3600.0


@dataclass(frozen=True)
class RefreshOutcome:
    """What the sweep did about one credential."""

    name: str
    status: str
    """``refreshed``, ``current``, ``skipped``, or ``failed``."""
    detail: str = ""
    expires_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("refreshed", "current")


@dataclass(frozen=True)
class RefreshReport:
    """The result of one sweep."""

    outcomes: tuple[RefreshOutcome, ...] = ()

    def of(self, status: str) -> tuple[RefreshOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == status)

    @property
    def refreshed(self) -> tuple[RefreshOutcome, ...]:
        return self.of("refreshed")

    @property
    def failed(self) -> tuple[RefreshOutcome, ...]:
        return self.of("failed")

    def __bool__(self) -> bool:
        return bool(self.outcomes)

    def __len__(self) -> int:
        return len(self.outcomes)


@dataclass
class _Backoff:
    """Per-credential retry state. In memory only, so a restart retries at once
    — which is what an operator who just fixed the credential expects."""

    not_before: datetime
    delay: float = INITIAL_BACKOFF


class CredentialRefreshService:
    """Renews stored credentials on a timer, before anything asks for them.

    Wire it into a long-lived process::

        service = CredentialRefreshService(store, runtime=runtime)
        await service.start()      # sweeps once now, then every interval

    Passing *runtime* registers with ``Runtime.supervise()``, so
    ``runtime.shutdown()`` stops this too — the same registration
    ``TriggerDispatcher`` and ``QueueConsumer`` use, so a host does not have to
    know which background services it happens to have wired up.
    """

    def __init__(
        self,
        store: CredentialStore,
        *,
        policy: RefreshPolicy | None = None,
        interval: float = DEFAULT_INTERVAL,
        clock: Clock | None = None,
        runtime: Any | None = None,
    ) -> None:
        self._store = store
        # The store's own policy when it has one, so the sweep and the
        # on-use path cannot disagree about what "due" means — two thresholds
        # for one question is how a credential ends up refreshed twice, or
        # never.
        self._policy = policy or getattr(store, "refresh_policy", None) or RefreshPolicy()
        self._interval = interval
        self._clock = clock or getattr(store, "clock", None) or SystemClock()
        self._runtime = runtime
        self._task: asyncio.Task[None] | None = None
        self._backoff: dict[str, _Backoff] = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Sweep once, then keep sweeping until :meth:`stop`.

        The immediate sweep is the "on restart" half: a process coming back up
        after being down longer than a token's lifetime renews before serving
        its first request, rather than discovering the problem on that request.
        """
        if self._task is not None:
            return
        await self.sweep()
        self._task = asyncio.create_task(self._loop())
        if self._runtime is not None:
            self._runtime.supervise(self)

    async def stop(self) -> None:
        """Stop sweeping. Safe to call when never started, or twice."""
        import contextlib

        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._runtime is not None:
            self._runtime.unsupervise(self)

    async def _loop(self) -> None:
        while True:
            await self._clock.sleep(self._interval)
            await self.sweep()

    # -- the sweep -----------------------------------------------------------

    async def sweep(
        self, names: Sequence[str] | None = None, *, force: bool = False
    ) -> RefreshReport:
        """Renew everything due (or *names*), and report what happened.

        Never raises. A background loop that dies on the first unreachable
        authorization server is worse than no background loop: the process
        keeps serving, silently without the safety net, and nothing says so.
        Every failure is an outcome in the report and a log line.

        *force* ignores both the due-check and the backoff — what an explicit
        ``loom refresh`` means, where the user is asking for the request to be
        made now.
        """
        try:
            stored = await self._peek_all(names)
        except Exception as exc:
            logger.warning("could not read the credential store: %s", exc)
            return RefreshReport()

        outcomes = [
            await self._refresh_one(name, credential, force=force)
            for name, credential in sorted(stored.items())
        ]
        return RefreshReport(tuple(outcomes))

    async def _peek_all(
        self, names: Sequence[str] | None
    ) -> dict[str, StoredCredential]:
        peek_all = getattr(self._store, "peek_all", None)
        peek = getattr(self._store, "peek", None)

        if names is None:
            if peek_all is not None:
                everything: dict[str, StoredCredential] = await peek_all()
                return everything
            if peek is None:
                return {}
            found: dict[str, StoredCredential] = {}
            for name in await self._store.names():
                credential = await peek(name)
                if credential is not None:
                    found[name] = credential
            return found

        if peek is None:
            return {}
        chosen: dict[str, StoredCredential] = {}
        for name in names:
            credential = await peek(name)
            if credential is not None:
                chosen[name] = credential
        return chosen

    async def _refresh_one(
        self, name: str, credential: StoredCredential, *, force: bool
    ) -> RefreshOutcome:
        now = self._clock.now()

        if not force:
            if not self._policy.is_due(credential, self._clock):
                return RefreshOutcome(name, "current", expires_at=credential.expires_at)
            waiting = self._backoff.get(name)
            if waiting is not None and now < waiting.not_before:
                return RefreshOutcome(
                    name,
                    "skipped",
                    f"retrying after {waiting.not_before.isoformat()}",
                    credential.expires_at,
                )

        if credential.refresh_token is None:
            # Nothing to renew with. Asking anyway is a guaranteed AuthExpired
            # and a wasted request every sweep, forever.
            return RefreshOutcome(
                name,
                "skipped",
                f"no refresh token — run 'loom connect {name}' to reauthorize",
                credential.expires_at,
            )

        try:
            # The store owns the whole renewal path: per-name lock,
            # cross-process lease, rotation, write-back. None of it is
            # reimplemented here. The returned Secret is deliberately dropped:
            # this service exists to update the store, not to hold a token.
            #
            # `refresh()` rather than `get()`, and the difference matters:
            # `get()` deliberately *fails soft* while a token is still valid,
            # returning it and trying again later. That is right for a caller
            # who wants a working token, and wrong here — a sweep that cannot
            # tell a successful renewal from a failed one would report every
            # broken credential as healthy and never back off, which is
            # precisely the blindness this service exists to remove.
            renew = getattr(self._store, "refresh", None)
            if renew is not None:
                await renew(name)
            else:
                await self._store.get(name)
        except Exception as exc:
            self._back_off(name, now)
            logger.warning(
                "could not refresh credential '%s': %s: %s",
                name, type(exc).__name__, exc,
            )
            return RefreshOutcome(name, "failed", str(exc), credential.expires_at)

        self._backoff.pop(name, None)
        # Read back rather than assumed: the store may have adopted another
        # process's write instead of minting its own, and the expiry that is
        # now on disk is the one worth reporting either way.
        current = await self._current(name)
        expires_at = current.expires_at if current is not None else None
        logger.info("refreshed credential '%s' (valid until %s)", name, expires_at)
        return RefreshOutcome(name, "refreshed", expires_at=expires_at)

    async def _current(self, name: str) -> StoredCredential | None:
        peek = getattr(self._store, "peek", None)
        return await peek(name) if peek is not None else None

    def _back_off(self, name: str, now: datetime) -> None:
        previous = self._backoff.get(name)
        delay = min(previous.delay * 2, MAX_BACKOFF) if previous else INITIAL_BACKOFF
        self._backoff[name] = _Backoff(now + timedelta(seconds=delay), delay)

    def __repr__(self) -> str:
        return f"<CredentialRefreshService every {self._interval}s>"


def service_for(
    host: Any, *, interval: float = DEFAULT_INTERVAL
) -> CredentialRefreshService | None:
    """A sweeper for *host*'s credential store, or ``None`` if it has none.

    *host* is a ``Runtime`` or anything exposing one as ``.runtime`` — a
    facade, for instance. Returning ``None`` rather than a no-op service is
    what keeps this free for everyone who has not configured credentials: a
    bare ``Runtime()`` has ``credentials=None``, so no background task is
    created and nothing changes for the hosts and tests that predate this.
    """
    runtime = getattr(host, "runtime", host)
    store = getattr(runtime, "credentials", None)
    if store is None:
        return None
    return CredentialRefreshService(store, interval=interval, runtime=runtime)
