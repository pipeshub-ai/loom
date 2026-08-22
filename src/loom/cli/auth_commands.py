"""``loom login`` / ``logout`` / ``whoami`` / ``connect``.

Two things get authenticated by these four commands, through the same
underlying flow:

``login``
    This CLI, against a LOOM server (``--server``, matching every other
    command's flag). Stored under ``loom:<server>`` so multiple servers keep
    independent logins, and picked up automatically by :func:`server_token_provider`
    the next time any command passes ``--server`` to that same URL.
``connect <name>``
    A named credential a workflow's toolsets read via
    ``ctx.credentials.get(name)`` — the exact name a ``CredentialNotFound``
    or an ``AuthExpired`` park names in its hint. Reauthorizing and storing
    under that same name is what a parked run's ``credential:<name>`` event
    resumes against once delivered.

Both go through :func:`_connect`, which picks a flow — browser PKCE by
default, the device-code fallback on a headless machine or with
``--device`` — and hands the result to a
:class:`~loom.connectors.credentials.CredentialStore`.
Endpoint configuration (token endpoint, client id, ...) is read from flags
first, then ``LOOM_LOGIN_*`` / ``LOOM_CONNECT_<NAME>_*`` environment
variables, so a script can drive this non-interactively without a
committed secret.

Every credential is stored with enough of that configuration in its own
``metadata`` for :class:`~loom.connectors.oauth_client.MetadataRefresher`
to renew it later without the CLI needing to remember per-name server
config across invocations.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from loom.cli.commands import printer_for, run_async
from loom.cli.output import Exit, Printer
from loom.connectors.flows import (
    ConnectEvent,
    ConnectRequest,
    LoopbackListener,
    OAuthBrowserFlow,
    OAuthTarget,
    is_headless,
    run_device_authorization,
)
from loom.core.exceptions import AuthExpired, ConfigurationError, CredentialNotFound
from loom.toolsets.manifest import AuthSpec

if TYPE_CHECKING:
    from loom.connectors.credentials import CredentialStore, StoredCredential
    from loom.connectors.oauth_client import OAuthClient

#: The flow machinery moved to ``loom.connectors.flows`` so the session, the
#: MCP server and the coding agent could reach it — none of them has an
#: ``argparse.Namespace``, and every line of it used to require one. These names
#: are what this module has always called them, kept so a caller (and
#: ``tests/test_cli_auth.py``, which is the regression bar for the extraction)
#: does not have to care that the implementation moved.
_OAuthTarget = OAuthTarget
_is_headless = is_headless

__all__ = [
    "cmd_connect",
    "cmd_disconnect",
    "cmd_login",
    "cmd_logout",
    "cmd_providers",
    "cmd_whoami",
    "server_token_provider",
]

#: Matches every other command's own default — ``loom serve``'s.
DEFAULT_SERVER = "http://127.0.0.1:8000"

#: Loopback port the PKCE redirect listener binds. Stable so an OAuth client
#: can register ``http://127.0.0.1:8931/callback`` once. Same port the Google
#: setup helper uses; the path (``/callback`` vs ``/``) keeps the two URIs
#: distinct. Override with ``LOOM_OAUTH_REDIRECT_PORT`` in ``.env`` (or
#: ``--redirect-port``); a busy port is an error, not a silent hop to another.
DEFAULT_REDIRECT_PORT = 8931
REDIRECT_PORT_ENV = "LOOM_OAUTH_REDIRECT_PORT"


# ---------------------------------------------------------------------------
# The store this CLI reads and writes
# ---------------------------------------------------------------------------


def _store(lock: Any | None = None, *, owner: str = "") -> CredentialStore:
    """The CLI's own credential store: keyring-backed, self-refreshing.

    A module-level function rather than a singleton so tests can monkeypatch
    it wholesale (typically with a shared in-memory store instance, since
    each call otherwise gets an independent one backed by the real
    keyring/file — see ``tests/test_cli_auth.py``).

    *lock* is a :class:`~loom.stores.base.LockProvider` — every LOOM store
    implements one — and is what makes refresh safe across processes. Without
    it, a ``loom serve`` and a ``loom run`` sharing this credential file can
    refresh the same credential simultaneously; servers that rotate the refresh
    token on use then invalidate each other's, which surfaces as an
    intermittent ``invalid_grant`` that only happens under concurrency. It is
    optional because most commands have no store to borrow one from, and one
    process refreshing alone was always correct.
    """
    from loom.connectors.credentials import KeyringCredentialStore
    from loom.connectors.oauth_client import MetadataRefresher

    # Before the store is built, because which key source it uses is read at
    # construction. `loom run` happened to work — `targets.resolve()` discovers
    # the project (and so loads `.env`) before it asks for a store — while
    # `loom connect` and `loom doctor` come straight here, so a project pinning
    # `LOOM_CREDENTIAL_BACKEND` in `.env` would have been honoured by one
    # command and ignored by the next. One reader, one answer.
    _load_project_env()

    store = KeyringCredentialStore()
    # Doc'd wiring in oauth_client.py: the refresher needs the store it is
    # refreshing for (peek/put during a lock race), and the store needs its
    # refresher — so one is built, then wired into the other.
    store._refresher = MetadataRefresher(
        store=store,
        lock=lock,
        owner=owner,
        # One policy for both halves: the store decides when a credential is
        # due, and the refresher decides whether someone else already handled
        # it. Two policies would let those disagree.
        refresh_policy=store.refresh_policy,
    )
    return store


def credential_store_for(runtime: Any) -> CredentialStore:
    """This CLI's credential store, locked against *runtime*'s store.

    Used where the CLI builds a Runtime, so that (a) a workflow's toolsets can
    read what ``loom connect`` stored, and (b) concurrent refreshes across
    processes serialize through the same lock every LOOM store already provides.
    """
    from loom.stores.base import LockProvider

    store = getattr(runtime, "store", None)
    lock = store if isinstance(store, LockProvider) else None
    return _store(lock, owner=getattr(runtime, "node_id", "") or "")


def _server_credential_name(server: str) -> str:
    return f"loom:{server.rstrip('/')}"


def server_token_provider(server: str) -> Callable[[bool], Awaitable[str | None]]:
    """A ``LoomClient`` ``token_provider`` reading this CLI's stored login.

    Returns ``None`` when nothing is stored or it cannot be refreshed — an
    unauthenticated server needs no token, and ``LoomClient`` sends no
    ``Authorization`` header when the provider returns a falsy value, so
    talking to a server with no identity configured stays unaffected.
    """
    name = _server_credential_name(server)

    async def _provider(force_refresh: bool) -> str | None:
        del force_refresh  # CredentialStore.get() already refreshes on expiry
        store = _store()
        try:
            secret = await store.get(name)
        except (CredentialNotFound, AuthExpired):
            return None
        return secret.reveal()

    return _provider


# ---------------------------------------------------------------------------
# PKCE redirect port
# ---------------------------------------------------------------------------


def _load_project_env() -> None:
    """Load the project's ``.env``, through the one reader that knows where it is.

    :func:`loom.cli.config.load_dotenv` reads the file beside the nearest
    ``pyproject.toml`` and never overrides a real environment variable, so
    exporting a value for one command still wins.
    """
    from loom.cli.config import ProjectConfig, load_dotenv

    load_dotenv(ProjectConfig.discover(load_env=False).root)


def _dotenv_get(name: str) -> str | None:
    """``name`` from the project's ``.env``, if there is one.

    This used to read ``Path.cwd()/.env`` with its own parser, so ``loom
    connect`` run from a subdirectory read a different file — or none — than
    ``loom run`` did from the same project. Two readers for one file is one too
    many; two *answers* is a bug.
    """
    _load_project_env()
    return os.environ.get(name) or None


def resolve_redirect_port(explicit: int | None = None) -> int:
    """The loopback port the PKCE listener will bind.

    Precedence: ``explicit`` (``--redirect-port``) > ``$LOOM_OAUTH_REDIRECT_PORT``
    > the same key in ``.env`` > :data:`DEFAULT_REDIRECT_PORT`. ``0`` is
    ephemeral, for tests; it is never the documented default.
    """
    if explicit is not None:
        port = explicit
    else:
        raw = os.environ.get(REDIRECT_PORT_ENV) or _dotenv_get(REDIRECT_PORT_ENV)
        if raw is None:
            return DEFAULT_REDIRECT_PORT
        try:
            port = int(raw)
        except ValueError as exc:
            raise ConfigurationError(
                f"{REDIRECT_PORT_ENV}={raw!r} is not a port number"
            ) from exc
    if port != 0 and not (1 <= port <= 65535):
        raise ConfigurationError(f"OAuth redirect port {port} is not in 1..65535")
    return port


# ---------------------------------------------------------------------------
# Resolving what to authenticate against
# ---------------------------------------------------------------------------


def env_prefix_for(name: str) -> str:
    """``LOOM_CONNECT_<NAME>``, name slugged into a valid env var segment."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return f"LOOM_CONNECT_{slug}"


def _resolve_target(
    args: argparse.Namespace,
    *,
    name: str,
    env_prefix: str,
    provider_hint: str | None = None,
) -> _OAuthTarget:
    def flag(attr: str) -> str | None:
        value = getattr(args, attr, None)
        return str(value) if value else None

    def pick(attr: str, suffix: str) -> str | None:
        return flag(attr) or os.environ.get(f"{env_prefix}_{suffix}") or None

    provider = None
    need = None
    if provider_hint:
        from loom.connectors.inspect import need_for
        from loom.connectors.oauth_providers import get_oauth_provider
        from loom.toolsets.registry import builtin_catalog

        # The join this command did not have. `loom connect jira` looked the
        # *credential* name up in the provider registry, found nothing, and
        # refused with "'jira' is not a known provider" — Jira's provider is
        # `atlassian`, Gmail's is `google_gmail`, and all six Graph toolsets
        # share `microsoft`. A manifest declares which, so ask it.
        provider = get_oauth_provider(provider_hint)
        if provider is None:
            need = need_for(provider_hint, builtin_catalog())
            if need.provider:
                provider = get_oauth_provider(need.provider)

    authorization_endpoint = (
        pick("authorization_endpoint", "AUTHORIZATION_ENDPOINT")
        or (provider.authorization_endpoint if provider else None)
    )
    token_endpoint = (
        pick("token_endpoint", "TOKEN_ENDPOINT")
        or (provider.token_endpoint if provider else None)
    )
    device_authorization_endpoint = (
        pick("device_authorization_endpoint", "DEVICE_AUTHORIZATION_ENDPOINT")
        or (provider.device_authorization_endpoint if provider else None)
    )
    client_id = pick("client_id", "CLIENT_ID")
    client_secret = pick("client_secret", "CLIENT_SECRET")

    scope_flags = tuple(getattr(args, "scope", None) or ())
    env_scopes = tuple(os.environ.get(f"{env_prefix}_SCOPES", "").split())
    if scope_flags:
        scopes = scope_flags
    elif env_scopes:
        scopes = env_scopes
    elif need is not None and need.scopes:
        # What the toolsets reading this credential actually declare, which is
        # narrower than the provider's defaults: a Jira-only workflow should
        # not be made to grant Confluence.
        scopes = need.scopes
    else:
        scopes = provider.default_scopes if provider else ()

    if not token_endpoint or not client_id:
        hint = ""
        if provider is None and provider_hint:
            hint = (
                f". '{provider_hint}' is not a known provider — pass "
                "--token-endpoint/--client-id, or see 'loom providers'"
            )
        elif provider is not None and need is not None and need.known:
            # It *is* known, just not under this name. Say which, or the
            # message reads as "unsupported" for a toolset LOOM ships.
            hint = (
                f". '{provider_hint}' is served by the '{provider.id}' provider"
                f" — create an app there and pass --client-id"
            )
        raise ConfigurationError(
            f"'{name}' needs a token endpoint and a client id: pass "
            "--token-endpoint/--client-id, or set "
            f"{env_prefix}_TOKEN_ENDPOINT/{env_prefix}_CLIENT_ID{hint}"
        )
    if not authorization_endpoint and not device_authorization_endpoint:
        raise ConfigurationError(
            f"'{name}' needs --authorization-endpoint and/or "
            "--device-authorization-endpoint (or the matching "
            f"{env_prefix}_* environment variable) — nothing to connect to"
        )

    extra = dict(provider.extra_auth_params) if provider else {}
    return _OAuthTarget(
        name=name,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        device_authorization_endpoint=device_authorization_endpoint,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        extra_auth_params=extra,
        pkce=provider.supports_pkce if provider else True,
        provider_id=provider.id if provider else "",
    )


def _choose_flow(args: argparse.Namespace, target: _OAuthTarget) -> str:
    if getattr(args, "device", False):
        requested = "device"
    elif getattr(args, "pkce", False):
        requested = "pkce"
    else:
        requested = "device" if _is_headless() else "pkce"

    # A flag beats detection, but neither beats reality: fall back to
    # whichever flow this target actually offers rather than failing on a
    # default the operator never chose.
    if requested == "pkce" and not target.authorization_endpoint:
        requested = "device"
    if requested == "device" and not target.device_authorization_endpoint:
        requested = "pkce"
    if requested == "pkce" and not target.authorization_endpoint:
        raise ConfigurationError(
            f"'{target.name}' has no authorization endpoint or device "
            "authorization endpoint configured — nothing to connect to"
        )
    return requested


def _target_metadata(target: _OAuthTarget) -> dict[str, str]:
    """Kept as a name; the rule lives on :meth:`OAuthTarget.metadata`."""
    return target.metadata()


# ---------------------------------------------------------------------------
# The PKCE redirect listener
# ---------------------------------------------------------------------------


class _PkceListener(LoopbackListener):
    """:class:`~loom.connectors.flows.LoopbackListener`, port resolved the
    CLI's way.

    Where the redirect lands is a *library* concern; which port a project uses
    is a CLI one — ``--redirect-port``, then ``$LOOM_OAUTH_REDIRECT_PORT``,
    then the same key in the project's ``.env``. Resolving that in the library
    would mean ``loom.connectors`` importing ``loom.cli.config`` to read a
    dotenv, which is exactly the import edge this extraction exists to avoid.
    """

    @classmethod
    async def start(cls, port: int | None = None) -> _PkceListener:
        listener = await super().start(resolve_redirect_port(port))
        return cast("_PkceListener", listener)


# ---------------------------------------------------------------------------
# The two flows
# ---------------------------------------------------------------------------


def _render(out: Printer) -> Any:
    """Turn a :class:`ConnectEvent` into lines. The CLI's whole share of a flow.

    A flow reports what happened; only this knows there is a terminal, a
    ``rich`` console, or a ``--json`` mode to stay out of the way of. Passed as
    a callback for the reason ``LocalFacade.on_stage`` is: a renderer must not
    be something the library depends on.
    """

    def draw(event: ConnectEvent) -> None:
        if event.kind == "redirect_ready":
            out.line()
            out.line(f"  Redirect URI: [bold]{event.redirect_uri}[/bold]")
            if event.scopes:
                out.line("  Requesting scopes: " + " ".join(event.scopes))
        elif event.kind == "needs_app":
            out.line()
            out.line("  No OAuth client is configured for this provider.")
            out.line(f"  Register [bold]{event.redirect_uri}[/bold] as the redirect URI")
            if event.setup_url:
                out.line(f"  Create an app: [dim]{event.setup_url}[/dim]")
            out.line("  Then re-run with --client-id (and --client-secret).")
        elif event.kind == "opening_browser":
            out.line("  Opening your browser to continue.")
            out.line(f"  If it does not open, visit: [dim]{event.authorization_url}[/dim]")
            out.line()
        elif event.kind == "waiting" and event.verification_uri:
            out.line()
            out.line(f"  Go to [bold]{event.verification_uri}[/bold]")
            out.line(f"  and enter code: [bold]{event.user_code}[/bold]")
            out.line()
            out.line("  Waiting for approval…")

    return draw


async def _run_device_flow(client: OAuthClient, out: Printer) -> StoredCredential:
    return await run_device_authorization(client, on_event=_render(out))


async def _run_pkce_flow(
    target: _OAuthTarget,
    out: Printer,
    *,
    timeout: float,
    redirect_port: int | None = None,
) -> StoredCredential:
    outcome = await OAuthBrowserFlow().connect(
        ConnectRequest(
            name=target.name,
            # Resolved here, not in the flow: see _PkceListener.
            redirect_port=resolve_redirect_port(redirect_port),
            timeout=timeout,
        ),
        AuthSpec(kind="oauth2"),
        target=target,
        on_event=_render(out),
    )
    if outcome.credential is None:
        raise ConfigurationError(outcome.reason or "authorization did not complete")
    return outcome.credential


async def _connect(
    args: argparse.Namespace, out: Printer, *, name: str, env_prefix: str
) -> StoredCredential:
    """Resolve a target, pick a flow, run it, and stamp the result with the
    metadata :class:`MetadataRefresher` needs to renew it unattended."""
    # Provider lookup is connect-only. ``cmd_login`` stores under
    # ``loom:<server>`` and must not infer a third-party provider from that name.
    provider_hint = None
    if env_prefix.startswith("LOOM_CONNECT"):
        provider_hint = getattr(args, "provider", None) or name
    target = _resolve_target(
        args, name=name, env_prefix=env_prefix, provider_hint=provider_hint
    )
    if target.scopes:
        out.line("  Requesting scopes: " + " ".join(target.scopes))
    flow = _choose_flow(args, target)
    timeout = float(getattr(args, "timeout", None) or 300.0)

    if flow == "device":
        credential = replace(
            await _run_device_flow(target.client(), out),
            metadata=target.metadata(),
        )
    else:
        credential = await _run_pkce_flow(
            target, out, timeout=timeout, redirect_port=getattr(args, "redirect_port", None)
        )

    # No re-stamping: `OAuthBrowserFlow` and `OAuthDeviceFlow` already attach
    # what `MetadataRefresher` needs, from `OAuthTarget.metadata()`.
    return credential


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _add_credential_status(out: Printer, name: str, credential: StoredCredential) -> None:
    out.line(f"  [green]Connected[/green] '{name}'.")
    if credential.scopes:
        out.value("scopes", sorted(credential.scopes))
    if credential.expires_at:
        out.value("expires", credential.expires_at.isoformat())


def cmd_login(args: argparse.Namespace) -> int:
    """Authenticate this CLI against a LOOM server."""
    out = printer_for(args)
    server = args.server or DEFAULT_SERVER
    name = _server_credential_name(server)

    async def body() -> int:
        try:
            credential = await _connect(args, out, name=name, env_prefix="LOOM_LOGIN")
        except AuthExpired as exc:
            raise ConfigurationError(str(exc)) from exc
        store = _store()
        await store.put(name, credential)
        _add_credential_status(out, server, credential)
        out.json({"server": server, "name": name, "scopes": sorted(credential.scopes)})
        return int(Exit.OK)

    return run_async(body())


def cmd_logout(args: argparse.Namespace) -> int:
    """Forget this CLI's stored login for a server."""
    out = printer_for(args)
    server = args.server or DEFAULT_SERVER
    name = _server_credential_name(server)

    async def body() -> int:
        store = _store()
        await store.forget(name)
        out.line(f"  Logged out of {server}.")
        out.json({"server": server, "name": name, "logged_out": True})
        return int(Exit.OK)

    return run_async(body())


def cmd_whoami(args: argparse.Namespace) -> int:
    """List every credential this CLI has stored, and its expiry."""
    out = printer_for(args)

    async def body() -> int:
        from loom.connectors.credentials import Peekable, RefreshPolicy
        from loom.runtime.clock import SystemClock

        store = _store()
        names = await store.names()
        if not names:
            out.line("Not connected to anything.")
            out.hint("loom login")
            out.hint("loom connect <name>")
            out.json({"connected": []})
            return int(Exit.OK)

        clock = SystemClock()
        policy = getattr(store, "refresh_policy", None) or RefreshPolicy()
        rows: list[list[str]] = []
        connected: list[dict[str, Any]] = []
        for name in names:
            stored = await store.peek(name) if isinstance(store, Peekable) else None
            if stored is None:
                continue
            expires_at = stored.expires_at.isoformat() if stored.expires_at else "-"
            expired = stored.is_expired(clock)
            due = policy.is_due(stored, clock)
            # Three states, not two. "due" is the one worth surfacing: the
            # credential works, and the next call will try to renew it — so an
            # operator seeing repeated "due" knows renewal is failing, long
            # before it becomes "expired" and something breaks.
            status = "expired" if expired else ("due" if due else "ok")
            scopes = ", ".join(sorted(stored.scopes)) or "-"
            rows.append([name, scopes, expires_at, status])
            connected.append(
                {
                    "name": name,
                    "scopes": sorted(stored.scopes),
                    "expires_at": stored.expires_at.isoformat() if stored.expires_at else None,
                    "expired": expired,
                    "refresh_due": due,
                    "can_refresh": stored.refresh_token is not None,
                }
            )
        out.table(["name", "scopes", "expires_at", "status"], rows, status_column=3)
        # Never includes the token itself — StoredCredential.token is a
        # Secret, and nothing here ever calls .reveal() on it.
        out.json({"connected": connected})
        return int(Exit.OK)

    return run_async(body())


def cmd_connect(args: argparse.Namespace) -> int:
    """Authenticate a named credential a workflow's toolsets can read."""
    out = printer_for(args)
    name = args.name

    async def body() -> int:
        # OAuth only, and that is a statement about the *clients* rather than
        # about this command. All six store-backed credentials the shipped
        # toolsets declare are `oauth2`; the twelve api-key ones read
        # environment variables and no `CredentialStore`, so connecting them
        # would store a value nothing looks up. `ApiKeyFlow` exists and
        # `facade.connect` routes to it — what is missing is a store path in
        # those twelve clients, which is a change to them.
        try:
            credential = await _connect(args, out, name=name, env_prefix=env_prefix_for(name))
        except AuthExpired as exc:
            raise ConfigurationError(str(exc)) from exc
        store = _store()
        await store.put(name, credential)
        _add_credential_status(out, name, credential)
        out.json({"name": name, "scopes": sorted(credential.scopes)})
        return int(Exit.OK)

    return run_async(body())


def cmd_disconnect(args: argparse.Namespace) -> int:
    """Forget a named credential connected via ``loom connect``.

    The counterpart to :func:`cmd_connect` — same name, same store, same
    encrypted-at-rest file. Distinct from :func:`cmd_logout`, which forgets
    this CLI's own ``loom:<server>`` login rather than a toolset credential.
    """
    out = printer_for(args)
    name = args.name

    async def body() -> int:
        store = _store()
        existed = name in await store.names()
        await store.forget(name)
        out.line(
            f"  Disconnected '{name}'."
            if existed
            else f"  '{name}' was not connected."
        )
        out.json({"name": name, "disconnected": existed})
        return int(Exit.OK)

    return run_async(body())


def cmd_refresh(args: argparse.Namespace) -> int:
    """Renew stored OAuth credentials that are near expiry.

    The hook for "on restart": run it from a login profile, a systemd unit, or
    a machine's own cron, and every stored credential is renewed before
    anything needs it. Long-lived ``loom serve`` / ``loom mcp`` processes do
    the same thing on a timer without being asked.

    Exit codes follow the CLI's contract: ``0`` when nothing failed, ``1`` when
    at least one credential could not be renewed — so a scheduled invocation
    reports a dead refresh token instead of succeeding quietly.
    """
    out = printer_for(args)
    names: list[str] = list(getattr(args, "name", None) or [])

    async def body() -> int:
        from loom.connectors.refresh import CredentialRefreshService

        store = _store()
        known = await store.names()
        if not known:
            out.line("Nothing connected, so nothing to refresh.")
            out.hint("loom connect <name>")
            out.json({"refreshed": [], "failed": []})
            return int(Exit.OK)

        unknown = [name for name in names if name not in known]
        if unknown:
            raise ConfigurationError(
                f"not connected: {', '.join(unknown)}. Known: "
                f"{', '.join(known)}. Run 'loom connect <name>' first."
            )

        service = CredentialRefreshService(store)
        report = await service.sweep(names or None, force=bool(getattr(args, "force", False)))

        out.table(
            ["name", "result", "expires_at", "detail"],
            [
                [
                    outcome.name,
                    outcome.status,
                    outcome.expires_at.isoformat() if outcome.expires_at else "-",
                    outcome.detail or "-",
                ]
                for outcome in report.outcomes
            ],
            status_column=1,
        )
        # Never the token itself — the report carries names and expiries only.
        out.json(
            {
                "refreshed": [o.name for o in report.refreshed],
                "failed": [{"name": o.name, "error": o.detail} for o in report.failed],
                "checked": [
                    {
                        "name": o.name,
                        "status": o.status,
                        "expires_at": o.expires_at.isoformat() if o.expires_at else None,
                    }
                    for o in report.outcomes
                ],
            }
        )
        if report.failed:
            out.error(
                f"{len(report.failed)} credential(s) could not be refreshed. "
                "Reauthorize with 'loom connect <name>'."
            )
            return int(Exit.FAILED)
        return int(Exit.OK)

    return run_async(body())


def cmd_providers(args: argparse.Namespace) -> int:
    """List pre-configured OAuth providers."""
    from loom.connectors.oauth_providers import list_oauth_providers

    out = printer_for(args)
    providers = list_oauth_providers()
    out.table(
        ["id", "name", "pkce", "default_scopes"],
        [
            [
                p.id,
                p.display_name,
                "yes" if p.supports_pkce else "no",
                " ".join(p.default_scopes) or "-",
            ]
            for p in providers
        ],
    )
    out.json(
        {
            "providers": [
                {
                    "id": p.id,
                    "display_name": p.display_name,
                    "authorization_endpoint": p.authorization_endpoint,
                    "token_endpoint": p.token_endpoint,
                    "device_authorization_endpoint": p.device_authorization_endpoint,
                    "default_scopes": list(p.default_scopes),
                    "supports_pkce": p.supports_pkce,
                    "docs_url": p.docs_url,
                }
                for p in providers
            ]
        }
    )
    return int(Exit.OK)
