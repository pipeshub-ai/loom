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
import asyncio
import os
import re
import secrets
import sys
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from loom.cli.commands import printer_for, run_async
from loom.cli.output import Exit, Printer
from loom.connectors.oauth_client import OAuthTokenError, generate_pkce_pair
from loom.core.exceptions import AuthExpired, ConfigurationError, CredentialNotFound

if TYPE_CHECKING:
    from loom.connectors.credentials import CredentialStore, StoredCredential
    from loom.connectors.oauth_client import DeviceAuthorization, OAuthClient

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


def _dotenv_get(name: str) -> str | None:
    """``name`` from ``.env`` in the current directory, if present.

    Does not override a real environment variable — exporting a port for one
    run still wins over the file. Quoted values and comments match the
    cookbook loader so a ``.env`` written for examples works here too.
    """
    path = Path.cwd() / ".env"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("'\"") or None
    return None


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
# Headless detection
# ---------------------------------------------------------------------------


def _is_headless() -> bool:
    """Best-effort: is there a browser to open here?

    ``LOOM_LOGIN_DEVICE=1`` (or ``--device``, checked by the caller) always
    wins; this is only the auto-detection used when neither flag is given.
    SSH without X11 forwarding and a bare Linux console are the common
    cases; macOS and Windows are graphical unless told otherwise.
    """
    if os.environ.get("LOOM_LOGIN_HEADLESS", "").lower() in ("1", "true", "yes"):
        return True
    if sys.platform.startswith(("darwin", "win32")):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# ---------------------------------------------------------------------------
# Resolving what to authenticate against
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OAuthTarget:
    name: str
    authorization_endpoint: str | None
    token_endpoint: str
    device_authorization_endpoint: str | None
    client_id: str
    client_secret: str | None
    scopes: tuple[str, ...]
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    pkce: bool = True
    provider_id: str = ""


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
    if provider_hint:
        from loom.connectors.oauth_providers import get_oauth_provider

        provider = get_oauth_provider(provider_hint)

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
    else:
        scopes = provider.default_scopes if provider else ()

    if not token_endpoint or not client_id:
        hint = ""
        if provider is None and provider_hint:
            hint = (
                f". '{provider_hint}' is not a known provider — pass "
                "--token-endpoint/--client-id, or see 'loom providers'"
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
    """What :class:`MetadataRefresher` needs to renew this later.

    Goes into the same encrypted-at-rest file as the token itself, so
    storing a confidential client's secret here carries no more exposure
    than the refresh token sitting right next to it.
    """
    meta: dict[str, str] = {"token_endpoint": target.token_endpoint, "client_id": target.client_id}
    if target.client_secret:
        meta["client_secret"] = target.client_secret
    if target.device_authorization_endpoint:
        meta["device_authorization_endpoint"] = target.device_authorization_endpoint
    if target.provider_id:
        meta["provider_id"] = target.provider_id
    return meta


# ---------------------------------------------------------------------------
# The PKCE redirect listener
# ---------------------------------------------------------------------------


class _PkceListener:
    """A one-shot local HTTP server for the OAuth redirect, pure asyncio.

    No threads: :meth:`wait_for_redirect` is a plain ``asyncio.wait_for``, so
    a login the user never finishes gives up cleanly on its own timeout
    instead of leaving a background thread blocked in ``accept()``.
    """

    def __init__(self) -> None:
        self._result: dict[str, str] = {}
        self._done = asyncio.Event()
        self._server: asyncio.Server | None = None
        self.port = 0

    @classmethod
    async def start(cls, port: int | None = None) -> _PkceListener:
        self = cls()
        bind_port = resolve_redirect_port(port)
        try:
            self._server = await asyncio.start_server(
                self._handle, "127.0.0.1", bind_port, reuse_address=False
            )
        except OSError as exc:
            if bind_port:
                raise ConfigurationError(
                    f"OAuth redirect port {bind_port} is in use. Free it, or set "
                    f"{REDIRECT_PORT_ENV} in .env to a free port (and register "
                    f"http://127.0.0.1:<port>/callback on the OAuth client)."
                ) from exc
            raise
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _handle(self, reader: Any, writer: Any) -> None:
        try:
            request_line = await reader.readline()
            try:
                _, target, _ = request_line.decode("latin-1").split(" ", 2)
            except ValueError:
                target = "/"
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b""):
                    break
            parsed = urlparse(target)
            self._result = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            body = (
                b"<html><body><p>Signed in to LOOM. "
                b"You may close this window.</p></body></html>"
            )
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()
            self._done.set()

    async def wait_for_redirect(self, *, timeout: float) -> dict[str, str]:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except TimeoutError as exc:
            raise ConfigurationError(
                "timed out waiting for the browser login to complete — "
                "try 'loom login --device' instead"
            ) from exc
        return self._result

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


# ---------------------------------------------------------------------------
# The two flows
# ---------------------------------------------------------------------------


async def _run_device_flow(client: OAuthClient, out: Printer) -> StoredCredential:
    device: DeviceAuthorization = await client.start_device_authorization()
    out.line()
    out.line(f"  Go to [bold]{device.verification_uri}[/bold]")
    out.line(f"  and enter code: [bold]{device.user_code}[/bold]")
    out.line()
    if device.verification_uri_complete:
        webbrowser.open(device.verification_uri_complete)
    out.line("  Waiting for approval…")
    try:
        return await client.poll_device_token(device)
    except OAuthTokenError as exc:
        raise ConfigurationError(f"authorization failed: {exc}") from exc


async def _run_pkce_flow(
    target: _OAuthTarget,
    out: Printer,
    *,
    timeout: float,
    redirect_port: int | None = None,
) -> StoredCredential:
    from loom.connectors.oauth_client import OAuthClient

    listener = await _PkceListener.start(port=redirect_port)
    state = secrets.token_urlsafe(16)
    try:
        redirect_uri = f"http://127.0.0.1:{listener.port}/callback"
        client = OAuthClient(
            client_id=target.client_id,
            client_secret=target.client_secret,
            authorization_endpoint=target.authorization_endpoint or "",
            token_endpoint=target.token_endpoint,
            redirect_uri=redirect_uri,
            scopes=target.scopes,
            pkce=target.pkce,
        )
        extra = target.extra_auth_params or None
        if target.pkce:
            verifier, challenge = generate_pkce_pair()
            url = client.authorization_url(
                state=state, code_challenge=challenge, extra_params=extra
            )
        else:
            verifier = ""
            url = client.authorization_url(state=state, extra_params=extra)
        out.line()
        out.line("  Opening your browser to continue.")
        out.line(f"  Redirect URI: [bold]{redirect_uri}[/bold]")
        out.line(f"  If it does not open, visit: [dim]{url}[/dim]")
        out.line()
        webbrowser.open(url)
        params = await listener.wait_for_redirect(timeout=timeout)
    finally:
        await listener.close()

    if params.get("error"):
        description = params.get("error_description", "")
        raise ConfigurationError(f"authorization failed: {params['error']} {description}".strip())
    if params.get("state") != state:
        raise ConfigurationError(
            "authorization response had an unexpected 'state' — aborting, "
            "in case this redirect was not meant for this login"
        )
    code = params.get("code")
    if not code:
        raise ConfigurationError("authorization response carried no 'code'")

    try:
        return await client.exchange_code(code, code_verifier=verifier)
    except OAuthTokenError as exc:
        raise ConfigurationError(f"authorization failed: {exc}") from exc


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
        from loom.connectors.oauth_client import OAuthClient

        client = OAuthClient(
            client_id=target.client_id,
            client_secret=target.client_secret,
            authorization_endpoint=target.authorization_endpoint or "",
            token_endpoint=target.token_endpoint,
            device_authorization_endpoint=target.device_authorization_endpoint,
            scopes=target.scopes,
            pkce=target.pkce,
        )
        credential = await _run_device_flow(client, out)
    else:
        credential = await _run_pkce_flow(
            target, out, timeout=timeout, redirect_port=getattr(args, "redirect_port", None)
        )

    return replace(credential, metadata={**credential.metadata, **_target_metadata(target)})


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
