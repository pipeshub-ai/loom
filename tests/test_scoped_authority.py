"""A caller's scopes narrow what the run it starts may reach.

`scopes_to_grant` was written for exactly this, is property-tested with
Hypothesis to never widen, and was **called from no production path at all** —
referenced only by docstrings and its own tests. So a token scoped to
`jira:read` started runs carrying the workflow's full declaration, and the scope
on the token described nothing that actually happened. An access-control
mechanism that is only documented is worse than none: it is relied on.

The scopes ride on the *record*, not on the call, and that is the load-bearing
part. A run parks on a timer or an event and is later resumed by the scheduler,
where no caller is present to ask — so an authority derived from the caller has
to survive parking or it silently widens back on the first resume.
"""

from __future__ import annotations

from loom import Context, Runtime, step, workflow
from loom.facade import LocalFacade
from loom.identity.facade import PRINCIPAL_KEY, SCOPES_KEY, AuthorizedFacade
from loom.identity.principal import ServicePrincipal
from loom.runtime.context import _authority_for
from loom.runtime.effects import GuardedBroker
from loom.security.authority import Authority
from loom.security.grants import GrantSet
from loom.stores.memory import MemoryStore


@step
async def jira_search_issues(jql: str) -> list[str]:
    return ["PA-1"]


@step
async def slack_post_message(text: str) -> str:
    return "sent"


@workflow(name="cross", grants=GrantSet(toolsets=["jira", "slack"]))
async def cross(ctx: Context, _: None = None) -> str:
    found = await ctx.step(jira_search_issues, "project = PA")
    await ctx.step(slack_post_message, f"{len(found)} issues")
    return "done"


def _runtime() -> Runtime:
    rt = Runtime(store=MemoryStore(), broker=GuardedBroker(), authority=Authority())
    rt.register(cross)
    return rt


class TestTheScopesReachTheRecord:
    async def test_start_pins_them_beside_the_subject(self) -> None:
        rt = _runtime()
        facade = AuthorizedFacade(
            LocalFacade(runtime=rt),
            ServicePrincipal(subject="svc", scopes=frozenset({"runs:write", "jira"})),
        )
        try:
            result = await facade.start("cross", None)
            record = await rt.store.get_execution(result["run_id"])
        finally:
            await rt.shutdown()

        assert record.metadata[PRINCIPAL_KEY] == "svc"
        assert "jira" in record.metadata[SCOPES_KEY]

    async def test_an_unauthenticated_start_records_nothing(self) -> None:
        """A bare Runtime is not scope-limited, and must not become so by
        accident — the absence of a caller is not the absence of permission."""
        rt = _runtime()
        try:
            result = await rt.run(cross)
            record = await rt.store.get_execution(result.run_id)
        finally:
            await rt.shutdown()

        assert SCOPES_KEY not in (record.metadata or {})


class TestTheAuthorityIsNarrowed:
    """`_authority_for` is where the recorded scopes become a decision."""

    def _authority(self, scopes: list[str] | None):
        from loom.core.models import ExecutionRecord

        rt = _runtime()
        metadata = {SCOPES_KEY: scopes} if scopes is not None else {}
        record = ExecutionRecord(run_id="r1", workflow="cross", metadata=metadata)
        try:
            return _authority_for(rt, rt.workflows["cross"], record)
        finally:
            rt._background.clear()

    def test_no_scopes_leaves_the_declaration_alone(self) -> None:
        assert set(self._authority(None).grant.toolsets) == {"jira", "slack"}

    def test_a_narrow_token_narrows_the_grant(self) -> None:
        """The whole point: the token said jira, so slack is gone."""
        granted = self._authority(["jira"]).grant

        assert set(granted.toolsets) == {"jira"}

    def test_a_token_cannot_widen_beyond_the_declaration(self) -> None:
        """`scopes_to_grant` intersects. A scope naming something the workflow
        never declared must not add it — otherwise a token becomes a way to
        grant yourself capability the code never asked for."""
        granted = self._authority(["jira", "github"]).grant

        assert set(granted.toolsets) == {"jira"}
        assert "github" not in granted.toolsets

    def test_admin_keeps_the_declaration(self) -> None:
        """Admin has no toolset-shaped counterpart to narrow against, so it maps
        to the declaration unchanged — still a subset, still never widening."""
        assert set(self._authority(["admin"]).grant.toolsets) == {"jira", "slack"}

    def test_the_narrowed_grant_is_strict(self) -> None:
        """An identity-derived grant that named some toolsets but said nothing
        about agents or sub-workflows must not leave those dimensions
        unchecked — the bug `GuardedBroker` had before its own strict flag."""
        assert self._authority(["jira"]).grant.strict is True


class TestItActuallyStopsACall:
    """Narrowing that no dispatch consults is narrowing in name only.

    Exercised at the broker, which is where a grant becomes a decision, and with
    a ``tool`` call because that is the kind a grant's ``toolsets`` dimension
    applies to. A plain local ``@step`` is deliberately *unclassified* — it
    belongs to no manifest, so no toolset claims it — and inventing a class for
    one would guess at the declaration a manifest exists to make.
    """

    def _narrowed(self, scopes: list[str]):
        from loom.core.models import ExecutionRecord

        rt = _runtime()
        record = ExecutionRecord(
            run_id="r1", workflow="cross", metadata={SCOPES_KEY: scopes}
        )
        try:
            return _authority_for(rt, rt.workflows["cross"], record)
        finally:
            rt._background.clear()

    async def _dispatch(self, authority, target: str):
        """The broker's verdict. It *returns* a refusal rather than raising —
        `DurableCall._resolve` is what turns a failed result into
        `EffectDenied`, so the decision and the exception live a layer apart."""
        from loom.runtime.effects import EffectCall

        async def perform() -> str:
            return "ran"

        return await GuardedBroker().dispatch(
            EffectCall(kind="tool", target=target, perform=perform), authority
        )

    async def test_a_toolset_outside_the_token_is_denied(self) -> None:
        verdict = await self._dispatch(self._narrowed(["jira"]), "slack.chat.post")

        assert verdict.ok is False
        assert "slack.chat.post" in (verdict.error or "")
        # `needs` is the actionable half — what the caller would have to hold.
        assert verdict.needs

    async def test_a_toolset_inside_the_token_still_runs(self) -> None:
        """The negative control — a narrowing that denies everything is not a
        narrowing, it is an outage."""
        verdict = await self._dispatch(self._narrowed(["jira"]), "jira.issues.search")

        assert verdict.ok is not False
        assert verdict.value == "ran"

    async def test_without_a_token_both_are_permitted(self) -> None:
        """The declaration alone allows both, so the token is doing the work."""
        from loom.core.models import ExecutionRecord

        rt = _runtime()
        record = ExecutionRecord(run_id="r1", workflow="cross", metadata={})
        try:
            authority = _authority_for(rt, rt.workflows["cross"], record)
        finally:
            rt._background.clear()

        assert (await self._dispatch(authority, "slack.chat.post")).value == "ran"
        assert (await self._dispatch(authority, "jira.issues.search")).value == "ran"
