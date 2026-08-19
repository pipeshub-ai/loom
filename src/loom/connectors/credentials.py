"""Where a step's ``ctx.credential(name)`` and ``loom connect`` both end up.

:class:`CredentialStore` is the port :mod:`loom.steps.context`
already calls (``await self.credentials.get(name)``) — this module is what
makes that call resolve to something real instead of an unresolvable import.

Three reference implementations, one conformance suite
(``tests/test_credential_store_conformance.py``) run against all of them so
they cannot quietly diverge:

``MemoryCredentialStore``
    In-process, unencrypted. Tests, and short embedded runs where losing
    everything on restart is fine because nothing durable depends on it.
``EncryptedFileCredentialStore``
    Every credential in one file, encrypted at rest. Takes any
    :class:`~loom.connectors.encryption.KeyProvider`.
``KeyringCredentialStore``
    The interactive default: the master key lives in the OS keyring, the
    encrypted payload lives in a file (see the class docstring for why those
    are split rather than putting the payload in the keyring too).

``get()`` returns a :class:`~loom.core.secret.Secret` rather than
a bare ``str`` — the same fail-closed reasoning as everywhere else a live
token exists in memory: a caller that forgets to ``.reveal()`` gets a type
error immediately, not a token leaked into a log line three months from now.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loom.connectors.encryption import (
    DecryptionError,
    Envelope,
    GeneratedFileKeyProvider,
    KeyProvider,
    KeyringKeyProvider,
    atomic_write_bytes,
    default_key_provider,
)
from loom.core.exceptions import AuthExpired, CredentialNotFound
from loom.core.secret import Secret
from loom.runtime.clock import Clock, SystemClock

__all__ = [
    "DEFAULT_REFRESH_SKEW",
    "REFRESH_SKEW_ENV",
    "CredentialStore",
    "EncryptedFileCredentialStore",
    "KeyringCredentialStore",
    "LayeredCredentialStore",
    "MemoryCredentialStore",
    "Peekable",
    "RefreshPolicy",
    "Refresher",
    "StoredCredential",
    "credential_store_scope",
    "current_credential_store",
    "resolve_bearer_token",
]

logger = logging.getLogger("workflow.credentials")

#: Renew this long before a credential actually expires.
#:
#: Refreshing at the moment of expiry is refreshing too late: a token with two
#: seconds left passes every check here and then 401s on the request it was
#: fetched for. The window also absorbs modest clock drift between this machine
#: and the authorization server, which is otherwise indistinguishable from a
#: token that expired early.
DEFAULT_REFRESH_SKEW = timedelta(minutes=10)

REFRESH_SKEW_ENV = "LOOM_OAUTH_REFRESH_SKEW"


# ---------------------------------------------------------------------------
# The store bound to the run currently executing
# ---------------------------------------------------------------------------

_current_store: ContextVar[CredentialStore | None] = ContextVar(
    "loom_current_credential_store", default=None
)


def current_credential_store() -> CredentialStore | None:
    """The :class:`CredentialStore` bound to the run executing right now, if any.

    A toolset's lazy client factory — ``get_default_client()``,
    ``get_default_auth()`` — is a process-wide singleton with no ``ctx``
    parameter to thread a store through, and adding one would mean every
    ``@step`` function in every toolset takes a ``StepContext`` it does not
    otherwise need (see ``steps/definition.py::_wants_step_context``). Bound
    for the duration of a step's attempt (``runtime/context.py::_attempt_loop``)
    via a :class:`~contextvars.ContextVar` rather than a mutable module
    global, because ``Runtime`` drives many runs concurrently on one
    process's event loop and a global would let one run's store leak into a
    concurrently-executing sibling's.

    Returns ``None`` when no run is currently resolving a step (including
    every call site that predates ``Runtime(credentials=...)``), which is
    exactly the signal every toolset call site below uses to fall back to
    its own environment-variable resolution unchanged.
    """
    return _current_store.get()


@contextmanager
def credential_store_scope(store: CredentialStore | None) -> Iterator[None]:
    """Bind *store* as :func:`current_credential_store` for this block."""
    token = _current_store.set(store)
    try:
        yield
    finally:
        _current_store.reset(token)


async def resolve_bearer_token(name: str) -> str | None:
    """This run's stored credential for *name*, as a bearer token, or ``None``.

    ``None`` covers two cases a caller's own env-var fallback treats
    identically: no store is bound to this run at all, or one is bound but
    has nothing under *name*. Neither is an error — every toolset client had
    a working env-var story before ``CredentialStore`` existed, and this is
    what keeps "nothing connected" and "the pre-Phase-7 behaviour" the same
    code path (see each toolset's ``get_default_client``/``get_default_auth``).

    An expired, unrefreshable credential under *name* is different and is
    **not** swallowed here — :class:`AuthExpired` propagates to the caller,
    which for a toolset invoked from inside a workflow step eventually
    reaches ``runtime/engine.py``, which parks the run rather than silently
    falling back to a (possibly stale, possibly absent) environment variable.
    """
    store = current_credential_store()
    if store is None:
        return None
    try:
        secret = await store.get(name)
    except CredentialNotFound:
        return None
    return secret.reveal()


@dataclass(frozen=True)
class StoredCredential:
    """A credential as a store persists it — the unit ``put``/``get`` exchange.

    Distinct from :class:`loom.toolsets.connections.Credential`:
    that one is a workflow-facing, journalable value returned by a
    ``ConnectionBroker``; this one is what a ``CredentialStore`` keeps at
    rest, and it is never journaled — ``token``/``refresh_token`` are
    :class:`Secret`, so returning one from a step fails at write time
    instead of leaking a live token into the journal.
    """

    token: Secret[str]
    refresh_token: Secret[str] | None = None
    expires_at: datetime | None = None
    scopes: frozenset[str] = frozenset()
    token_type: str = "bearer"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    issued_at: datetime | None = None
    """When this token was minted, when the minter recorded it.

    Only used to derive the token's *lifetime*, which is what stops a fixed
    refresh window from being wrong for a short-lived token — see
    :meth:`RefreshPolicy.effective_skew`. Optional because a credential written
    before this field existed has none, and inventing one would manufacture a
    lifetime nobody measured.
    """

    def is_expired(self, clock: Clock | None = None) -> bool:
        """Whether ``expires_at`` has actually passed, per an injected clock.

        The hard fact, deliberately distinct from
        :meth:`RefreshPolicy.is_due`, which is the *policy* — "renew it soon".
        A credential can be due for renewal and perfectly usable, and the two
        callers of these want different answers: ``loom whoami`` reports this
        one, ``get()`` acts on the other.

        Same reasoning as ``Credential.expired`` in ``toolsets/connections.py``
        — a credential store is read from inside a run, so its notion of
        "now" must be replayable, not a call to the wall clock.
        """
        if self.expires_at is None:
            return False
        return (clock or SystemClock()).now() >= _aware(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        """The plain-dict projection a store encrypts and writes.

        The only place ``.reveal()`` is called outside a caller's own use of
        the token — and it is called here, at the boundary where the value
        is about to be encrypted, which is exactly the greppable point the
        design intends.
        """
        return {
            "token": self.token.reveal(),
            "refresh_token": self.refresh_token.reveal() if self.refresh_token else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "scopes": sorted(self.scopes),
            "token_type": self.token_type,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StoredCredential:
        expires_at = data.get("expires_at")
        # Absent from every record written before this field existed. Read with
        # a default rather than indexed, so an existing encrypted store loads
        # unchanged instead of raising on the first read after an upgrade.
        issued_at = data.get("issued_at")
        refresh_token = data.get("refresh_token")
        return cls(
            token=Secret(data["token"]),
            refresh_token=Secret(refresh_token) if refresh_token else None,
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
            issued_at=datetime.fromisoformat(issued_at) if issued_at else None,
            scopes=frozenset(data.get("scopes", [])),
            token_type=data.get("token_type", "bearer"),
            metadata=dict(data.get("metadata", {})),
        )


def _aware(when: datetime) -> datetime:
    """A naive timestamp read as UTC. Stores round-trip ISO strings, and one
    written without an offset must not compare as local time."""
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RefreshPolicy:
    """When a stored credential should be renewed — a policy, not a fact.

    Split from :class:`StoredCredential` because it is a *decision* about a
    credential rather than a property of one, and hosts legitimately differ:
    a laptop CLI wants a generous window, a fleet talking to a rate-limited
    authorization server wants a tight one.

    ``max_fraction`` is the part that is not obvious, and the reason a flat
    skew is not enough on its own. A provider issuing **five-minute** tokens
    under a ten-minute window would report *every* token as due the moment it
    was minted: every call refreshes, the authorization server sees a refresh
    storm, and on a server that rotates refresh tokens each of those rotations
    invalidates the last — turning a helpful default into an outage. Clamping
    the window to a fraction of the token's own lifetime makes the policy scale
    with whatever the provider actually issues.

    That clamp is also what makes it safe for
    :class:`~loom.connectors.oauth_client.OAuthClient` to use this policy when
    checking whether another process already refreshed: a freshly minted token
    has its whole lifetime ahead of it, so it can never be immediately due, so
    two processes cannot bounce a credential back and forth.
    """

    skew: timedelta = DEFAULT_REFRESH_SKEW
    max_fraction: float = 0.5
    """Never claim more than this much of a token's total lifetime."""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RefreshPolicy:
        """Read :data:`REFRESH_SKEW_ENV` (seconds), or use the defaults.

        An unparseable or negative value is a misconfiguration that would
        silently disable early refresh, so it warns and falls back rather than
        being adopted.
        """
        source = os.environ if env is None else env
        raw = source.get(REFRESH_SKEW_ENV)
        if not raw:
            return cls()
        try:
            seconds = float(raw)
        except ValueError:
            logger.warning(
                "%s=%r is not a number of seconds; using the default of %ss",
                REFRESH_SKEW_ENV, raw, int(DEFAULT_REFRESH_SKEW.total_seconds()),
            )
            return cls()
        if seconds < 0:
            logger.warning(
                "%s=%r is negative; using the default of %ss",
                REFRESH_SKEW_ENV, raw, int(DEFAULT_REFRESH_SKEW.total_seconds()),
            )
            return cls()
        return cls(skew=timedelta(seconds=seconds))

    def effective_skew(self, credential: StoredCredential) -> timedelta:
        """:attr:`skew`, clamped to a fraction of this token's own lifetime.

        Falls back to the unclamped window when the lifetime is unknown — a
        credential stored before ``issued_at`` existed. That is the pre-existing
        behaviour for those records and cannot be improved by guessing.
        """
        if credential.expires_at is None or credential.issued_at is None:
            return self.skew
        lifetime = _aware(credential.expires_at) - _aware(credential.issued_at)
        if lifetime <= timedelta(0):
            return timedelta(0)
        return min(self.skew, lifetime * self.max_fraction)

    def due_at(self, credential: StoredCredential) -> datetime | None:
        """When *credential* becomes due for renewal, or ``None`` if never."""
        if credential.expires_at is None:
            return None
        return _aware(credential.expires_at) - self.effective_skew(credential)

    def is_due(self, credential: StoredCredential, clock: Clock | None = None) -> bool:
        """Whether *credential* should be renewed now.

        A credential with no ``expires_at`` is never due: the issuer said
        nothing about a lifetime, and renewing on a schedule this code invented
        would spend a refresh token to answer a question nobody asked.
        """
        due = self.due_at(credential)
        if due is None:
            return False
        return (clock or SystemClock()).now() >= due


@runtime_checkable
class Refresher(Protocol):
    """Mints a fresh credential when a stored one has expired.

    :mod:`loom.connectors.oauth_client` (Phase 2) is the reference
    implementation, and is where cross-process single-flighting via a
    ``LockProvider`` lease belongs — a store only guarantees single-flight
    *within one process* (see ``BaseCredentialStore``). A store with no
    refresher configured raises :class:`AuthExpired` on expiry rather than
    guessing at how to renew something it was never told how to renew.
    """

    async def refresh(self, name: str, stored: StoredCredential) -> StoredCredential:
        """Return a fresh credential for *name*, or raise ``AuthExpired``."""
        ...


@runtime_checkable
class Peekable(Protocol):
    """A store that can report its raw record without triggering a refresh.

    All three reference stores implement this via ``BaseCredentialStore``.
    A cross-process :class:`Refresher` (Phase 2's OAuth client) is written
    against this rather than against the concrete store classes, so it works
    with any store, including a future fourth implementation, as long as it
    offers this one extra method.
    """

    async def peek(self, name: str) -> StoredCredential | None: ...


@runtime_checkable
class CredentialStore(Protocol):
    """Resolves a named credential, refreshing it if it has expired."""

    async def get(self, name: str) -> Secret[str]:
        """The current, valid token for *name*.

        Raises :class:`CredentialNotFound` if nothing is stored under that
        name, and :class:`AuthExpired` if it has expired and either no
        refresher is configured or the refresher itself could not renew it
        without a human — the run should park, not fail, on that one.
        """
        ...

    async def put(self, name: str, credential: StoredCredential) -> None:
        """Store (or replace) the credential named *name*."""
        ...

    async def forget(self, name: str) -> None:
        """Remove the credential named *name*, if present. Never raises for
        a name that is already absent — a repeated logout is a no-op."""
        ...

    async def names(self) -> list[str]:
        """Every credential name currently stored, sorted."""
        ...


class BaseCredentialStore:
    """Shared expiry, refresh, and single-flight logic; subclasses supply storage.

    Splitting it out means ``MemoryCredentialStore`` and
    ``EncryptedFileCredentialStore`` cannot each grow their own answer to
    "what happens when a stored credential is expired but there is no
    refresher" — a question with exactly one right answer (:class:`AuthExpired`,
    not a silent stale token), which drifting into two implementations
    invites getting wrong.
    """

    def __init__(
        self,
        *,
        refresher: Refresher | None = None,
        clock: Clock | None = None,
        refresh_policy: RefreshPolicy | None = None,
    ) -> None:
        self._refresher = refresher
        self.clock = clock or SystemClock()
        self.refresh_policy = refresh_policy or RefreshPolicy.from_env()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = self._locks[name] = asyncio.Lock()
        return lock

    async def get(self, name: str) -> Secret[str]:
        """The current token for *name*, renewed early if it is close to expiry.

        Two thresholds, and the gap between them is the point:

        **Due** (:meth:`RefreshPolicy.is_due`) is when renewal is *attempted*.
        Waiting for actual expiry means renewing too late — a token with two
        seconds left passes every check here and then 401s on the request it
        was fetched for.

        **Expired** (:meth:`StoredCredential.is_expired`) is when failing to
        renew becomes an error. In between, a refresh that cannot happen — no
        refresher configured, the network is down, the authorization server is
        having an outage — returns the credential that is *still valid*, logs,
        and lets the next call try again. Raising there instead would take a
        working token away from a caller ten minutes before it needed to,
        turning a transient upstream failure into a hard failure of our own.
        """
        stored = await self._read(name)
        if stored is None:
            raise CredentialNotFound(f"no credential named '{name}' is stored")
        if not self.refresh_policy.is_due(stored, self.clock):
            return stored.token

        async with self._lock_for(name):
            # Another task in this process may have refreshed while this one
            # waited for the lock — re-read rather than refreshing again.
            stored = await self._read(name)
            if stored is None:
                raise CredentialNotFound(f"no credential named '{name}' is stored")
            if not self.refresh_policy.is_due(stored, self.clock):
                return stored.token
            # Soft only while the token still works. Once it has expired there
            # is nothing to fall back to and the caller must hear about it.
            return await self._renew(
                name, stored, soft=not stored.is_expired(self.clock)
            )

    async def refresh(self, name: str) -> Secret[str]:
        """Renew *name* now, whether or not it is due.

        What an explicit ``loom refresh`` runs. Distinct from :meth:`get`,
        which renews only when the policy says so — but the renewal itself is
        the same code, so the two cannot drift into different answers about
        locking, rotation, or write-back.

        Unlike :meth:`get`, a failure here always raises: the caller asked for
        this to happen, so reporting success because the old token still works
        would be answering a different question.
        """
        async with self._lock_for(name):
            stored = await self._read(name)
            if stored is None:
                raise CredentialNotFound(f"no credential named '{name}' is stored")
            return await self._renew(name, stored, soft=False)

    async def _renew(
        self, name: str, stored: StoredCredential, *, soft: bool
    ) -> Secret[str]:
        """Refresh and persist, holding this name's lock.

        *soft* is the early-window rule: a renewal that cannot happen — no
        refresher configured, the network down, the authorization server having
        an outage — returns the credential that is *still valid* and lets the
        next call try again. Raising instead would take a working token away
        from a caller ten minutes before it needed to, turning a transient
        upstream failure into a hard failure of our own.
        """
        if self._refresher is None:
            if soft:
                return stored.token
            raise AuthExpired(
                f"credential '{name}' has expired and no refresher is "
                f"configured. Run 'loom connect {name}' to reauthorize.",
                name=name,
            )

        try:
            refreshed = await self._refresher.refresh(name, stored)
        except Exception as exc:
            if not soft:
                raise
            # Warning rather than debug: a credential that keeps failing here
            # becomes a hard failure at expiry, and that is worth seeing coming.
            logger.warning(
                "early refresh of credential '%s' failed (%s); the current "
                "token is still valid until %s and will be retried",
                name, type(exc).__name__, stored.expires_at,
            )
            return stored.token

        await self._write(name, refreshed)
        return refreshed.token

    async def put(self, name: str, credential: StoredCredential) -> None:
        await self._write(name, credential)

    async def forget(self, name: str) -> None:
        await self._delete(name)

    async def names(self) -> list[str]:
        return await self._list()

    async def peek(self, name: str) -> StoredCredential | None:
        """The stored record as-is, with no expiry check and no refresh.

        Not part of :class:`CredentialStore` — a `Refresher` that has just
        lost a cross-process lock race needs to check whether the winner
        already wrote a fresh credential *without* recursing back into
        :meth:`get` (which would call this same refresher again). Calling
        this instead of ``get()`` is what makes that check safe.
        """
        return await self._read(name)

    async def peek_all(self) -> dict[str, StoredCredential]:
        """Every stored record at once, no expiry check and no refresh.

        Exists for the background sweep, which asks "is anything due?" about
        the whole store on a timer. Doing that as ``names()`` plus a ``peek()``
        each costs one full decrypt-and-parse of the credential file *per
        credential*, forever, on a loop — so the encrypted store overrides this
        to read once.
        """
        found: dict[str, StoredCredential] = {}
        for name in await self._list():
            stored = await self._read(name)
            if stored is not None:
                found[name] = stored
        return found

    async def _read(self, name: str) -> StoredCredential | None:
        raise NotImplementedError

    async def _write(self, name: str, credential: StoredCredential) -> None:
        raise NotImplementedError

    async def _delete(self, name: str) -> None:
        raise NotImplementedError

    async def _list(self) -> list[str]:
        raise NotImplementedError


class MemoryCredentialStore(BaseCredentialStore):
    """In-process, unencrypted. The default for tests and for embedded runs
    that need no persistence across a restart."""

    def __init__(
        self,
        *,
        refresher: Refresher | None = None,
        clock: Clock | None = None,
        refresh_policy: RefreshPolicy | None = None,
    ) -> None:
        super().__init__(
            refresher=refresher, clock=clock, refresh_policy=refresh_policy
        )
        self._data: dict[str, StoredCredential] = {}

    async def _read(self, name: str) -> StoredCredential | None:
        return self._data.get(name)

    async def _write(self, name: str, credential: StoredCredential) -> None:
        self._data[name] = credential

    async def _delete(self, name: str) -> None:
        self._data.pop(name, None)

    async def _list(self) -> list[str]:
        return sorted(self._data)

    def __repr__(self) -> str:
        return f"<MemoryCredentialStore {len(self._data)} credential(s)>"


class EncryptedFileCredentialStore(BaseCredentialStore):
    """Every credential in one file, encrypted at rest with a :class:`KeyProvider`.

    Reads and writes serialize through one ``asyncio.Lock`` so a read sees a
    consistent file and two writes in the same process do not race the
    load-modify-save cycle. That does not extend across processes — see
    :func:`~loom.connectors.encryption.atomic_write_bytes` for
    what cross-process safety this *does* provide (no corruption from a
    write landing mid-write) and what it does not (a lost update between two
    writers that both read before either wrote), which matches the plan's
    own stated scope rather than promising distributed locking it does not
    implement.

    A missing file behaves as "no credentials yet" (fresh install). A file
    that exists but is empty, truncated, or fails to decrypt raises
    :class:`~loom.connectors.encryption.DecryptionError` instead —
    silently treating either as empty would be indistinguishable from a
    logout nobody asked for.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        key_provider: KeyProvider | None = None,
        refresher: Refresher | None = None,
        clock: Clock | None = None,
        refresh_policy: RefreshPolicy | None = None,
    ) -> None:
        super().__init__(
            refresher=refresher, clock=clock, refresh_policy=refresh_policy
        )
        self._path = Path(path) if path else _default_store_path()
        self._envelope = Envelope(
            key_provider or default_key_provider(app_dir=self._path.parent)
        )
        self._io_lock = asyncio.Lock()

    async def _load(self) -> dict[str, StoredCredential]:
        if not self._path.exists():
            return {}
        raw = self._path.read_bytes()
        if not raw:
            raise DecryptionError(
                f"credential store at {self._path} exists but is empty. "
                "Refusing to treat that as 'no credentials' — restore a "
                "backup, or delete the file and re-run 'loom login'."
            )
        plaintext = self._envelope.decrypt(raw)
        try:
            data = json.loads(plaintext)
        except ValueError as exc:
            raise DecryptionError(
                f"credential store at {self._path} decrypted but is not "
                f"valid JSON: {exc}. Re-run 'loom login'."
            ) from exc
        return {name: StoredCredential.from_dict(entry) for name, entry in data.items()}

    async def _save(self, data: Mapping[str, StoredCredential]) -> None:
        plaintext = json.dumps({name: cred.to_dict() for name, cred in data.items()}).encode(
            "utf-8"
        )
        ciphertext = self._envelope.encrypt(plaintext)
        atomic_write_bytes(self._path, ciphertext, mode=0o600)

    async def _read(self, name: str) -> StoredCredential | None:
        async with self._io_lock:
            data = await self._load()
        return data.get(name)

    async def _write(self, name: str, credential: StoredCredential) -> None:
        async with self._io_lock:
            data = await self._load()
            data[name] = credential
            await self._save(data)

    async def _delete(self, name: str) -> None:
        async with self._io_lock:
            data = await self._load()
            if name in data:
                del data[name]
                await self._save(data)

    async def _list(self) -> list[str]:
        async with self._io_lock:
            data = await self._load()
        return sorted(data)

    async def peek_all(self) -> dict[str, StoredCredential]:
        """One decrypt for the whole store, rather than one per credential."""
        async with self._io_lock:
            return await self._load()

    def __repr__(self) -> str:
        return f"<EncryptedFileCredentialStore {self._path}>"


class KeyringCredentialStore(EncryptedFileCredentialStore):
    """The interactive default: master key in the OS keyring, payload in a file.

    Split for a concrete reason, not layering for its own sake: Windows
    Credential Manager caps a stored value near 2.5 KB, and a realistic
    OAuth token plus refresh token plus metadata clears that easily. A
    32-byte Fernet key never will. So only the key goes in the keyring; the
    encrypted payload — however large — goes in a file, exactly as
    ``EncryptedFileCredentialStore`` already handles it.

    Falls back to a generated, warned-about local file if the keyring is
    absent or unusable (headless Linux with no secret service, a locked
    keychain) — discoverable only by trying, so the fallback lives in
    :class:`~loom.connectors.encryption.KeyringKeyProvider`
    itself rather than here.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        service: str = "loom-credential-store",
        refresher: Refresher | None = None,
        clock: Clock | None = None,
        refresh_policy: RefreshPolicy | None = None,
    ) -> None:
        resolved_path = Path(path) if path else _default_store_path()
        key_provider = KeyringKeyProvider(
            service=service,
            fallback=GeneratedFileKeyProvider(resolved_path.parent / "credentials.key"),
        )
        super().__init__(
            resolved_path,
            key_provider=key_provider,
            refresher=refresher,
            clock=clock,
            refresh_policy=refresh_policy,
        )

    def __repr__(self) -> str:
        return f"<KeyringCredentialStore {self._path}>"


class LayeredCredentialStore:
    """A :class:`CredentialStore` that checks multiple stores in priority order.

    Implements the protocol directly rather than subclassing
    :class:`BaseCredentialStore`: expiry and refresh live in each layer's
    ``get()``, and reading through ``peek()`` would skip them.

    ``CredentialNotFound`` from an identity layer falls through to the next
    identity layer. :class:`AuthExpired` propagates — silently dropping from
    a caller-supplied token to an ambient one is an identity swap. Names in
    *required* that no identity layer can resolve raise ``AuthExpired``
    *before* ambient stores are consulted, so the engine parks on
    ``credential:<name>`` instead of continuing as a different principal.
    Ambient stores (typically ``Runtime.credentials``) still resolve names
    this run did not declare.
    """

    def __init__(
        self,
        *layers: CredentialStore,
        ambient: Sequence[CredentialStore] = (),
        required: frozenset[str] = frozenset(),
    ) -> None:
        self._layers = layers
        self._ambient = tuple(ambient)
        self._required = required

    async def get(self, name: str) -> Secret[str]:
        last_missing: CredentialNotFound | None = None
        for layer in self._layers:
            try:
                return await layer.get(name)
            except CredentialNotFound as exc:
                last_missing = exc
                continue
        if name in self._required:
            raise AuthExpired(
                f"credential '{name}' was declared for this run but is not "
                "available. Re-supply it via credentials= or a "
                "credential_resolver.",
                name=name,
            )
        for layer in self._ambient:
            try:
                return await layer.get(name)
            except CredentialNotFound as exc:
                last_missing = exc
                continue
        if last_missing is not None:
            raise last_missing
        raise CredentialNotFound(f"no credential named '{name}' is stored")

    async def put(self, name: str, credential: StoredCredential) -> None:
        writable = self._layers or self._ambient
        if not writable:
            raise CredentialNotFound("LayeredCredentialStore has no layers to write to")
        await writable[0].put(name, credential)

    async def forget(self, name: str) -> None:
        writable = self._layers or self._ambient
        if writable:
            await writable[0].forget(name)

    async def names(self) -> list[str]:
        seen: set[str] = set()
        for layer in (*self._layers, *self._ambient):
            seen.update(await layer.names())
        return sorted(seen)

    async def peek(self, name: str) -> StoredCredential | None:
        """The first layer that has a record for *name*, with no refresh."""
        for layer in (*self._layers, *self._ambient):
            peek = getattr(layer, "peek", None)
            if peek is None:
                continue
            found: StoredCredential | None = await peek(name)
            if found is not None:
                return found
        return None

    def __repr__(self) -> str:
        return f"<LayeredCredentialStore layers={len(self._layers)}>"


def _default_store_path() -> Path:
    base = os.environ.get("LOOM_HOME")
    directory = Path(base) if base else Path.home() / ".loom"
    return directory / "credentials.enc"
