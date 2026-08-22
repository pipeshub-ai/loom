"""Assembling a toolset client from values decided elsewhere.

:mod:`loom.toolsets.resolution` decides *where a value comes from*. This decides
*how a client is built from it* — a different job, and one that was previously
done twenty-seven times, once per toolset, by a module-level singleton reading
``os.environ`` on first use and caching the result for the life of the process.

That singleton is what made a credential a property of the *process*: one set,
fixed at whatever the first call saw, so ``loom connect jira`` afterwards changed
nothing and two tenants in one process were impossible. It is gone. Construction
now happens per run, from a :class:`~loom.toolsets.resolution.ToolsetSession`
bound for that run.

**One generic factory, twenty-seven toolsets.** Everything it needs is declared
on the manifest: ``AuthSpec.client`` names the class, ``AuthField.arg`` maps a
value to a constructor keyword, and ``AuthSpec.credentials`` names the holder for
the twelve clients that take an ``auth`` object rather than loose values. Adding
a toolset is a manifest entry; it is not a factory.

**The bearer token stays per request**, and that is a correctness property rather
than a compatibility one. Six clients resolve a stored token on *every* call, so
a credential the store refreshes during a long run is picked up immediately. What
changes is how they reach it: they used to read a process-wide contextvar from
inside their own request path, and now they are handed a :class:`TokenSource`.
Same freshness, no ambient lookup — the client depends on what it was given and
on nothing else.
"""

from __future__ import annotations

import contextvars
import importlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, overload, runtime_checkable

from loom.core.exceptions import ConfigurationError, CredentialNotFound
from loom.toolsets.manifest import AuthSpec
from loom.toolsets.resolution import ResolvedCredentials, ToolsetSession

__all__ = [
    "StoreTokenSource",
    "TokenSource",
    "build_client",
    "client_for",
    "current_toolset_session",
    "forget_shared_auth",
    "shared_auth",
    "toolset_session_scope",
    "use_clients",
]


@runtime_checkable
class TokenAuth(Protocol):
    """Something a client can ask for the headers its next request needs.

    Written down rather than invented: three classes already satisfy it —
    ``GoogleAuth``, ``MicrosoftAuth``, ``ZoomAuth`` — and every client that
    takes one calls exactly ``await self._auth.headers()``. A shape three
    implementations share and nothing names is a port that exists and cannot be
    implemented against, which is what a host writing a fourth had to work
    from.

    Two methods, not more. Minting, caching, locking and refresh are each
    implementation's own business; what a client needs to know is what to send
    now, and how to say that what it was given has stopped working.
    """

    async def headers(self) -> dict[str, str]:
        """The headers the next request should carry, minting if it must."""
        ...

    def invalidate(self) -> None:
        """Discard any cached token.

        Called when a request comes back 401 despite headers that looked
        valid — which is the only way a caller learns that a token expired
        between being handed over and being used.
        """
        ...


@runtime_checkable
class TokenSource(Protocol):
    """Somewhere a client can ask for a bearer token, per request.

    One method, because the only thing a client needs to know is what the token
    is *now*. Where it is stored, whether it was refreshed a second ago, and what
    happens when it cannot be — none of that belongs in a vendor's HTTP client.
    """

    async def token(self) -> str | None:
        """The current token, or ``None`` when there is none.

        ``None`` is not an error: it means this deployment authenticates the
        other way — Basic from a declared email and API token, say — and the
        client should use what it was constructed with.
        """
        ...


@dataclass(frozen=True)
class StoreTokenSource:
    """A :class:`TokenSource` over a ``CredentialStore`` key.

    Reads on every call rather than caching, which is the whole point: a run
    parked two hours on an approval, whose token the store has since refreshed,
    must not still be sending the one that was current when its client was
    built.
    """

    name: str
    store: Any = None
    """The store to read. ``None`` uses whichever is bound to this run, which is
    what the engine binds and what ``loom connect`` writes to."""

    async def token(self) -> str | None:
        from loom.connectors.credentials import (
            credential_store_scope,
            resolve_bearer_token,
        )

        if not self.name:
            return None
        if self.store is None:
            return await resolve_bearer_token(self.name)
        with credential_store_scope(self.store):
            return await resolve_bearer_token(self.name)


_SESSION: contextvars.ContextVar[ToolsetSession | None] = contextvars.ContextVar(
    "loom_current_toolset_session", default=None
)

# `None` rather than `{}` as the default: a mutable default on a ContextVar is
# one object shared by every context that never sets it, so a caller mutating
# what it reads would reach every other run in the process — the class of bug
# this whole module removes.
_OVERRIDES: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar(
    "loom_toolset_client_overrides", default=None
)


@contextmanager
def use_clients(**clients: Any) -> Iterator[None]:
    """Bind already-built clients for this block, bypassing construction.

    Two callers want this and neither is served by supplying credentials. A test
    needs a client wired to a fake transport, which no amount of configuration
    produces; a host that builds its own — a shared connection pool, a tenant
    cache — needs somewhere to hand the result in.

    It replaces assigning a module-level ``_default_client``, which is how the
    tests used to do it: that reached into another module's global, leaked into
    every test after it unless a ``finally`` put it back, and only worked
    because the thing being replaced was process-wide in the first place. This
    is scoped, so it cannot leak.
    """
    token = _OVERRIDES.set({**(_OVERRIDES.get() or {}), **clients})
    try:
        yield
    finally:
        _OVERRIDES.reset(token)


def current_toolset_session() -> ToolsetSession | None:
    """The session bound to this run, if any."""
    return _SESSION.get()


@contextmanager
def toolset_session_scope(session: ToolsetSession | None) -> Iterator[None]:
    """Bind *session* for this block.

    Ambient because the 390 call sites are ``@step`` functions carrying business
    arguments only — there is nowhere for a client to enter as a parameter, and
    widening every tool's signature to add one would put a plumbing concern into
    every workflow that calls a tool.

    A contextvar rather than a module global, and the difference is the entire
    point: a contextvar is scoped to the task that set it, so two runs resolve
    against their own sessions concurrently. The global it replaces could hold
    exactly one credential set per process.
    """
    token = _SESSION.set(session)
    try:
        yield
    finally:
        _SESSION.reset(token)


#: How many distinct identities an auth object is cached for.
#:
#: Bounded because the key is derived from credentials, and a host serving many
#: tenants would otherwise grow this for the life of the process. Small on
#: purpose: the number of *accounts* a deployment authenticates as is a handful,
#: not a function of its traffic.
_AUTH_CACHE_MAX = 32

_AUTH_CACHE: dict[tuple[str, str], Any] = {}


def _identity(values: Mapping[str, str]) -> str:
    """A stable fingerprint of the values that authenticate.

    Hashed rather than kept, because this is a cache key and a cache key gets
    logged, repr'd and put in error messages. The inputs are secrets.

    Sorted so two mappings that differ only in insertion order share an entry,
    and over the whole mapping rather than a chosen subset: deciding which
    fields "really" identify an account is a guess, and getting it too narrow
    shares a token between two of them.
    """
    import hashlib

    digest = hashlib.sha256()
    for name in sorted(values):
        digest.update(name.encode())
        digest.update(b"\x00")
        digest.update(values[name].encode())
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def shared_auth(auth_cls: Any, values: Mapping[str, str], scopes: Sequence[str]) -> Any:
    """One auth object per *identity*, rather than one per process or per call.

    Sharing is deliberate and predates this: five Google toolsets authenticate
    against the same account, and one cached token serving all five is a real
    saving — a service account otherwise signs a fresh JWT for each. What was
    wrong is that the sharing was keyed on **nothing**. A module-level global
    holds one auth for the whole process, so two tenants are handed each other's
    token, and the only reason that was survivable is that nothing else could
    hold two credential sets either.

    Keyed on the credentials, the saving survives and the leak does not: the
    same account shares, a different one cannot. `add_scopes` still widens a
    shared entry, which is what stops the second toolset to be used from getting
    a token minted without its scopes — accumulation *within* one identity is
    the behaviour that fix exists for.
    """
    key = (f"{auth_cls.__module__}:{auth_cls.__qualname__}", _identity(values))
    existing = _AUTH_CACHE.get(key)
    if existing is not None:
        widen = getattr(existing, "add_scopes", None)
        if callable(widen) and scopes:
            widen(list(scopes))
        return existing

    auth = auth_cls.from_values(values, scopes=scopes)
    if len(_AUTH_CACHE) >= _AUTH_CACHE_MAX:
        # Oldest first. A cache that refuses to evict is a leak with a ceiling
        # nobody set.
        _AUTH_CACHE.pop(next(iter(_AUTH_CACHE)))
    _AUTH_CACHE[key] = auth
    return auth


def forget_shared_auth() -> None:
    """Drop every cached auth. For tests, and after a credential rotation."""
    _AUTH_CACHE.clear()


def _import(path: str) -> Any:
    module_name, _, symbol = path.partition(":")
    if not symbol:
        raise ConfigurationError(
            f"{path!r} is not a client path — expected 'module:Class'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(f"cannot import {module_name!r}: {exc}") from exc
    try:
        return getattr(module, symbol)
    except AttributeError as exc:
        raise ConfigurationError(f"{module_name!r} has no {symbol!r}") from exc


def _keywords(spec: AuthSpec, values: Mapping[str, str]) -> dict[str, Any]:
    """Constructor keywords, from the declared field-to-argument mapping.

    Two fields may share one ``arg`` — Slack's client reads ``SLACK_BOT_TOKEN or
    SLACK_TOKEN`` — and **the first declared one that has a value wins**. Not the
    last, which is what a plain comprehension over the fields would give, and
    which would silently prefer the fallback over the primary.
    """
    keywords: dict[str, Any] = {}
    for field in spec.fields:
        if not field.arg or field.arg in keywords:
            continue
        value = values.get(field.name)
        if value:
            keywords[field.arg] = value
    return keywords


def build_client(spec: AuthSpec, resolved: ResolvedCredentials) -> Any:
    """Construct the client *spec* names, from *resolved*.

    Raises :class:`ConfigurationError` when something is missing, rather than
    building a client that will 401 later against the vendor. The failure a
    person can act on names the variable and where it was looked for; the one
    they cannot names somebody else's API.
    """
    if not spec.client:
        raise ConfigurationError(
            f"{resolved.toolset!r} declares no client class, so nothing can "
            "build one. Set AuthSpec.client on its manifest."
        )
    if resolved.missing:
        # `CredentialNotFound`, which `looks_like_missing_credentials` already
        # recognises, rather than the `MissingCredentials` a caller finally
        # sees. The difference matters: `runtime/context.py` wraps an
        # auth-shaped failure *after* it happens, building the message from the
        # manifest — the toolset, its provider, the command that fixes it — and
        # does so only when `explain_credentials` is on. Raising the wrapped
        # type here would skip the message and the dial both.
        raise CredentialNotFound(
            f"{resolved.toolset} is not configured: {', '.join(resolved.missing)} "
            f"{'are' if len(resolved.missing) > 1 else 'is'} not set. "
            f"Looked in: {', '.join(sorted({*resolved.sources.values()})) or 'nothing'}.",
        )

    cls = _import(spec.client)
    if spec.credentials:
        # The auth-object family. `from_values` is the one construction path
        # these classes publish; it maps every variable through the credentials
        # holder's own `from_env`, including the multi-mode logic
        # `AuthField.mode` mirrors, and wraps it in the object that actually
        # mints tokens.
        #
        # Naming the *holder* here instead was a shipped bug: the client's
        # `auth=` wants something with `headers()`, a bare dataclass of values
        # has none, and nothing checks — so twelve toolsets constructed cleanly
        # and raised `AttributeError` on their first request.
        auth = shared_auth(
            _import(spec.credentials), resolved.values, spec.scopes
        )
        # Config travels too. `credentials` names what authenticates; `arg`
        # routes what sits beside it — `MS_TEAMS_USER -> user_id` names whose
        # chats an app-only deployment acts on, and is not a secret. Returning
        # `cls(auth=auth)` alone dropped every one of those, which is invisible
        # until a Graph call resolves `/me` under a token that has no `me`.
        return cls(auth=auth, **_keywords(spec, resolved.values))

    keywords = _keywords(spec, resolved.values)
    if spec.credential and _accepts_a_token_source(cls):
        keywords["token_source"] = StoreTokenSource(spec.credential)
    return cls(**keywords)


def _accepts_a_token_source(cls: Any) -> bool:
    """Whether this client takes an injected bearer-token source.

    Asked rather than assumed. Passing it unconditionally is what a factory
    written against one migrated client does, and it raised ``TypeError`` at
    construction for the fourteen that still resolve their own key from an
    ambient contextvar — every credentialed toolset except the one it was
    written against.

    Those still work: they read the same store, from the same place, exactly as
    before. What they do not yet get is the injection, and this returns ``False``
    for them until they do.
    """
    import inspect

    try:
        return "token_source" in inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return False


_Client = TypeVar("_Client")


@overload
async def client_for(
    toolset: str, cls: type[_Client], *, session: ToolsetSession | None = None
) -> _Client: ...


@overload
async def client_for(
    toolset: str, cls: None = None, *, session: ToolsetSession | None = None
) -> Any: ...


async def client_for(
    toolset: str, cls: Any = None, *, session: ToolsetSession | None = None
) -> Any:
    """The client for *toolset*, built from this run's session.

    *cls* is the expected type. It is not used to construct anything — the
    manifest decides that — but naming it keeps the call site typed, and
    without it every tool returning a typed model would return ``Any`` and
    silently lose its annotation. ``tests/test_toolset_construction.py``
    asserts each call site's class is the one its manifest declares, so the
    two cannot drift.

    Replaces ``get_default_client()``, and the difference is not the spelling:
    that returned a process-wide singleton built once from the first environment
    it ever saw. This resolves per call against the session bound to this run, so
    a credential connected or refreshed since is picked up, and two runs in one
    process can use different ones.
    """
    override = (_OVERRIDES.get() or {}).get(toolset)
    if override is not None:
        return override

    from loom.toolsets.registry import get_catalog

    manifest = get_catalog().get(toolset)
    if manifest is None:
        raise ConfigurationError(
            f"no toolset named {toolset!r} is registered in this process"
        )
    spec = manifest.auth
    active = session or current_toolset_session() or ToolsetSession()
    resolved = await active.resolve(toolset, spec)
    return build_client(spec, resolved)
