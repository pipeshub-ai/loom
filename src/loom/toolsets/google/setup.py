"""One-shot OAuth helper: turn a client id and secret into a refresh token.

    python -m loom.toolsets.google.setup

Opens a browser, catches the redirect on a loopback port, exchanges the code,
and prints the environment variables to paste into ``.env``.

Why a refresh token rather than an access token: an access token is valid for an
hour, so a workflow that runs tomorrow has nothing. The refresh token is what
the toolsets actually want — they mint access tokens from it as needed.

Only the standard library and httpx. Nothing here is imported by the toolsets;
it exists so that setting them up does not mean hand-assembling a URL.
"""

from __future__ import annotations

import argparse
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from typing import Any

#: Stable by default so a Web application client can register one redirect URI
#: and keep it. Arbitrary, but unlikely to collide with a dev server.
DEFAULT_PORT = 8931

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Per-toolset scope sets, and the two combinations worth having by name.
#:
#: ``read`` and ``write`` are composed from the per-toolset entries below rather
#: than written out again, so adding a scope to a toolset cannot leave the
#: combined set behind — which would hand out a credential that works for three
#: toolsets and 403s on the fourth, at the point of use rather than of setup.
_GMAIL_READ = ["https://www.googleapis.com/auth/gmail.readonly"]
_GMAIL_WRITE = [
    *_GMAIL_READ,
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]
_CALENDAR_READ = ["https://www.googleapis.com/auth/calendar.readonly"]
_CALENDAR_WRITE = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]
_DRIVE_READ = ["https://www.googleapis.com/auth/drive.readonly"]
_DRIVE_WRITE = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
]
#: Meet has no separate read/write split worth making: reading a transcript and
#: creating a space are two different scopes, not two halves of one.
_MEET_READ = ["https://www.googleapis.com/auth/meetings.space.readonly"]
_MEET_WRITE = [
    *_MEET_READ,
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.settings",
]


def _unique(*groups: list[str]) -> list[str]:
    """Concatenate scope lists, keeping the first occurrence of each.

    Google rejects nothing for a repeat, but the consent screen lists what it
    is given, and a duplicate reads as a mistake to whoever is approving it.
    """
    seen: dict[str, None] = {}
    for group in groups:
        for scope in group:
            seen.setdefault(scope, None)
    return list(seen)


_GMAIL = _GMAIL_WRITE
_CALENDAR = _unique(_CALENDAR_READ, _CALENDAR_WRITE)
_DRIVE = _unique(_DRIVE_READ, _DRIVE_WRITE)
_MEET = _MEET_WRITE

SCOPE_SETS: dict[str, list[str]] = {
    "read": _unique(_GMAIL_READ, _CALENDAR_READ, _DRIVE_READ, _MEET_READ),
    # The union of the four, not a hand-kept parallel list — so ``--scopes
    # write`` cannot mint a credential that is missing whatever a per-toolset
    # set gained most recently.
    "write": _unique(_GMAIL, _CALENDAR, _DRIVE, _MEET),
    "gmail": _GMAIL,
    "calendar": _CALENDAR,
    "drive": _DRIVE,
    "meet": _MEET,
}

_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>LOOM</title>
<body style="font:16px system-ui;padding:3rem;max-width:32rem">
<h2>%s</h2><p>%s</p></body>"""


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)

    client_id = args.client_id or os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = args.client_secret or os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print(_HOW_TO_GET_A_CLIENT, file=sys.stderr)
        return 2

    scopes = SCOPE_SETS[args.scopes]
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}

    # Bind before building the URL, so the redirect URI names the port actually
    # held rather than one that was free a moment ago.
    try:
        server = _bind(args.port, state, received)
    except OSError:
        print(
            f"Port {args.port} is in use. Free it, or pass --port with another "
            "(remembering to register the matching redirect URI if this is a "
            "Web application client).",
            file=sys.stderr,
        )
        return 2

    port = server.server_port
    # The loopback IP rather than "localhost": Google's own guidance, because
    # localhost resolution trips over some client firewalls.
    redirect_uri = f"http://127.0.0.1:{port}/"

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            # Without these two there is no refresh token: offline asks for one,
            # and consent forces the screen even if this user already approved —
            # a repeat approval otherwise returns an access token alone.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    url = f"{AUTH_URL}?{query}"

    print(f"Requesting: {', '.join(s.rsplit('/', 1)[-1] for s in scopes)}")
    print(f"Listening on {redirect_uri}")
    if port != DEFAULT_PORT and not args.port:
        print(f"  ({DEFAULT_PORT} was busy, so this run took {port}.)")
    print(_MISMATCH_HINT.format(redirect_uri=redirect_uri, port=port))
    print("\nIf a browser does not open, visit:\n" + url + "\n")

    webbrowser.open(url)

    try:
        server.handle_request()  # blocks until Google redirects back
    except KeyboardInterrupt:
        return 1
    finally:
        server.server_close()

    if "code" not in received:
        print(f"Authorization failed: {received.get('error', 'no code returned')}",
              file=sys.stderr)
        return 1

    tokens = _exchange(received["code"], client_id, client_secret, redirect_uri)
    refresh = tokens.get("refresh_token", "")
    access = tokens.get("access_token", "")

    if not refresh:
        print(
            "\nNo refresh token came back. Google only returns one on a fresh "
            "consent — revoke this app at https://myaccount.google.com/permissions "
            "and run again.",
            file=sys.stderr,
        )

    print("\n" + "=" * 62)
    print("Paste into .env at the repo root:\n")
    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    if refresh:
        print(f"GOOGLE_REFRESH_TOKEN={refresh}")
    print("=" * 62)
    if access:
        print(f"\nAccess token (expires in {tokens.get('expires_in', 3600)}s):")
        print(f"GOOGLE_ACCESS_TOKEN={access}")
    print(_TESTING_MODE_WARNING)
    return 0


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m loom.toolsets.google.setup",
        description="Exchange a Google OAuth client for a refresh token.",
    )
    parser.add_argument("--client-id", help="Defaults to $GOOGLE_CLIENT_ID")
    parser.add_argument("--client-secret", help="Defaults to $GOOGLE_CLIENT_SECRET")
    parser.add_argument(
        "--scopes",
        choices=sorted(SCOPE_SETS),
        default="read",
        help="Which access to request (default: read)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help=f"Loopback port (default: {DEFAULT_PORT}, or any free one if busy)",
    )
    return parser.parse_args(argv)


def _bind(
    requested: int, state: str, received: dict[str, str]
) -> http.server.HTTPServer:
    """Bind the callback server, preferring a stable port.

    Stability matters here: with a Web application client the redirect URI has
    to be registered in advance, so a port that changes every run means
    re-registering every run. The default is therefore fixed, and only falls
    back to an ephemeral port when something else holds it — which is announced,
    because that run will not match a registered URI.

    An explicitly requested port is never silently moved; the caller asked for
    that one, most likely because it is the one Google knows about.
    """
    if requested:
        return _serve(requested, state, received)

    try:
        return _serve(DEFAULT_PORT, state, received)
    except OSError:
        return _serve(0, state, received)  # 0 asks the OS for any free port


def _serve(
    port: int, state: str, received: dict[str, str]
) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name BaseHTTPRequestHandler dispatches on
            params = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query
            )
            got = {k: v[0] for k, v in params.items()}

            # A mismatched state means this redirect is not the one we started.
            if got.get("state") != state:
                self._reply(400, b"State mismatch", b"Ignoring this redirect.")
                return

            received.update(got)
            if "code" in got:
                self._reply(
                    200, b"Authorized", b"Return to your terminal; you can close this."
                )
            else:
                self._reply(
                    400,
                    b"Authorization failed",
                    got.get("error", "unknown error").encode(),
                )

        def _reply(self, status: int, title: bytes, detail: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_PAGE % (title, detail))

        def log_message(self, *_: Any) -> None:
            """Silence the default request logging."""

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 300
    threading.current_thread().name = "oauth-callback"
    return server


def _exchange(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> dict[str, Any]:
    import httpx

    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"Token exchange failed: {response.text}", file=sys.stderr)
        raise SystemExit(1)
    result: dict[str, Any] = response.json()
    return result


_HOW_TO_GET_A_CLIENT = """\
Need a client id and secret first. Once, in the Google Cloud console:

  1. Create or pick a project at https://console.cloud.google.com
  2. Enable the APIs you want:
       https://console.cloud.google.com/apis/library/gmail.googleapis.com
       https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
       https://console.cloud.google.com/apis/library/drive.googleapis.com
       https://console.cloud.google.com/apis/library/meet.googleapis.com
  3. Configure the OAuth consent screen (External is fine) and add your own
     Google account under "Test users" — an app in Testing will not let anyone
     else through.
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app
  5. Re-run with:
       --client-id ... --client-secret ...
     or export GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.
"""

_MISMATCH_HINT = """
If Google answers "Error 400: redirect_uri_mismatch", the OAuth client is the
wrong type. A Desktop app client accepts loopback on any port with nothing
registered; a Web application client accepts only exact pre-registered URIs, so
a port chosen at runtime can never match.

  Fix:  create an OAuth client of type "Desktop app" and use its id and secret.
  Or:   register this exact URI on the web client, then pass --port {port}
          {redirect_uri}
"""

_TESTING_MODE_WARNING = """
Note: while the consent screen's publishing status is "Testing", Google expires
the refresh token after 7 days. Publish the app to stop that.
"""


if __name__ == "__main__":
    sys.exit(main())
