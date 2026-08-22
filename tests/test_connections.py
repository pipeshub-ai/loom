"""Is this toolset connected here?

Nothing could answer it. `loom doctor` reported *"27 toolsets reachable"* and
*"3 credentials stored"* on the same screen and could not say whether Jira was
usable, because the credential name a client reads was a keyword default inside
one file. `AuthSpec` made it declarable; `ConnectionInspector` reads it.

Two properties matter more than any single state, and both are asserted:

* **it changes nothing by reporting** — `peek`, never `get`, so a status read
  cannot renew a credential or raise on an expired one, which is what makes it
  safe to call while building a prompt;
* **it costs nothing** — no toolset module is imported and no socket is opened,
  which is what makes it safe to call on every authoring job.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from loom.connectors.credentials import MemoryCredentialStore, StoredCredential
from loom.connectors.inspect import (
    ConnectionInspector,
    ConnectionState,
    ConnectionStatus,
)
from loom.core.secret import Secret
from loom.facade import LocalFacade, RemoteFacade
from loom.runtime.clock import ManualClock
from loom.runtime.engine import Runtime
from loom.stores import MemoryStore
from loom.toolsets.registry import builtin_catalog

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

#: A full Jira environment, so the fallback path can be exercised on its own.
JIRA_ENV = {
    "JIRA_URL": "https://acme.atlassian.net",
    "JIRA_EMAIL": "a@example.com",
    "JIRA_API_TOKEN": "t",
}


def _stored(*, expires_in: timedelta | None) -> StoredCredential:
    return StoredCredential(
        token=Secret("tok"),
        refresh_token=Secret("ref"),
        expires_at=None if expires_in is None else NOW + expires_in,
        issued_at=NOW - timedelta(hours=1),
    )


async def _inspector(
    *, stored: dict[str, StoredCredential] | None = None, environ: dict[str, str] | None = None
) -> ConnectionInspector:
    store = MemoryCredentialStore(clock=ManualClock(NOW))
    for name, credential in (stored or {}).items():
        await store.put(name, credential)
    return ConnectionInspector(
        builtin_catalog(), store, environ=environ or {}, clock=ManualClock(NOW)
    )


class TestTheSixStates:
    """Six rather than connected/not, because each leads somewhere different."""

    async def test_none_when_the_api_needs_no_credential(self) -> None:
        status = await (await _inspector()).status("duckduckgo")
        assert status.state is ConnectionState.NONE
        assert status.usable
        assert status.how == ""

    async def test_missing_when_nothing_is_stored_or_set(self) -> None:
        status = await (await _inspector()).status("jira")
        assert status.state is ConnectionState.MISSING
        assert not status.usable
        assert status.missing_fields == tuple(JIRA_ENV)
        assert status.how == "loom connect jira"

    async def test_env_when_the_environment_satisfies_it(self) -> None:
        status = await (await _inspector(environ=JIRA_ENV)).status("jira")
        assert status.state is ConnectionState.ENV
        assert status.usable
        assert status.missing_fields == ()

    async def test_env_is_not_connected(self) -> None:
        """They look the same to a caller and differ in what to do.

        There is nothing to renew and nothing to disconnect, so offering an
        OAuth flow here is advice for a state the machine is not in — the
        `loom refresh --all` failure, one subsystem over.
        """
        status = await (await _inspector(environ=JIRA_ENV)).status("jira")
        assert status.state is not ConnectionState.CONNECTED
        assert status.how == ""

    async def test_connected_when_a_credential_is_stored_and_fresh(self) -> None:
        inspector = await _inspector(stored={"jira": _stored(expires_in=timedelta(hours=6))})
        status = await inspector.status("jira")
        assert status.state is ConnectionState.CONNECTED
        assert status.usable
        assert status.how == ""

    async def test_due_when_renewal_is_imminent(self) -> None:
        """Its own state, for the reason `loom whoami` reports it.

        The credential *works*, so this is never an error — but the same
        credential reported `due` repeatedly is renewal failing, hours before
        it becomes `expired` and something breaks.
        """
        inspector = await _inspector(stored={"jira": _stored(expires_in=timedelta(minutes=2))})
        status = await inspector.status("jira")
        assert status.state is ConnectionState.DUE
        assert status.usable

    async def test_expired_when_it_has_run_out(self) -> None:
        inspector = await _inspector(stored={"jira": _stored(expires_in=timedelta(minutes=-5))})
        status = await inspector.status("jira")
        assert status.state is ConnectionState.EXPIRED
        assert not status.usable
        assert status.how == "loom refresh jira"

    async def test_a_stored_credential_beats_the_environment(self) -> None:
        inspector = await _inspector(
            stored={"jira": _stored(expires_in=timedelta(hours=6))}, environ=JIRA_ENV
        )
        assert (await inspector.status("jira")).state is ConnectionState.CONNECTED


class TestAlternativeCredentialModes:
    """Several services accept more than one way of authenticating.

    A rule wanting every required field reports a Google deployment holding a
    valid refresh token as missing an access token it does not need; one
    wanting any field reports an empty environment holding a stray client id as
    configured. `AuthSpec.mode` is what says which fields go together, and it
    mirrors `GoogleCredentials.mode` / `MicrosoftCredentials.mode`.
    """

    @pytest.mark.parametrize(
        ("toolset", "environ"),
        [
            ("gmail", {"GOOGLE_ACCESS_TOKEN": "t"}),
            ("gmail", {"GOOGLE_CLIENT_ID": "c", "GOOGLE_CLIENT_SECRET": "s",
                       "GOOGLE_REFRESH_TOKEN": "r"}),
            ("gmail", {"GOOGLE_SERVICE_ACCOUNT_FILE": "/tmp/sa.json"}),
            ("teams", {"MS_TENANT_ID": "t", "MS_CLIENT_ID": "c", "MS_CLIENT_SECRET": "s"}),
            ("teams", {"AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c",
                       "AZURE_CLIENT_SECRET": "s"}),
            ("teams", {"MS_GRAPH_ACCESS_TOKEN": "t"}),
            ("clickup", {"CLICKUP_OAUTH_TOKEN": "o"}),
            ("gitlab", {"GITLAB_OAUTH_TOKEN": "o"}),
        ],
    )
    async def test_any_complete_mode_configures_it(
        self, toolset: str, environ: dict[str, str]
    ) -> None:
        status = await (await _inspector(environ=environ)).status(toolset)
        assert status.state is ConnectionState.ENV, status.detail

    async def test_half_a_mode_is_not_a_mode(self) -> None:
        environ = {"GOOGLE_CLIENT_ID": "c", "GOOGLE_CLIENT_SECRET": "s"}
        status = await (await _inspector(environ=environ)).status("gmail")
        assert status.state is ConnectionState.MISSING

    async def test_the_shortfall_names_the_nearest_mode_only(self) -> None:
        """Not the union of every alternative.

        Someone reading this wants the shortest path to working. Listing all of
        them tells a deployment two thirds of the way through the refresh trio
        that it also needs an access token and a service account file.
        """
        environ = {"GOOGLE_CLIENT_ID": "c", "GOOGLE_CLIENT_SECRET": "s"}
        status = await (await _inspector(environ=environ)).status("gmail")
        assert status.missing_fields == ("GOOGLE_REFRESH_TOKEN",)


class TestItChangesNothingByReporting:
    async def test_an_expired_credential_is_reported_not_raised(self) -> None:
        """`store.get()` raises `AuthExpired` here; `peek` does not.

        That difference is what makes this safe to call while building a
        prompt: a status read must not fail because of the state it is
        reporting, and must not renew it either — the position
        `SubscriptionManager` takes about quarantining a subscriber.
        """
        inspector = await _inspector(stored={"jira": _stored(expires_in=timedelta(minutes=-5))})
        assert (await inspector.status("jira")).state is ConnectionState.EXPIRED

    async def test_a_store_that_cannot_be_read_is_not_an_exception(self) -> None:
        class Broken:
            refresh_policy = None

            async def peek(self, name: str) -> Any:
                raise OSError("credential file is unreadable")

        inspector = ConnectionInspector(
            builtin_catalog(), Broken(), environ={}, clock=ManualClock(NOW)
        )
        # `loom whoami` reports an unreadable store in detail. Here it only has
        # to not blow up in the middle of a prompt.
        assert (await inspector.status("jira")).state is ConnectionState.MISSING

    async def test_no_secret_crosses_the_boundary(self) -> None:
        inspector = await _inspector(
            stored={"jira": _stored(expires_in=timedelta(hours=6))}, environ=JIRA_ENV
        )
        rendered = repr((await inspector.status("jira")).model_dump(mode="json"))
        assert "tok" not in rendered
        assert "ref" not in rendered


class TestItCostsNothing:
    async def test_every_shipped_toolset_answers(self) -> None:
        statuses = await (await _inspector()).all()
        assert len(statuses) > 20
        assert all(isinstance(s, ConnectionStatus) for s in statuses)

    def test_it_imports_no_toolset_client_and_opens_no_socket(self) -> None:
        """A subprocess, because `sys.modules` is shared with every other test.

        The claim is Layer 1: reading a *manifest* must not drag in `httpx`, a
        vendor SDK, or a network call. It is what lets this run on every
        authoring job and every `loom doctor` rather than being something a
        caller has to think about.
        """
        program = """
import asyncio, socket, sys

# Outbound connections, not socket *creation*: asyncio's event loop makes a
# self-pipe out of a socketpair, and refusing that tests the harness.
def refuse(*a, **k):
    raise AssertionError("ConnectionInspector reached the network")

socket.socket.connect = refuse
socket.create_connection = refuse

from loom.connectors.inspect import ConnectionInspector
from loom.toolsets.registry import builtin_catalog

statuses = asyncio.run(ConnectionInspector(builtin_catalog(), None, environ={}).all())
assert len(statuses) > 20, statuses

leaked = [m for m in sys.modules if m.startswith("loom.toolsets.") and m.endswith(".tools")]
assert not leaked, f"imported toolset clients: {leaked}"
assert "httpx" not in sys.modules, "pulled in httpx"
print("ok")
"""
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ok" in result.stdout


class TestTheAdviceIsActionable:
    async def test_a_toolset_with_no_store_path_is_told_nothing(self) -> None:
        """`loom connect stripe` would store a token the client never reads.

        Advice with no move behind it is the failure `loom refresh --all` and
        `loom runs --status running` were both fixed for, so the field is empty
        rather than confidently wrong.
        """
        status = await (await _inspector()).status("stripe")
        assert status.state is ConnectionState.MISSING
        assert status.credential == ""
        assert status.how == ""
        assert status.missing_fields == ("STRIPE_API_KEY",)

    async def test_a_store_backed_toolset_names_the_command(self) -> None:
        status = await (await _inspector()).status("slack")
        assert status.how == "loom connect slack"
        assert status.provider == "slack"

    async def test_scopes_are_the_ones_a_flow_must_request(self) -> None:
        status = await (await _inspector()).status("jira")
        assert "read:jira-work" in status.scopes
        assert "write:jira-work" in status.scopes
        assert "offline_access" in status.scopes


class TestTheSurfaces:
    async def test_the_local_facade_answers(self) -> None:
        facade = LocalFacade(Runtime(store=MemoryStore()))
        rows = await facade.connections("jira")
        assert rows[0]["toolset"] == "jira"
        assert rows[0]["provider"] == "atlassian"

    async def test_the_local_facade_lists_everything(self) -> None:
        rows = await LocalFacade(Runtime(store=MemoryStore())).connections()
        assert len(rows) > 20

    async def test_a_remote_facade_refuses_with_the_reason(self) -> None:
        """A connection belongs to the process that will make the call.

        A server's toolsets read the server's store and the server's
        environment, and neither is visible from a client — so answering from
        here would report this machine's state as the server's.
        """
        from loom.core.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError, match="process that will make the call"):
            await RemoteFacade(client=None).connections()  # type: ignore[arg-type]

    async def test_the_authorized_facade_gates_it_on_reading(self) -> None:
        from loom.core.exceptions import InsufficientScope
        from loom.identity.facade import AuthorizedFacade
        from loom.identity.principal import Principal

        inner = LocalFacade(Runtime(store=MemoryStore()))
        reader = Principal(subject="s", scopes=frozenset({"workflows:read"}))
        allowed = AuthorizedFacade(inner, reader)
        assert await allowed.connections("jira")

        denied = AuthorizedFacade(inner, Principal(subject="s", scopes=frozenset()))
        with pytest.raises(InsufficientScope):
            await denied.connections("jira")
