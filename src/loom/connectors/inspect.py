"""Is this toolset connected here?

The question nothing could answer. A toolset's credential requirement lived in
three places that did not know about each other: a `credential_name` default
buried in one client file, a free-form `auth` dict on the manifest whose shape
varied per toolset, and whatever the process happened to have in its
environment. So `loom doctor` could say *"27 toolsets reachable"* and
*"1 credential stored"* and nothing could put the two together — and the coding
agent, which is the thing that most needs to know, was told neither.

`AuthSpec` made the requirement declarable. This reads it.

**Layer 1 discipline throughout.** Manifest metadata, `CredentialStore.peek`,
and a mapping of environment variables. No toolset module is imported, no
socket is opened, and no token is ever minted — so this is safe to call on
every authoring job, every `loom doctor`, and every turn of a prompt. The one
thing it must never do is the thing that would make it useful to hide: it does
not refresh, because a status command that changed what it reports by reporting
it is useless (the position `SubscriptionManager` already takes about
quarantine).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from loom.connectors.credentials import Peekable, RefreshPolicy, StoredCredential
from loom.runtime.clock import Clock, SystemClock
from loom.toolsets.manifest import AuthSpec, ToolsetManifest

__all__ = [
    "ConnectionInspector",
    "ConnectionState",
    "ConnectionStatus",
    "CredentialNeed",
    "looks_like_missing_credentials",
    "need_for",
    "preflight",
]


#: Exception *names* that mean "nothing authenticated this call".
#:
#: Matched by name rather than by class because each toolset raises its own —
#: `JiraAuthError`, `GraphAuthError`, `SlackAuthError` — and importing
#: twenty-seven client modules to catch them would undo the lazy catalogue this
#: layer exists for.
_AUTH_NAMES = ("AuthError", "AuthExpired", "CredentialNotFound", "Unauthorized")


def looks_like_missing_credentials(exc: BaseException) -> bool:
    """Whether *exc* is a client saying it has no credential.

    Narrow on purpose. A `TimeoutError` from the same call is a real failure,
    and reporting a broken thing as merely unconfigured is worse than the
    reverse — nobody looks. The `ValueError` arm is the shape every toolset
    constructor takes: the name of an environment variable and the word
    "required".
    """
    name = type(exc).__name__
    if any(marker in name for marker in _AUTH_NAMES):
        return True
    text = str(exc)
    return isinstance(exc, ValueError) and "required" in text and "_" in text


async def preflight(
    function: str,
    *,
    toolsets: Any,
    credentials: Any = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Why calling *function* would fail for want of a credential, or ``""``.

    The check that was not happening. A generated workflow calling
    ``jira_search_issues`` with nothing connected failed with
    ``ValueError: JIRA_URL is required (env var or base_url argument)`` — an
    environment variable name, raised deep inside the client, naming neither
    the toolset nor the thing that fixes it. The run had already started, so
    the failure also cost whatever ran before it.

    Deliberately **conservative**: it answers only when it is certain, because
    a preflight that refuses a working deployment is far worse than one that
    misses. It stays silent unless the manifest declares a credential
    requirement *and* nothing stored satisfies it *and* the environment does
    not either. A host resolving credentials some third way — a
    ``ConnectionBroker``, an injected client — trips none of those and is
    untouched.

    Cheap by construction: manifest metadata, one ``peek``, and a mapping
    lookup. No import, no socket, and no token minted.
    """
    resolve = getattr(toolsets, "manifest_of", None)
    manifest = resolve(function) if callable(resolve) else None
    if manifest is None:
        # A plain local `@step`. Nothing declares what it needs, and inventing
        # a requirement here would guess at the declaration a manifest exists
        # to make.
        return ""

    auth: AuthSpec = manifest.auth
    if auth.kind == "none":
        return ""

    if auth.credential and credentials is not None:
        # `peek`, never `get`: a check must not renew a credential, and must
        # not raise on an expired one — an expired token is the client's to
        # refresh and the engine's to park on, not this function's to fail.
        peek = getattr(credentials, "peek", None)
        if peek is None:
            # A store that cannot be inspected is one this cannot judge.
            return ""
        try:
            if await peek(auth.credential) is not None:
                return ""
        except Exception:
            return ""

    satisfied, missing = auth.satisfied_by(
        os.environ if environ is None else environ
    )
    if satisfied:
        return ""

    fix = (
        f"loom connect {auth.credential}"
        if auth.credential
        else f"set {', '.join(missing)}"
    )
    where = f" (provider: {auth.provider})" if auth.provider else ""
    return (
        f"{manifest.id} is not connected{where} — {function} needs "
        f"{', '.join(missing) or 'a credential'}. Run: {fix}"
    )


class CredentialNeed(BaseModel):
    """What one `CredentialStore` name is for, gathered across the toolsets.

    The join `loom connect jira` did not have. It looked the *credential* name
    up in the OAuth provider registry, found nothing, and refused with
    *"'jira' is not a known provider"* — Jira's provider is `atlassian`.

    Keyed by credential rather than by toolset because several toolsets share
    one: five Google toolsets read `google` and six Graph ones read
    `microsoft`.
    """

    credential: str
    provider: str = ""
    kind: str = "none"
    toolsets: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    """The **union** across every toolset that reads this credential.

    Deliberately the union: `GoogleAuth` caches one token and merges each
    toolset's scopes into it, because a token minted for Calendar alone is a
    403 the first time a Drive call is made — a failure that reads as a broken
    credential rather than a narrow one. Minting it with the whole set is the
    same reasoning applied one step earlier.
    """
    setup_url: str = ""
    docs_url: str = ""

    @property
    def known(self) -> bool:
        return bool(self.toolsets)


def need_for(credential: str, catalog: Any) -> CredentialNeed:
    """What *credential* is for, from the manifests that declare it.

    Returns an empty `CredentialNeed` for a name no toolset reads — `loom
    connect` has always accepted an arbitrary name with explicit endpoints, and
    refusing one now would break every deployment doing that.
    """
    ids = getattr(catalog, "catalogue_ids", None)
    listed = list(ids()) if callable(ids) else list(catalog.toolset_ids)

    toolsets: list[str] = []
    scopes: set[str] = set()
    provider = kind = setup_url = docs_url = ""
    for toolset_id in listed:
        manifest = catalog.get(toolset_id)
        if manifest is None or manifest.auth.credential != credential:
            continue
        toolsets.append(toolset_id)
        scopes.update(manifest.required_scopes())
        provider = provider or manifest.auth.provider
        kind = kind or manifest.auth.kind
        setup_url = setup_url or manifest.auth.setup_url
        docs_url = docs_url or manifest.auth.docs_url

    return CredentialNeed(
        credential=credential,
        provider=provider,
        kind=kind or "none",
        toolsets=tuple(toolsets),
        scopes=tuple(sorted(scopes)),
        setup_url=setup_url,
        docs_url=docs_url,
    )


class ConnectionState(StrEnum):
    """How close this toolset is to being callable.

    Six rather than "connected / not", because the four in the middle lead to
    different actions and collapsing them is how advice becomes useless.
    """

    CONNECTED = "connected"
    """A stored credential exists and is not near expiry."""

    DUE = "due"
    """Stored, usable, and the next call will try to renew it.

    Worth its own state for the reason `loom whoami` reports it: the credential
    *works*, so this is never an error — but an operator seeing `due` on the
    same credential repeatedly is watching renewal fail, hours before it
    becomes `expired` and something breaks.
    """

    EXPIRED = "expired"
    """Stored and past its expiry. A refresh may still rescue it; a read must
    not attempt one, so this is what a read reports."""

    ENV = "env"
    """No stored credential, but the environment satisfies what the client
    reads.

    Deliberately not folded into `CONNECTED`. The two look identical to a
    caller and differ in what to *do*: there is nothing to renew here and
    nothing to disconnect, and offering an OAuth flow for a toolset that is
    already working from `.env` is the *"loom refresh --all"* failure — advice
    for a state the machine is not in.
    """

    MISSING = "missing"
    """Nothing stored and the environment does not satisfy it."""

    NONE = "none"
    """This toolset needs no credential at all."""

    @property
    def usable(self) -> bool:
        """Whether a call would have something to authenticate with."""
        return self in (
            ConnectionState.CONNECTED,
            ConnectionState.DUE,
            ConnectionState.ENV,
            ConnectionState.NONE,
        )


class ConnectionStatus(BaseModel):
    """What a toolset needs, and how much of it this machine has.

    Carries **no token and no secret value** — names, states and expiries only,
    the shape `loom refresh`'s JSON already takes. It crosses the facade to a
    CLI, to an MCP client, and into a model's context.
    """

    toolset: str
    state: ConnectionState
    method: str = "none"
    """The wire scheme, from `AuthSpec.kind`."""

    credential: str = ""
    """The `CredentialStore` key, when the client reads one."""
    provider: str = ""
    """The OAuth provider that can obtain it, when a browser flow can."""
    scopes: tuple[str, ...] = ()

    present_fields: tuple[str, ...] = ()
    """Required environment variables this process has."""
    missing_fields: tuple[str, ...] = ()
    """Required environment variables it does not.

    Required only: an optional one that is absent is not a gap, and listing it
    turns a working toolset into a list of complaints.
    """

    expires_at: datetime | None = None
    setup_url: str = ""
    docs_url: str = ""
    how: str = ""
    """The one command that changes this state, or `""` when none does."""

    detail: str = ""
    """One sentence a person can act on."""

    @property
    def usable(self) -> bool:
        return self.state.usable


class ConnectionInspector:
    """Answers `ConnectionStatus` for a toolset, from declarations alone.

    Composed rather than global: a host with its own store, its own
    environment, or a `ManualClock` passes them, and nothing here reads a
    process default it was not given except `os.environ`, which is what an
    unconfigured caller means.
    """

    def __init__(
        self,
        catalog: Any,
        store: Any = None,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        policy: RefreshPolicy | None = None,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._environ = os.environ if environ is None else environ
        self._clock = clock or SystemClock()
        # The store's own policy when it has one, so "due" here and "renew now"
        # there cannot disagree about the same credential.
        self._policy = (
            policy or getattr(store, "refresh_policy", None) or RefreshPolicy()
        )

    async def status(self, toolset_id: str) -> ConnectionStatus:
        manifest = self._catalog.get(toolset_id)
        if manifest is None:
            raise KeyError(f"no toolset {toolset_id!r}")
        return await self._status(manifest)

    async def all(self) -> list[ConnectionStatus]:
        """Every toolset that may be named, in catalogue order.

        `catalogue_ids()` where the registry has it: what a workflow may be
        written against is the set worth reporting on, and it is not the same
        as what happens to be registered. Falls back to `toolset_ids`, which
        every `ToolsetCatalog` has — a bare catalogue, including the built-in
        tier itself, has no `list_toolsets`.
        """
        ids = getattr(self._catalog, "catalogue_ids", None)
        listed = list(ids()) if callable(ids) else list(self._catalog.toolset_ids)
        out: list[ConnectionStatus] = []
        for toolset_id in listed:
            manifest = self._catalog.get(toolset_id)
            if manifest is not None:
                out.append(await self._status(manifest))
        return out

    # -- internals ---------------------------------------------------------

    async def _status(self, manifest: ToolsetManifest) -> ConnectionStatus:
        auth: AuthSpec = manifest.auth
        base = {
            "toolset": manifest.id,
            "method": auth.kind,
            "credential": auth.credential,
            "provider": auth.provider,
            "scopes": manifest.required_scopes(),
            "setup_url": auth.setup_url,
            "docs_url": auth.docs_url,
        }

        if auth.kind == "none":
            return ConnectionStatus(
                state=ConnectionState.NONE,
                detail="needs no credential",
                **base,
            )

        stored = await self._peek(auth.credential)
        if stored is not None:
            state, detail = self._judge(stored, manifest.id)
            return ConnectionStatus(
                state=state,
                expires_at=stored.expires_at,
                detail=detail,
                how=self._how(auth, state),
                **base,
            )

        # Asked of the spec, not counted here: several toolsets accept
        # alternative credential *modes*, and a rule that wants every required
        # field reports a Google deployment holding a valid refresh token as
        # missing an access token it does not need. `AuthSpec.satisfied_by`
        # owns that, and answers with the nearest mode's shortfall.
        ok, missing = auth.satisfied_by(self._environ)
        present = tuple(
            f.name for f in auth.fields if self._environ.get(f.name)
        )

        if ok and auth.fields:
            state = ConnectionState.ENV
            detail = "configured from the environment"
        else:
            state = ConnectionState.MISSING
            detail = (
                f"not configured — needs {', '.join(missing)}"
                if missing
                else "not configured"
            )

        return ConnectionStatus(
            state=state,
            present_fields=present,
            missing_fields=missing,
            detail=detail,
            how=self._how(auth, state),
            **base,
        )

    async def _peek(self, credential: str) -> StoredCredential | None:
        """The stored record, without triggering a refresh.

        `peek` rather than `get`, and the difference is the whole reason this
        module is safe to call from a prompt: `get` renews a credential that is
        due and raises on one that has expired. Reporting a state must not
        change it, and must not fail because of it.
        """
        if not credential or self._store is None:
            return None
        if not isinstance(self._store, Peekable):
            return None
        try:
            return await self._store.peek(credential)
        except Exception:
            # An unreadable credential file is `loom whoami`'s finding to
            # report in detail. Here it only needs to not be an exception in
            # the middle of building a prompt.
            return None

    def _judge(self, stored: StoredCredential, toolset: str) -> tuple[ConnectionState, str]:
        if stored.is_expired(self._clock):
            return (
                ConnectionState.EXPIRED,
                "stored credential has expired — renewing may still rescue it",
            )
        if self._policy.is_due(stored, self._clock):
            return (
                ConnectionState.DUE,
                "stored and usable; the next call will renew it",
            )
        return ConnectionState.CONNECTED, f"connected as {toolset}"

    @staticmethod
    def _how(auth: AuthSpec, state: ConnectionState) -> str:
        """The one command that changes this state, or nothing.

        Empty is an answer. A toolset with no `credential` has no store path,
        so `loom connect` would write a token its client never reads — naming
        it here would be advice with no move behind it, which is the failure
        `loom refresh --all` and `loom runs --status running` were both fixed
        for.
        """
        if not auth.credential:
            return ""
        if state is ConnectionState.EXPIRED:
            return f"loom refresh {auth.credential}"
        if state is ConnectionState.MISSING:
            return f"loom connect {auth.credential}"
        # Nothing for ENV: it works. Offering a connect flow to a toolset that
        # is already authenticated from the environment is advice for a state
        # the machine is not in, which is what makes people stop reading these.
        return ""
