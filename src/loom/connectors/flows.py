"""Obtaining a credential — the write half of the connection plane.

Every line of this used to live inside ``cli/auth_commands.py`` and take an
``argparse.Namespace``, so nothing but the CLI could perform a connection. The
session, the MCP server and the coding agent each need one and none of them has
a ``Namespace``, which is the DRY problem underneath *"the agent should be able
to connect an account"*: there was one implementation and it was welded to one
caller.

**Library-adapter tier** (see `phases/phase-15` §3.6). Stdlib plus ``httpx``,
which the toolsets already require. It may open a browser and read a terminal —
``CLIUserInteraction`` sets that precedent one package over — but only when a
host *calls* it. Importing this module opens nothing, reads nothing, and
installs no handler; and nothing here imports ``loom.cli``, ``rich``,
``prompt_toolkit`` or ``argparse``, which ``tests/test_layering.py`` enforces.

A flow reports progress as a :class:`ConnectEvent`, never as a rendered line.
``_run_pkce_flow`` took a ``loom.cli.output.Printer`` and wrote four lines
through it; lifting that as-is would have put a ``[cli]``-flavoured renderer in
``loom/connectors/`` and made the second library-to-CLI import edge in the
codebase. Turning an event into a line is the CLI's job, the same arrangement
``LocalFacade.on_stage`` and ``ProgressRenderer`` already use.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlparse

from loom.connectors.credentials import StoredCredential
from loom.connectors.oauth_client import OAuthClient, OAuthTokenError, generate_pkce_pair
from loom.core.exceptions import ConfigurationError
from loom.core.secret import Secret
from loom.toolsets.manifest import AuthField, AuthSpec

__all__ = [
    "ApiKeyFlow",
    "AppRegistrationStore",
    "ConnectEvent",
    "ConnectFlow",
    "ConnectOutcome",
    "ConnectRequest",
    "ConsoleSecretPrompt",
    "LoopbackListener",
    "OAuthBrowserFlow",
    "OAuthDeviceFlow",
    "OAuthTarget",
    "SecretPrompt",
    "is_headless",
    "open_in_browser",
    "run_device_authorization",
]

#: The loopback port the redirect listener binds by default. Stable so an OAuth
#: client can register ``http://127.0.0.1:8931/callback`` once and keep it.
DEFAULT_REDIRECT_PORT = 8931


# ---------------------------------------------------------------------------
# What a flow is asked for, what it reports, and what it returns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectEvent:
    """Something a person may want to see while a flow runs.

    Structured, never a rendered line — see the module docstring. ``kind`` is a
    closed set so a renderer can be exhaustive; everything else is context that
    particular kind carries.
    """

    kind: Literal[
        "needs_app",
        "redirect_ready",
        "opening_browser",
        "waiting",
        "exchanged",
        "stored",
    ]
    detail: str = ""
    redirect_uri: str = ""
    authorization_url: str = ""
    verification_uri: str = ""
    user_code: str = ""
    scopes: tuple[str, ...] = ()
    setup_url: str = ""


#: What a flow calls to report progress. Optional, and it fails open: a flow
#: with no listener still connects, and a renderer that raises must never lose a
#: credential that was successfully obtained.
Listener = Callable[[ConnectEvent], None]


def open_in_browser(url: str) -> None:
    """Open *url*, looking ``webbrowser.open`` up when called.

    A function rather than ``webbrowser.open`` itself, because a dataclass
    field default is evaluated at **import** time: passing the bound function
    would freeze whatever ``webbrowser.open`` was then, so a test that patches
    it afterwards would have no effect and would open a real browser on
    somebody's machine. The indirection is one attribute lookup and it is what
    keeps the seam patchable.
    """
    webbrowser.open(url)


@dataclass(frozen=True)
class ConnectRequest:
    """Everything a flow needs that is not the target's own configuration."""

    name: str
    """The `CredentialStore` key the result will be stored under."""
    client_id: str = ""
    client_secret: str = ""
    fields: Mapping[str, str] = field(default_factory=dict)
    """Values for an :class:`~loom.toolsets.manifest.AuthField`, for the
    api-key route. Collected by the caller — see :class:`ApiKeyFlow`."""
    scopes: tuple[str, ...] = ()
    redirect_port: int | None = None
    timeout: float = 300.0


@dataclass(frozen=True)
class ConnectOutcome:
    """What came back. Carries **no token and no secret value.**

    A credential is handed to the caller through :attr:`credential`, which is
    the thing that gets stored; everything a *renderer* or a model sees is here
    and is safe to print. `loom refresh`'s JSON already draws this line.
    """

    connected: bool
    name: str
    scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None
    needs: tuple[AuthField, ...] = ()
    """What is still missing, when the flow could not proceed."""
    redirect_uri: str = ""
    authorization_url: str = ""
    reason: str = ""

    credential: StoredCredential | None = field(default=None, repr=False, compare=False)
    """The obtained credential, for the caller to store. ``repr=False`` so an
    outcome logged or echoed cannot spill one; the fields it holds are
    :class:`~loom.core.secret.Secret` besides."""


@runtime_checkable
class SecretPrompt(Protocol):
    """How a secret value is collected from a person.

    Its own seam, and **never** ``ask_user``: `AskedQuestion` records the
    question *and its answer* on `CodingResult.questions`, and
    ``loom author --save-answers`` writes that to disk. A client secret
    collected that way would land in a file people commit.
    """

    def available(self) -> bool:
        """Whether anything can actually answer right now."""
        ...

    async def read(self, field: AuthField) -> str:
        """The value for *field*, without echoing it."""
        ...


@runtime_checkable
class ConnectFlow(Protocol):
    """One way of obtaining a credential."""

    def supports(self, spec: AuthSpec) -> bool: ...

    async def connect(
        self,
        request: ConnectRequest,
        spec: AuthSpec,
        *,
        target: OAuthTarget | None = None,
        on_event: Listener | None = None,
    ) -> ConnectOutcome: ...


def _emit(on_event: Listener | None, event: ConnectEvent) -> None:
    """Report progress, and never let reporting be the thing that fails.

    Fails open, the rule the non-deciding hook families already follow: a
    broken renderer must not lose a credential the flow has already obtained.
    """
    if on_event is None:
        return
    with contextlib.suppress(Exception):
        on_event(event)


# ---------------------------------------------------------------------------
# Where to authenticate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthTarget:
    """The endpoints and client identity one OAuth connection needs.

    Built by the caller — from a provider entry, from flags, or from
    environment variables — because *which* of those wins is a policy the CLI
    owns and the flow does not.
    """

    name: str
    token_endpoint: str
    authorization_endpoint: str | None = None
    device_authorization_endpoint: str | None = None
    client_id: str = ""
    client_secret: str | None = None
    scopes: tuple[str, ...] = ()
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    pkce: bool = True
    provider_id: str = ""

    @classmethod
    def from_provider(
        cls,
        name: str,
        provider_id: str,
        *,
        scopes: tuple[str, ...] = (),
        client_id: str = "",
        client_secret: str | None = None,
    ) -> OAuthTarget:
        """A target from a registered provider, or raise naming what is known.

        This is the join `loom connect jira` did not have: the credential name
        a client reads (`jira`) and the provider that issues it (`atlassian`)
        are different strings, and looking the first up in the provider
        registry answered *"'jira' is not a known provider"*.
        """
        from loom.connectors.oauth_providers import (
            get_oauth_provider,
            list_oauth_providers,
        )

        provider = get_oauth_provider(provider_id)
        if provider is None:
            known = ", ".join(p.id for p in list_oauth_providers())
            raise ConfigurationError(
                f"'{provider_id}' is not a known OAuth provider. Known: {known}"
            )
        return cls(
            name=name,
            token_endpoint=provider.token_endpoint,
            authorization_endpoint=provider.authorization_endpoint,
            device_authorization_endpoint=provider.device_authorization_endpoint,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes or provider.default_scopes,
            extra_auth_params=dict(provider.extra_auth_params),
            pkce=provider.supports_pkce,
            provider_id=provider.id,
        )

    def metadata(self) -> dict[str, str]:
        """What :class:`~loom.connectors.oauth_client.MetadataRefresher` needs
        to renew this later, unattended.

        Goes into the same encrypted-at-rest file as the token, so a
        confidential client's secret here carries no more exposure than the
        refresh token sitting beside it.
        """
        meta = {"token_endpoint": self.token_endpoint, "client_id": self.client_id}
        if self.client_secret:
            meta["client_secret"] = self.client_secret
        if self.device_authorization_endpoint:
            meta["device_authorization_endpoint"] = self.device_authorization_endpoint
        if self.provider_id:
            meta["provider_id"] = self.provider_id
        return meta

    def client(self, *, redirect_uri: str = "") -> OAuthClient:
        return OAuthClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            authorization_endpoint=self.authorization_endpoint or "",
            token_endpoint=self.token_endpoint,
            device_authorization_endpoint=self.device_authorization_endpoint,
            redirect_uri=redirect_uri,
            scopes=self.scopes,
            pkce=self.pkce,
        )


def is_headless() -> bool:
    """Best-effort: is there a browser to open here?

    Only the auto-detection used when no flag was given. SSH without X11 and a
    bare Linux console are the common cases; macOS and Windows are graphical
    unless told otherwise.
    """
    if os.environ.get("LOOM_LOGIN_HEADLESS", "").lower() in ("1", "true", "yes"):
        return True
    if sys.platform.startswith(("darwin", "win32")):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# ---------------------------------------------------------------------------
# The loopback redirect listener
# ---------------------------------------------------------------------------


class LoopbackListener:
    """A one-shot local HTTP server for the OAuth redirect, pure asyncio.

    The redirect target RFC 8252 prescribes for a native client: the app
    listens on the loopback interface and the authorization server sends the
    code back to it.

    No threads — :meth:`wait_for_redirect` is a plain ``asyncio.wait_for``, so
    a login nobody finishes gives up on its own timeout instead of leaving a
    background thread blocked in ``accept()``.
    """

    def __init__(self) -> None:
        self._result: dict[str, str] = {}
        self._done = asyncio.Event()
        self._server: asyncio.Server | None = None
        self.port = 0

    @classmethod
    async def start(cls, port: int | None = None) -> LoopbackListener:
        self = cls()
        bind_port = DEFAULT_REDIRECT_PORT if port is None else port
        try:
            self._server = await asyncio.start_server(
                self._handle, "127.0.0.1", bind_port, reuse_address=False
            )
        except OSError as exc:
            if bind_port:
                raise ConfigurationError(
                    f"OAuth redirect port {bind_port} is in use. Free it, or set "
                    "LOOM_OAUTH_REDIRECT_PORT in .env to a free port (and register "
                    "http://127.0.0.1:<port>/callback on the OAuth client)."
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
# The flows
# ---------------------------------------------------------------------------


def _needs_app(target: OAuthTarget) -> bool:
    return not target.client_id


@dataclass
class OAuthBrowserFlow:
    """Authorization code + PKCE, over a loopback redirect. RFC 8252's shape.

    PKCE by default because the RFC requires it for a native client: a public
    client cannot keep a secret, so the code is bound to a verifier this
    process generated instead.

    ``open_browser`` is injected so a host that renders the URL itself — a
    remote session, a page — can pass ``None`` and use the
    ``opening_browser`` event instead of having a browser opened underneath it.
    """

    open_browser: Callable[[str], Any] | None = open_in_browser

    def supports(self, spec: AuthSpec) -> bool:
        return spec.kind == "oauth2"

    async def connect(
        self,
        request: ConnectRequest,
        spec: AuthSpec,
        *,
        target: OAuthTarget | None = None,
        on_event: Listener | None = None,
    ) -> ConnectOutcome:
        if target is None:
            raise ConfigurationError("an OAuth flow needs a target")
        listener = await LoopbackListener.start(port=request.redirect_port)
        redirect_uri = f"http://127.0.0.1:{listener.port}/callback"

        # Before anything is asked for. n8n's credential dialog shows the
        # callback URL as a read-only field for the same reason: registering it
        # on the provider is the one step nobody can do for you, and finding
        # out afterwards means going back and doing the whole flow again.
        _emit(on_event, ConnectEvent(
            kind="redirect_ready", redirect_uri=redirect_uri,
            scopes=target.scopes, setup_url=spec.setup_url,
        ))
        if _needs_app(target):
            await listener.close()
            _emit(on_event, ConnectEvent(
                kind="needs_app", redirect_uri=redirect_uri,
                setup_url=spec.setup_url,
                detail="register the redirect URI above, then supply the client id",
            ))
            return ConnectOutcome(
                connected=False,
                name=request.name,
                redirect_uri=redirect_uri,
                reason=(
                    f"'{request.name}' needs an OAuth client id. Create an app, "
                    f"register {redirect_uri} as its redirect URI, and pass "
                    "--client-id (and --client-secret if it is confidential)."
                ),
            )

        state = _state()
        try:
            client = target.client(redirect_uri=redirect_uri)
            extra = target.extra_auth_params or None
            if target.pkce:
                verifier, challenge = generate_pkce_pair()
                url = client.authorization_url(
                    state=state, code_challenge=challenge, extra_params=extra
                )
            else:
                verifier = ""
                url = client.authorization_url(state=state, extra_params=extra)

            _emit(on_event, ConnectEvent(
                kind="opening_browser", authorization_url=url,
                redirect_uri=redirect_uri, scopes=target.scopes,
            ))
            if self.open_browser is not None:
                self.open_browser(url)
            _emit(on_event, ConnectEvent(kind="waiting", authorization_url=url))
            params = await listener.wait_for_redirect(timeout=request.timeout)
        finally:
            await listener.close()

        if params.get("error"):
            description = params.get("error_description", "")
            raise ConfigurationError(
                f"authorization failed: {params['error']} {description}".strip()
            )
        if params.get("state") != state:
            raise ConfigurationError(
                "authorization response had an unexpected 'state' — aborting, "
                "in case this redirect was not meant for this login"
            )
        code = params.get("code")
        if not code:
            raise ConfigurationError("authorization response carried no 'code'")

        try:
            credential = await client.exchange_code(code, code_verifier=verifier)
        except OAuthTokenError as exc:
            raise ConfigurationError(f"authorization failed: {exc}") from exc

        return _finished(request, target, credential, redirect_uri, on_event)


@dataclass
class OAuthDeviceFlow:
    """The device-code flow, for a host with no browser to open."""

    open_browser: Callable[[str], Any] | None = open_in_browser

    def supports(self, spec: AuthSpec) -> bool:
        return spec.kind == "oauth2"

    async def connect(
        self,
        request: ConnectRequest,
        spec: AuthSpec,
        *,
        target: OAuthTarget | None = None,
        on_event: Listener | None = None,
    ) -> ConnectOutcome:
        if target is None:
            raise ConfigurationError("an OAuth flow needs a target")
        if _needs_app(target):
            return ConnectOutcome(
                connected=False,
                name=request.name,
                reason=f"'{request.name}' needs an OAuth client id.",
            )
        credential = await run_device_authorization(
            target.client(),
            scopes=target.scopes,
            on_event=on_event,
            open_browser=self.open_browser,
        )
        return _finished(request, target, credential, "", on_event)


async def run_device_authorization(
    client: OAuthClient,
    *,
    scopes: tuple[str, ...] = (),
    on_event: Listener | None = None,
    open_browser: Callable[[str], Any] | None = open_in_browser,
) -> StoredCredential:
    """Start a device authorization and poll until it is approved or fails.

    Its own function rather than a method so a caller holding an
    :class:`OAuthClient` — the CLI, driving a target it resolved from flags —
    runs the *same* dance as :class:`OAuthDeviceFlow` rather than a second copy
    of it.
    """
    device = await client.start_device_authorization()
    _emit(on_event, ConnectEvent(
        kind="waiting",
        verification_uri=device.verification_uri,
        user_code=device.user_code,
        scopes=scopes,
    ))
    if device.verification_uri_complete and open_browser is not None:
        open_browser(device.verification_uri_complete)
    try:
        return await client.poll_device_token(device)
    except OAuthTokenError as exc:
        raise ConfigurationError(f"authorization failed: {exc}") from exc


def _finished(
    request: ConnectRequest,
    target: OAuthTarget,
    credential: StoredCredential,
    redirect_uri: str,
    on_event: Listener | None,
) -> ConnectOutcome:
    """Stamp the renewal metadata and report the result. No token escapes."""
    stamped = replace(
        credential, metadata={**credential.metadata, **target.metadata()}
    )
    _emit(on_event, ConnectEvent(
        kind="exchanged", scopes=tuple(sorted(stamped.scopes))
    ))
    return ConnectOutcome(
        connected=True,
        name=request.name,
        scopes=tuple(sorted(stamped.scopes)),
        expires_at=stamped.expires_at,
        redirect_uri=redirect_uri,
        credential=stamped,
    )


@dataclass
class ApiKeyFlow:
    """A credential that is simply a value somebody pastes in.

    **It does not prompt.** It reads ``request.fields`` and reports whatever is
    still missing as :attr:`ConnectOutcome.needs`; collecting those is the
    caller's job, through a :class:`SecretPrompt` for the secret ones. That
    keeps the flow a pure function of its request and keeps the terminal in the
    tier that owns one — and it is what makes the 21 toolsets with no OAuth
    provider connectable at all.
    """

    def supports(self, spec: AuthSpec) -> bool:
        return spec.kind in ("api_key", "basic", "bearer")

    async def connect(
        self,
        request: ConnectRequest,
        spec: AuthSpec,
        *,
        target: OAuthTarget | None = None,
        on_event: Listener | None = None,
    ) -> ConnectOutcome:
        del target
        ok, missing = spec.satisfied_by({**dict(request.fields)})
        if not ok:
            by_name = {f.name: f for f in spec.fields}
            return ConnectOutcome(
                connected=False,
                name=request.name,
                needs=tuple(by_name[n] for n in missing if n in by_name),
                reason=f"'{request.name}' still needs: {', '.join(missing)}",
                redirect_uri="",
            )

        # The first value of the mode that is complete is the token; the rest
        # travel as metadata so a client that needs a site URL beside its token
        # (Jira, GitLab, Airtable) gets both from one connection.
        values = {k: v for k, v in request.fields.items() if v}
        secret_names = [f.name for f in spec.secret_fields if values.get(f.name)]
        token = values[secret_names[0]] if secret_names else next(iter(values.values()))
        credential = StoredCredential(
            token=Secret(token),
            scopes=frozenset(spec.scopes),
            token_type="bearer" if spec.kind != "basic" else "basic",
            metadata={k: v for k, v in values.items() if k not in secret_names},
        )
        _emit(on_event, ConnectEvent(kind="exchanged", detail="value accepted"))
        return ConnectOutcome(
            connected=True,
            name=request.name,
            scopes=tuple(sorted(spec.scopes)),
            credential=credential,
        )


# ---------------------------------------------------------------------------
# Collecting a secret
# ---------------------------------------------------------------------------


@dataclass
class ConsoleSecretPrompt:
    """Reads a secret from a terminal without echoing it.

    Beside `CLIUserInteraction`'s precedent: a stdlib-only terminal adapter may
    live in the library, and a host opts into it. `available()` is false on a
    non-TTY, so a piped or CI invocation degrades to "not collected" rather
    than blocking on a prompt nobody can see.
    """

    def available(self) -> bool:
        return sys.stdin.isatty()

    async def read(self, field: AuthField) -> str:
        if not self.available():
            return ""
        label = field.label or field.name
        example = f" ({field.example})" if field.example else ""
        # Stderr, so `loom connect x --json > out.json` still yields JSON, and
        # so a prompt cannot corrupt `loom mcp --transport stdio`.
        if field.secret:
            import getpass

            return await asyncio.to_thread(
                getpass.getpass, f"  {label}{example}: ", sys.stderr
            )
        print(f"  {label}{example}: ", end="", file=sys.stderr, flush=True)
        line: str = await asyncio.to_thread(sys.stdin.readline)
        return line.strip()


# ---------------------------------------------------------------------------
# The app registration
# ---------------------------------------------------------------------------


class AppRegistrationStore:
    """A provider's ``client_id``/``client_secret``, kept once per machine.

    They are per *provider* rather than per credential — one Atlassian app
    serves both `jira` and `confluence` — and today they must be re-supplied by
    flag or environment on every `loom connect`.

    Kept in the same `CredentialStore` as the tokens, under the reserved key
    ``oauth-app:<provider>``, so they inherit its encryption at rest. That is
    n8n's position, and the reason it is not `.env`: RFC 8252 is explicit that
    a native client cannot treat a distributed secret as confidential, so this
    is not protecting it from the provider's threat model — it is keeping it
    out of a git-tracked file and out of `ps`.
    """

    PREFIX = "oauth-app:"

    def __init__(self, store: Any) -> None:
        self._store = store

    @classmethod
    def key(cls, provider: str) -> str:
        return f"{cls.PREFIX}{provider}"

    async def get(self, provider: str) -> tuple[str, str]:
        """``(client_id, client_secret)``, or two empty strings."""
        peek = getattr(self._store, "peek", None)
        if peek is None:
            return "", ""
        try:
            stored = await peek(self.key(provider))
        except Exception:
            return "", ""
        if stored is None:
            return "", ""
        client_id = str(stored.metadata.get("client_id", ""))
        return client_id, stored.token.reveal()

    async def put(self, provider: str, client_id: str, client_secret: str) -> None:
        await self._store.put(
            self.key(provider),
            StoredCredential(
                token=Secret(client_secret),
                metadata={"client_id": client_id, "provider_id": provider},
            ),
        )

    async def forget(self, provider: str) -> None:
        await self._store.forget(self.key(provider))


def _state() -> str:
    import secrets

    return secrets.token_urlsafe(16)
