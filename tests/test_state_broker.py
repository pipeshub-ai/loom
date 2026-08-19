"""`ctx.state` reaches a broker, and `resources` finally means something.

Every other durable operation reaches a broker through `DurableCall`, which
journals it. State must *not* be journaled — it is shared across runs of one
workflow and mutable, so a recorded value replayed later would be a lie about
the present — and that is precisely why it reached no broker at all: the only
path to one went through the journal.

The consequence was that state was invisible to every broker. `max_calls` could
not see a loop writing a key a million times; `TaintBroker` could not see a run
reading data another run had put there; a host broker asked to log or refuse
effects was never told. This separates the two questions the journal had bundled:
the broker decides whether a call may happen, the journal records that it did.

It also gives `resources` a meaning. It was a declared grant dimension that
nothing ever read — a declaration that looked enforced and was not.
"""

from __future__ import annotations

import pytest

from loom import Context, Runtime, workflow
from loom.runtime.effects import EffectCall, EffectDenied, GuardedBroker
from loom.security.authority import Authority
from loom.security.grants import GrantSet
from loom.stores.memory import MemoryStore


@workflow(name="stateful")
async def stateful(ctx: Context, _: None = None) -> str:
    await ctx.state.set("cursor", 7)
    got = await ctx.state.get("cursor")
    return f"cursor={got}"


class Recording(GuardedBroker):
    """A broker that remembers what it was asked, to prove it was asked."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.seen: list[tuple[str, str, str]] = []

    async def dispatch(self, call: EffectCall, authority: Authority):
        self.seen.append((call.kind, call.target, call.effect.value))
        return await super().dispatch(call, authority)


class TestStateIsVisibleToABroker:
    async def test_reads_and_writes_are_dispatched(self) -> None:
        broker = Recording()
        rt = Runtime(store=MemoryStore(), broker=broker, authority=Authority())
        rt.register(stateful)
        try:
            result = await rt.run(stateful)
        finally:
            await rt.shutdown()

        assert result.output == "cursor=7"
        kinds = [(k, e) for k, _t, e in broker.seen if k == "state"]
        assert ("state", "write") in kinds
        assert ("state", "read") in kinds

    async def test_the_target_names_the_workflow_and_key(self) -> None:
        """A broker deciding on state needs to know *which* state."""
        broker = Recording()
        rt = Runtime(store=MemoryStore(), broker=broker, authority=Authority())
        rt.register(stateful)
        try:
            await rt.run(stateful)
        finally:
            await rt.shutdown()

        targets = [t for k, t, _e in broker.seen if k == "state"]
        assert any(t == "state:stateful:cursor" for t in targets), targets

    async def test_delete_is_destructive_not_merely_a_write(self) -> None:
        """A broker that treats removal as an ordinary write cannot express
        "may change data, may not remove it" — which is the distinction
        `block_destructive` exists for."""
        broker = Recording()

        @workflow(name="clears")
        async def clears(ctx: Context, _: None = None) -> str:
            await ctx.state.set("k", 1)
            await ctx.state.delete("k")
            return "done"

        rt = Runtime(store=MemoryStore(), broker=broker, authority=Authority())
        rt.register(clears)
        try:
            await rt.run(clears)
        finally:
            await rt.shutdown()

        assert ("state", "state:clears:k", "destructive") in broker.seen

    async def test_state_is_still_not_journaled(self) -> None:
        """The property that made this awkward, and must survive it.

        State is shared across runs and mutable; a journaled value served back
        on replay would describe a past that is no longer true.
        """
        rt = Runtime(store=MemoryStore(), broker=GuardedBroker(), authority=Authority())
        rt.register(stateful)
        try:
            result = await rt.run(stateful)
            entries = await rt.store.load_journal(result.run_id)
        finally:
            await rt.shutdown()

        assert not [e for e in entries if "state" in e.name]

    async def test_no_broker_configured_still_works(self) -> None:
        """A bare Runtime has no broker, and state must not require one."""
        rt = Runtime(store=MemoryStore())
        rt.register(stateful)
        try:
            assert (await rt.run(stateful)).output == "cursor=7"
        finally:
            await rt.shutdown()


class TestResourcesNowDecide:
    """`resources` was declared and read by nothing."""

    async def _run(self, grant: GrantSet):
        rt = Runtime(
            store=MemoryStore(),
            broker=GuardedBroker(),
            authority=Authority(grant=grant),
        )
        rt.register(stateful)
        try:
            return await rt.run(stateful)
        finally:
            await rt.shutdown()

    async def test_a_strict_grant_naming_no_resources_refuses_state(self) -> None:
        result = await self._run(GrantSet(toolsets=["jira"], strict=True))

        assert result.error is not None
        assert result.error.type == "EffectDenied"

    async def test_naming_state_permits_it(self) -> None:
        result = await self._run(GrantSet(resources=["state"], strict=True))

        assert result.error is None, result.error
        assert result.output == "cursor=7"

    async def test_a_read_only_grant_refuses_the_write(self) -> None:
        """The qualifier has to bite, or `state:read` is decoration."""
        result = await self._run(GrantSet(resources=["state:read"], strict=True))

        assert result.error is not None
        assert result.error.type == "EffectDenied"

    async def test_a_write_grant_implies_the_read(self) -> None:
        """A caller allowed to write state but not read it back is a grant
        nobody would write on purpose."""
        result = await self._run(GrantSet(resources=["state:write"], strict=True))

        assert result.error is None, result.error

    async def test_a_lenient_grant_that_never_mentions_resources_permits(self) -> None:
        """Deny-by-default applies *within a dimension the caller spoke to*.
        A non-strict grant listing only toolsets says nothing about state, and
        must not start refusing it — that would break every existing Runtime
        the moment a broker was configured."""
        result = await self._run(GrantSet(toolsets=["jira"]))

        assert result.error is None, result.error

    async def test_an_empty_grant_permits_everything(self) -> None:
        result = await self._run(GrantSet())

        assert result.error is None, result.error


class TestTheResourceMatcher:
    @pytest.mark.parametrize(
        ("held", "effect", "allowed"),
        [
            (["state"], "read", True),
            (["state"], "destructive", True),
            (["state:read"], "read", True),
            (["state:read"], "write", False),
            (["state:write"], "read", True),
            (["state:write"], "write", True),
            (["state:write"], "destructive", False),
            (["state:destructive"], "write", True),
            (["pg:read"], "read", False),
            ([], "read", False),
        ],
    )
    def test_effect_implication(self, held, effect, allowed) -> None:
        from loom.runtime.effects import _allows_resource

        assert _allows_resource(held, "state", effect) is allowed

    async def test_the_denial_names_what_would_allow_it(self) -> None:
        """`needs` is the actionable half — a refusal nobody can act on is a
        refusal that gets worked around rather than fixed."""
        broker = GuardedBroker()

        async def perform() -> None:
            return None

        from loom.toolsets.manifest import EffectClass

        verdict = await broker.dispatch(
            EffectCall(
                kind="state", target="state:w:k", effect=EffectClass.WRITE,
                perform=perform,
            ),
            Authority(grant=GrantSet(resources=["pg:read"], strict=True)),
        )

        assert verdict.ok is False
        assert verdict.needs == "state:write"


class TestItStillRaisesTheRightType:
    async def test_a_refusal_surfaces_as_effect_denied(self) -> None:
        """A workflow branches on the type — `except EffectDenied` is how it
        tells "policy refused this" from "this broke"."""
        rt = Runtime(
            store=MemoryStore(),
            broker=GuardedBroker(),
            authority=Authority(grant=GrantSet(resources=["pg"], strict=True)),
        )

        @workflow(name="catches")
        async def catches(ctx: Context, _: None = None) -> str:
            try:
                await ctx.state.set("k", 1)
            except EffectDenied:
                return "refused"
            return "allowed"

        rt.register(catches)
        try:
            assert (await rt.run(catches)).output == "refused"
        finally:
            await rt.shutdown()
