"""Resolving a toolset's configuration outside the toolset.

Twenty-seven clients read `os.environ` in `__init__` and cache the result in a
module-level singleton for the life of the process, so a deployment has exactly
one credential set, fixed at first use. This is the seam that replaces that: a
provider says what it has, a chain says which wins, and a session holds the
chain for a run rather than for the process.

Nothing consumes it yet — that is step 3. What these tests pin is the behaviour
step 3 will depend on, and above all the one property that makes the change
safe to land: a deployment that sets environment variables and composes nothing
behaves exactly as it did.
"""

from __future__ import annotations

import asyncio

import pytest

from loom.testing.conformance import verify_credential_provider
from loom.toolsets.manifest import AuthSpec
from loom.toolsets.registry import builtin_catalog
from loom.toolsets.resolution import (
    ChainProvider,
    CredentialProvider,
    EnvironmentProvider,
    StaticProvider,
    ToolsetSession,
    default_providers,
)

JIRA = builtin_catalog().get("jira").auth
GMAIL = builtin_catalog().get("gmail").auth
SLACK = builtin_catalog().get("slack").auth

FULL_JIRA = {
    "JIRA_URL": "https://acme.atlassian.net",
    "JIRA_EMAIL": "a@acme.test",
    "JIRA_API_TOKEN": "tok",
}


class TestPrecedenceIsPositional:
    """No scoring, no "most specific", no provider able to claim priority for
    itself. A host that wants its vault to beat the environment puts it first,
    and can see that it did."""

    async def test_the_earlier_provider_wins(self) -> None:
        session = ToolsetSession(providers=(
            StaticProvider({"JIRA_URL": "https://vault.test"}, id="vault"),
            EnvironmentProvider(environ=FULL_JIRA),
        ))
        resolved = await session.resolve("jira", JIRA)

        assert resolved.values["JIRA_URL"] == "https://vault.test"
        assert resolved.values["JIRA_EMAIL"] == "a@acme.test"

    async def test_reordering_reverses_it(self) -> None:
        session = ToolsetSession(providers=(
            EnvironmentProvider(environ=FULL_JIRA),
            StaticProvider({"JIRA_URL": "https://vault.test"}, id="vault"),
        ))
        resolved = await session.resolve("jira", JIRA)

        assert resolved.values["JIRA_URL"] == "https://acme.atlassian.net"

    async def test_an_empty_value_does_not_win(self) -> None:
        """An unset variable is often `""` rather than absent, and a provider
        that "supplied" it would shadow one that has the real thing."""
        session = ToolsetSession(providers=(
            StaticProvider({"JIRA_URL": ""}, id="vault"),
            EnvironmentProvider(environ=FULL_JIRA),
        ))
        resolved = await session.resolve("jira", JIRA)

        assert resolved.values["JIRA_URL"] == "https://acme.atlassian.net"
        assert resolved.sources["JIRA_URL"] == "environment"


class TestWhereEachValueCameFrom:
    """The first question when a call 401s, and today nothing can answer it."""

    async def test_each_field_records_its_provider(self) -> None:
        session = ToolsetSession(providers=(
            StaticProvider({"JIRA_URL": "https://vault.test"}, id="vault"),
            EnvironmentProvider(environ=FULL_JIRA),
        ))
        resolved = await session.resolve("jira", JIRA)

        assert resolved.sources["JIRA_URL"] == "vault"
        assert resolved.sources["JIRA_API_TOKEN"] == "environment"

    async def test_describe_names_them(self) -> None:
        session = ToolsetSession(providers=(EnvironmentProvider(environ=FULL_JIRA),))
        line = (await session.resolve("jira", JIRA)).describe()

        assert "JIRA_URL<-environment" in line
        assert "missing" not in line

    async def test_describe_says_what_is_short(self) -> None:
        session = ToolsetSession(providers=(
            EnvironmentProvider(environ={"JIRA_URL": "https://acme.test"}),
        ))
        line = (await session.resolve("jira", JIRA)).describe()

        assert "missing" in line and "JIRA_API_TOKEN" in line


class TestAlternativesAreChosenNotGuessed:
    """Google takes a ready-made access token, or a client-id/secret/refresh
    trio, or a service account file. Reporting a working deployment as missing
    five variables is as wrong as reporting an empty one as configured."""

    async def test_a_complete_mode_is_chosen(self) -> None:
        session = ToolsetSession(providers=(
            EnvironmentProvider(environ={"GOOGLE_ACCESS_TOKEN": "ya29."}),
        ))
        resolved = await session.resolve("gmail", GMAIL)

        assert resolved.complete
        assert resolved.mode == "token"

    async def test_a_half_set_mode_reports_only_its_own_gap(self) -> None:
        """Two thirds through the trio, the answer must be the missing third —
        not "set GOOGLE_ACCESS_TOKEN", which is the other path entirely."""
        session = ToolsetSession(providers=(EnvironmentProvider(environ={
            "GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret",
        }),))
        resolved = await session.resolve("gmail", GMAIL)

        assert not resolved.complete
        assert resolved.missing == ("GOOGLE_REFRESH_TOKEN",)
        assert resolved.mode == "refresh"

    async def test_a_toolset_without_alternatives_has_no_mode(self) -> None:
        session = ToolsetSession(providers=(EnvironmentProvider(environ=FULL_JIRA),))
        assert (await session.resolve("jira", JIRA)).mode == ""


class TestAbsenceDegradesToWhatShipped:
    """The compatibility guarantee of the whole change."""

    def test_the_default_chain_is_the_environment_and_nothing_else(self) -> None:
        providers = default_providers()

        assert [p.id for p in providers] == ["environment"]

    async def test_a_default_session_reads_the_real_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JIRA_URL", "https://from-os.test")
        monkeypatch.setenv("JIRA_EMAIL", "e@x.test")
        monkeypatch.setenv("JIRA_API_TOKEN", "t")

        resolved = await ToolsetSession().resolve("jira", JIRA)

        assert resolved.values["JIRA_URL"] == "https://from-os.test"
        assert resolved.complete


class TestTwoTenantsInOneProcess:
    """Impossible today, and the reason this is worth doing: the singleton is
    built once from the first environment it sees and is then the only
    credential set the process can ever have."""

    async def test_concurrent_sessions_do_not_leak(self) -> None:
        a = ToolsetSession(providers=(StaticProvider(
            {**FULL_JIRA, "JIRA_API_TOKEN": "tenant-a"}, id="a"),))
        b = ToolsetSession(providers=(StaticProvider(
            {**FULL_JIRA, "JIRA_API_TOKEN": "tenant-b"}, id="b"),))

        first, second = await asyncio.gather(
            a.resolve("jira", JIRA), b.resolve("jira", JIRA)
        )

        assert first.values["JIRA_API_TOKEN"] == "tenant-a"
        assert second.values["JIRA_API_TOKEN"] == "tenant-b"


class TestAProviderIsFilteredToTheSpec:
    async def test_one_toolsets_secret_is_not_handed_to_another(self) -> None:
        """A host passing one mapping for everything is the ordinary case."""
        everything = StaticProvider({**FULL_JIRA, "STRIPE_API_KEY": "sk_live_x"})

        supplied = await everything.supply(JIRA)

        assert "STRIPE_API_KEY" not in supplied


class TestTwoFieldsCanShareAnArg:
    """Slack reads `SLACK_BOT_TOKEN or SLACK_TOKEN`, so both feed `token` and
    resolution must keep them apart for step 3 to pick the first present."""

    async def test_both_are_resolved_separately(self) -> None:
        session = ToolsetSession(providers=(EnvironmentProvider(environ={
            "SLACK_BOT_TOKEN": "xoxb-", "SLACK_TOKEN": "xoxp-",
        }),))
        resolved = await session.resolve("slack", SLACK)

        assert resolved.values["SLACK_BOT_TOKEN"] == "xoxb-"
        assert resolved.values["SLACK_TOKEN"] == "xoxp-"
        assert [f.arg for f in SLACK.fields] == ["token", "token"]


class TestTheStoreKeyIsCarriedNotResolved:
    """`resolve_bearer_token` selects a wire scheme and is called per request,
    never cached, so a token refreshed mid-run is picked up. Resolving it once
    per run as a field value would quietly destroy that."""

    async def test_the_credential_name_travels_with_the_result(self) -> None:
        resolved = await ToolsetSession().resolve("jira", JIRA)

        assert resolved.credential == JIRA.credential == "jira"


class TestTheShippedProvidersConform:
    @pytest.mark.parametrize("provider", [
        EnvironmentProvider(environ={"CONFORMANCE_TOKEN": "x"}),
        StaticProvider({"CONFORMANCE_TOKEN": "x"}),
        ChainProvider((StaticProvider({"CONFORMANCE_TOKEN": "x"}),)),
    ], ids=["environment", "static", "chain"])
    async def test_it(self, provider: CredentialProvider) -> None:
        await verify_credential_provider(provider, holds={"CONFORMANCE_TOKEN": "x"})

    def test_they_satisfy_the_protocol(self) -> None:
        assert isinstance(EnvironmentProvider(), CredentialProvider)
        assert isinstance(StaticProvider({}), CredentialProvider)


class TestTheKitCatchesABadProvider:
    """A conformance kit that cannot fail is a kit nobody should trust."""

    async def test_a_provider_that_raises_on_absence(self) -> None:
        class Raises:
            id = "raises"

            async def supply(self, spec: AuthSpec) -> dict[str, str]:
                if not any(f.name == "CONFORMANCE_TOKEN" for f in spec.fields):
                    raise KeyError("nothing here")
                return {"CONFORMANCE_TOKEN": "x"}

        with pytest.raises(AssertionError, match="stops every provider behind"):
            await verify_credential_provider(Raises())

    async def test_a_provider_that_ignores_the_spec(self) -> None:
        class Leaks:
            id = "leaks"

            async def supply(self, spec: AuthSpec) -> dict[str, str]:
                return {"CONFORMANCE_TOKEN": "x", "SOMEONE_ELSES_SECRET": "y"}

        with pytest.raises(AssertionError, match="does not declare"):
            await verify_credential_provider(Leaks())

    async def test_a_provider_with_no_id(self) -> None:
        class Nameless:
            id = ""

            async def supply(self, spec: AuthSpec) -> dict[str, str]:
                return {}

        with pytest.raises(AssertionError, match="non-empty string id"):
            await verify_credential_provider(Nameless())


class TestEveryShippedToolsetResolves:
    """The port has to work for all 27, not for the two that were designed
    against. A spec it cannot walk is one step 3 cannot construct."""

    @pytest.mark.parametrize("toolset", sorted(builtin_catalog().toolset_ids))
    async def test_an_empty_chain_reports_rather_than_raises(self, toolset: str) -> None:
        spec = builtin_catalog().get(toolset).auth
        resolved = await ToolsetSession(providers=()).resolve(toolset, spec)

        assert resolved.toolset == toolset
        assert resolved.values == {}
        assert isinstance(resolved.describe(), str)

    @pytest.mark.parametrize("toolset", sorted(builtin_catalog().toolset_ids))
    async def test_a_full_environment_satisfies_it(self, toolset: str) -> None:
        spec = builtin_catalog().get(toolset).auth
        environ = {f.name: "value" for f in spec.fields}
        resolved = await ToolsetSession(providers=(
            EnvironmentProvider(environ=environ),
        )).resolve(toolset, spec)

        assert resolved.complete, resolved.describe()


class TestAChainIsItselfAProvider:
    async def test_chains_nest(self) -> None:
        """So a host can group its own sources and hand the group over as one."""
        inner = ChainProvider((
            StaticProvider({"JIRA_URL": "https://inner.test"}, id="inner"),
        ))
        outer = ChainProvider((
            StaticProvider({"JIRA_EMAIL": "e@x.test"}, id="outer"), inner,
        ))

        supplied = await outer.supply(JIRA)

        assert supplied == {"JIRA_EMAIL": "e@x.test", "JIRA_URL": "https://inner.test"}

    async def test_a_field_supplied_by_a_custom_provider_is_attributed_to_it(self) -> None:
        """Not to the chain that contained it — otherwise `sources` answers
        "chain", which is exactly as useful as answering nothing."""
        session = ToolsetSession(providers=(
            ChainProvider((StaticProvider({"JIRA_URL": "https://x.test"}, id="deep"),)),
        ))
        resolved = await session.resolve("jira", JIRA)

        assert resolved.sources["JIRA_URL"] == "deep"
