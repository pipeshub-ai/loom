"""Obtaining a credential, with no browser and no authorization server.

The flows moved out of `cli/auth_commands.py` because every line of them took
an `argparse.Namespace`, so the session, the MCP server and the coding agent
— none of which has one — could not connect anything. `tests/test_cli_auth.py`
is the regression bar for that move and passes unchanged; this covers what the
move *added*.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom.connectors.credentials import MemoryCredentialStore, StoredCredential
from loom.connectors.flows import (
    ApiKeyFlow,
    AppRegistrationStore,
    ConnectEvent,
    ConnectFlow,
    ConnectOutcome,
    ConnectRequest,
    ConsoleSecretPrompt,
    OAuthBrowserFlow,
    OAuthTarget,
    SecretPrompt,
)
from loom.core.exceptions import ConfigurationError
from loom.core.secret import Secret
from loom.facade import LocalFacade
from loom.runtime.engine import Runtime
from loom.stores import MemoryStore
from loom.testing.conformance import verify_connect_flow
from loom.toolsets.manifest import AuthField, AuthSpec

KEY_SPEC = AuthSpec(
    kind="api_key",
    credential="acme",
    fields=(
        AuthField(name="ACME_URL", label="Site URL", secret=False),
        AuthField(name="ACME_TOKEN", label="API token"),
    ),
)


class TestTheEventSeam:
    """A flow reports; only a surface renders.

    `_run_pkce_flow` took a `loom.cli.output.Printer` and wrote four lines
    through it. Lifting that as-is would have put a `[cli]`-flavoured renderer
    inside `loom/connectors/` and created the second library-to-CLI import edge
    in the codebase — see `tests/test_layering.py`.
    """

    async def test_events_are_objects_not_lines(self) -> None:
        seen: list[ConnectEvent] = []
        await ApiKeyFlow().connect(
            ConnectRequest(name="acme", fields={"ACME_URL": "u", "ACME_TOKEN": "t"}),
            KEY_SPEC,
            on_event=seen.append,
        )
        assert seen and all(isinstance(e, ConnectEvent) for e in seen)

    async def test_a_renderer_that_raises_cannot_lose_a_credential(self) -> None:
        """Fails open, the rule the non-deciding hook families follow.

        The credential has already been obtained by the time anything is
        drawn; a broken formatter must not destroy a valid result.
        """
        def explode(event: ConnectEvent) -> None:
            raise RuntimeError("the renderer is broken")

        outcome = await ApiKeyFlow().connect(
            ConnectRequest(name="acme", fields={"ACME_URL": "u", "ACME_TOKEN": "t"}),
            KEY_SPEC,
            on_event=explode,
        )
        assert outcome.connected
        assert outcome.credential is not None

    async def test_no_listener_is_fine(self) -> None:
        outcome = await ApiKeyFlow().connect(
            ConnectRequest(name="acme", fields={"ACME_URL": "u", "ACME_TOKEN": "t"}),
            KEY_SPEC,
        )
        assert outcome.connected


class TestNoSecretEscapes:
    async def test_the_outcome_repr_holds_no_token(self) -> None:
        """It crosses a facade to a CLI, an MCP client and a model's context."""
        outcome = await ApiKeyFlow().connect(
            ConnectRequest(
                name="acme", fields={"ACME_URL": "u", "ACME_TOKEN": "sk_live_secret"}
            ),
            KEY_SPEC,
        )
        assert "sk_live_secret" not in repr(outcome)
        assert outcome.credential is not None
        assert outcome.credential.token.reveal() == "sk_live_secret"

    async def test_a_non_secret_field_travels_as_metadata(self) -> None:
        """A site URL is half the credential for Jira, GitLab and Airtable, and
        is not a secret. Losing it means a connected toolset with no base URL."""
        outcome = await ApiKeyFlow().connect(
            ConnectRequest(
                name="acme", fields={"ACME_URL": "https://acme.test", "ACME_TOKEN": "t"}
            ),
            KEY_SPEC,
        )
        assert outcome.credential is not None
        assert outcome.credential.metadata["ACME_URL"] == "https://acme.test"


class TestApiKeyFlowDoesNotPrompt:
    """Collecting is the caller's job, which keeps the terminal in the tier
    that owns one and the flow a pure function of its request."""

    async def test_it_reports_what_is_missing(self) -> None:
        outcome = await ApiKeyFlow().connect(
            ConnectRequest(name="acme", fields={"ACME_URL": "u"}), KEY_SPEC
        )
        assert not outcome.connected
        assert [f.name for f in outcome.needs] == ["ACME_TOKEN"]
        assert "ACME_TOKEN" in outcome.reason

    async def test_a_refusal_always_says_why(self) -> None:
        outcome = await ApiKeyFlow().connect(ConnectRequest(name="acme"), KEY_SPEC)
        assert outcome.reason


class TestTheBrowserFlowStopsBeforeAskingForAnything:
    async def test_no_client_id_reports_the_redirect_uri_first(self) -> None:
        """n8n's read-only callback field, as an outcome.

        Registering the redirect URI on the provider is the one step nobody can
        do for you, and finding out afterwards means doing the whole flow
        again — so it is reported before a browser is opened, not after.
        """
        seen: list[ConnectEvent] = []
        target = OAuthTarget(name="acme", token_endpoint="https://auth.test/token")
        outcome = await OAuthBrowserFlow(open_browser=None).connect(
            ConnectRequest(name="acme", redirect_port=0),
            AuthSpec(kind="oauth2", credential="acme", setup_url="https://acme.test/apps"),
            target=target,
            on_event=seen.append,
        )
        assert not outcome.connected
        assert outcome.redirect_uri.startswith("http://127.0.0.1:")
        assert "redirect URI" in outcome.reason

        kinds = [e.kind for e in seen]
        assert kinds[0] == "redirect_ready", kinds
        assert "opening_browser" not in kinds
        assert seen[-1].setup_url == "https://acme.test/apps"

    async def test_it_needs_a_target(self) -> None:
        with pytest.raises(ConfigurationError, match="needs a target"):
            await OAuthBrowserFlow().connect(
                ConnectRequest(name="acme"), AuthSpec(kind="oauth2", credential="acme")
            )


class TestTargetFromProvider:
    """The join `loom connect jira` did not have."""

    def test_it_maps_a_credential_to_its_provider(self) -> None:
        target = OAuthTarget.from_provider("jira", "atlassian", client_id="x")
        assert target.token_endpoint == "https://auth.atlassian.com/oauth/token"
        assert target.provider_id == "atlassian"

    def test_an_unknown_provider_names_the_known_ones(self) -> None:
        with pytest.raises(ConfigurationError, match="atlassian"):
            OAuthTarget.from_provider("jira", "jira")

    def test_the_metadata_is_what_a_refresher_needs(self) -> None:
        target = OAuthTarget.from_provider(
            "jira", "atlassian", client_id="cid", client_secret="sec"
        )
        meta = target.metadata()
        assert meta["token_endpoint"] and meta["client_id"] == "cid"
        assert meta["provider_id"] == "atlassian"


class TestTheAppRegistration:
    async def test_it_round_trips_under_a_reserved_key(self) -> None:
        store = MemoryCredentialStore()
        apps = AppRegistrationStore(store)
        await apps.put("atlassian", "cid", "sec")

        assert await apps.get("atlassian") == ("cid", "sec")
        # Reserved prefix, so it cannot collide with a toolset credential and
        # is recognisable in `loom whoami`.
        assert "oauth-app:atlassian" in await store.names()

    async def test_an_absent_registration_is_two_empty_strings(self) -> None:
        assert await AppRegistrationStore(MemoryCredentialStore()).get("nope") == ("", "")


class TestTheConformanceKit:
    async def test_a_shipped_flow_passes_it(self) -> None:
        await verify_connect_flow(
            ApiKeyFlow(),
            spec=KEY_SPEC,
            request=ConnectRequest(
                name="acme",
                fields={"ACME_URL": "https://acme.test", "ACME_TOKEN": "sk_test_realistic"},
            ),
        )

    async def test_a_one_character_token_is_not_a_leak(self) -> None:
        """The kit's own false positive, pinned.

        A substring test on a short value is a coincidence detector — `"t"` is
        inside "connected" — and a conformance kit that fails a correct flow is
        one people switch off.
        """
        await verify_connect_flow(
            ApiKeyFlow(),
            spec=KEY_SPEC,
            request=ConnectRequest(name="acme", fields={"ACME_URL": "u", "ACME_TOKEN": "t"}),
        )

    async def test_it_catches_a_flow_that_leaks_its_token(self) -> None:
        class Leaky:
            def supports(self, spec: Any) -> bool:
                return True

            async def connect(self, request: Any, spec: Any, **kw: Any) -> Any:
                credential = StoredCredential(token=Secret("sk_live_leaked"))
                # `reason` is an ordinary string field, and this is exactly how
                # a token ends up in one: by being helpful.
                return ConnectOutcome(
                    connected=True,
                    name=request.name,
                    reason="stored sk_live_leaked",
                    credential=credential,
                )

        with pytest.raises(AssertionError, match="repr contains the token"):
            await verify_connect_flow(
                Leaky(), spec=KEY_SPEC, request=ConnectRequest(name="acme")
            )

    async def test_it_catches_a_refusal_with_no_reason(self) -> None:
        class Silent:
            def supports(self, spec: Any) -> bool:
                return True

            async def connect(self, request: Any, spec: Any, **kw: Any) -> Any:
                return ConnectOutcome(connected=False, name=request.name)

        with pytest.raises(AssertionError, match="must say why"):
            await verify_connect_flow(
                Silent(), spec=KEY_SPEC, request=ConnectRequest(name="acme")
            )


class TestTheProtocols:
    def test_the_shipped_flows_satisfy_it(self) -> None:
        assert isinstance(ApiKeyFlow(), ConnectFlow)
        assert isinstance(OAuthBrowserFlow(), ConnectFlow)

    def test_the_console_prompt_satisfies_the_secret_protocol(self) -> None:
        assert isinstance(ConsoleSecretPrompt(), SecretPrompt)

    def test_it_is_unavailable_without_a_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-TTY degrades to "not collected" rather than blocking on a
        prompt nobody can see — the rule `CLIUserInteraction` already follows."""
        monkeypatch.setattr("sys.stdin", type("NoTTY", (), {"isatty": lambda self: False})())
        assert ConsoleSecretPrompt().available() is False


class TestTheFacadeSeams:
    async def test_no_flow_composed_means_it_says_so_rather_than_prompting(self) -> None:
        """Absence degrades, the way `ask_user` and `observe_target` do.

        This is what makes `--json`, a pipe and CI correct with no `isatty`
        check written anywhere in the connection plane.
        """
        runtime = Runtime(store=MemoryStore())
        runtime.credentials = MemoryCredentialStore()
        result = await LocalFacade(runtime).connect("jira")

        assert result["connected"] is False
        assert "no connect flow" in result["reason"]

    async def test_the_cli_composes_both_adapters(self) -> None:
        """The wiring, not the unit: `interaction.py` shipped complete, fully
        unit-tested, and passed in by nobody."""
        import inspect

        from loom.cli import targets

        source = inspect.getsource(targets.resolve)
        assert "connect_flow=OAuthBrowserFlow()" in source
        # `_secret_prompt()` rather than `ConsoleSecretPrompt()` directly: the
        # CLI wraps it so the progress region is held while it asks, which is
        # a CLI concern and cannot live in `loom.connectors.flows`.
        assert "secret_prompt=_secret_prompt()" in source
        assert "on_connect_event=" in source

    async def test_disconnect_reports_whether_anything_was_there(self) -> None:
        runtime = Runtime(store=MemoryStore())
        store = MemoryCredentialStore()
        await store.put("acme", StoredCredential(token=Secret("t")))
        runtime.credentials = store
        facade = LocalFacade(runtime)

        assert (await facade.disconnect("acme"))["disconnected"] is True
        assert (await facade.disconnect("acme"))["disconnected"] is False

    async def test_a_stored_credential_is_what_connections_then_reports(self) -> None:
        """The loop closed: connect writes the name `ConnectionInspector` reads
        and the toolset client looks up. All three used to be different."""
        runtime = Runtime(store=MemoryStore())
        store = MemoryCredentialStore()
        await store.put("jira", StoredCredential(token=Secret("t")))
        runtime.credentials = store

        rows = await LocalFacade(runtime).connections("jira")
        assert rows[0]["state"] == "connected"
