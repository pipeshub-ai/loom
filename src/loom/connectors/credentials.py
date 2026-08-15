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
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    "CredentialStore",
    "EncryptedFileCredentialStore",
    "KeyringCredentialStore",
    "LayeredCredentialStore",
    "MemoryCredentialStore",
    "Peekable",
    "Refresher",
    "StoredCredential",
    "credential_store_scope",
    "current_credential_store",
    "resolve_bearer_token",
]


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

    def is_expired(self, clock: Clock | None = None) -> bool:
        """Whether ``expires_at`` has passed, per an injected clock.

        Same reasoning as ``Credential.expired`` in ``toolsets/connections.py``
        — a credential store is read from inside a run, so its notion of
        "now" must be replayable, not a call to the wall clock.
        """
        if self.expires_at is None:
            return False
        now = (clock or SystemClock()).now()
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return now >= expires_at

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
            "scopes": sorted(self.scopes),
            "token_type": self.token_type,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StoredCredential:
        expires_at = data.get("expires_at")
        refresh_token = data.get("refresh_token")
        return cls(
            token=Secret(data["token"]),
            refresh_token=Secret(refresh_token) if refresh_token else None,
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
            scopes=frozenset(data.get("scopes", [])),
            token_type=data.get("token_type", "bearer"),
            metadata=dict(data.get("metadata", {})),
        )


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

    def __init__(self, *, refresher: Refresher | None = None, clock: Clock | None = None) -> None:
        self._refresher = refresher
        self.clock = clock or SystemClock()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = self._locks[name] = asyncio.Lock()
        return lock

    async def get(self, name: str) -> Secret[str]:
        stored = await self._read(name)
        if stored is None:
            raise CredentialNotFound(f"no credential named '{name}' is stored")
        if not stored.is_expired(self.clock):
            return stored.token

        async with self._lock_for(name):
            # Another task in this process may have refreshed while this one
            # waited for the lock — re-read rather than refreshing again.
            stored = await self._read(name)
            if stored is None:
                raise CredentialNotFound(f"no credential named '{name}' is stored")
            if not stored.is_expired(self.clock):
                return stored.token
            if self._refresher is None:
                raise AuthExpired(
                    f"credential '{name}' has expired and no refresher is "
                    f"configured. Run 'loom connect {name}' to reauthorize.",
                    name=name,
                )
            refreshed = await self._refresher.refresh(name, stored)
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
        self, *, refresher: Refresher | None = None, clock: Clock | None = None
    ) -> None:
        super().__init__(refresher=refresher, clock=clock)
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
    ) -> None:
        super().__init__(refresher=refresher, clock=clock)
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
    ) -> None:
        resolved_path = Path(path) if path else _default_store_path()
        key_provider = KeyringKeyProvider(
            service=service,
            fallback=GeneratedFileKeyProvider(resolved_path.parent / "credentials.key"),
        )
        super().__init__(
            resolved_path, key_provider=key_provider, refresher=refresher, clock=clock
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
            found = await peek(name)
            if found is not None:
                return found
        return None

    def __repr__(self) -> str:
        return f"<LayeredCredentialStore layers={len(self._layers)}>"


def _default_store_path() -> Path:
    base = os.environ.get("LOOM_HOME")
    directory = Path(base) if base else Path.home() / ".loom"
    return directory / "credentials.enc"
