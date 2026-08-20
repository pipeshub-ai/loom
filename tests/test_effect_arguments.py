"""A node's payload must reach the broker, or `effect_by` decides nothing.

Found while wiring phase 13.2, and it had been shipped: `_effect_arguments`
returned `{}` for anything that was not a `dict`, and `ctx.node` passes the
validated **Pydantic model**. So for every node call the broker saw an empty
argument mapping, and `resolve_effect` fell straight through to the declared
class.

The consequence is the one this codebase keeps naming — a control that reads as
enforcing something and enforces nothing:

- `io.http_request(method="DELETE")` reached the broker classified **WRITE**,
  not DESTRUCTIVE. A deployment running the narrow taint dial that CLAUDE.md
  recommends — `block_writes=False, block_destructive=True` — therefore
  permitted exactly the calls it was configured to refuse.
- Every hook and guardrail deciding on *values* saw `{}` for a node.
- A remote broker's `describe()` carried no arguments for node calls.

`effect_by` was the only feature that could notice, and its one shipped user is
`io.http_request`, whose table was dead code from the day it was written.

These tests drive the predicate with the defect rather than trusting the fix,
the shape `tests/test_effect_gates_can_fail.py` uses.
"""

from __future__ import annotations

import contextlib

import pytest
from pydantic import BaseModel

from loom import Context, Runtime, workflow
from loom.runtime.context import _effect_arguments
from loom.runtime.effects import DirectBroker
from loom.stores.memory import MemoryStore
from loom.toolsets.effects import resolve_effect
from loom.toolsets.manifest import EffectClass


class Payload(BaseModel):
    method: str = "GET"
    url: str = ""
    nested: dict = {}


class TestArgumentsAreRecovered:
    def test_a_pydantic_payload_becomes_a_mapping(self) -> None:
        found = _effect_arguments(Payload(method="DELETE", url="http://x"))
        assert found["method"] == "DELETE"
        assert found["url"] == "http://x"

    def test_a_dict_payload_still_works(self) -> None:
        assert _effect_arguments({"method": "PUT"})["method"] == "PUT"

    def test_step_kwargs_still_win(self) -> None:
        """A step records ``{"args": …, "kwargs": …}``; only the named half is
        something a policy can read."""
        recorded = {"args": [1, 2], "kwargs": {"method": "POST"}}
        assert _effect_arguments(recorded) == {"method": "POST"}

    def test_presentation_never_fails_a_call(self) -> None:
        """The rule this function has always followed.

        A payload that will not dump yields an empty mapping — a policy that
        cannot read the values decides on the target instead, which is what it
        does for a positionally-invoked step anyway.
        """
        class Hostile(BaseModel):
            def model_dump(self, *args, **kwargs):  # type: ignore[override]
                raise RuntimeError("nope")

        assert _effect_arguments(Hostile()) == {}

    def test_an_unrecognised_object_is_empty_rather_than_an_error(self) -> None:
        assert _effect_arguments(object()) == {}


class TestTheDefectItself:
    """Driven by the bug, so the fix cannot silently regress."""

    def test_an_empty_mapping_makes_effect_by_dead(self) -> None:
        """What the broker used to be handed, and what it decided from it.

        This is the whole bug in one assertion: with no arguments,
        ``method="DELETE"`` is indistinguishable from ``method="GET"`` and both
        keep the declared class.
        """
        table = {"method": {"GET": EffectClass.READ,
                            "DELETE": EffectClass.DESTRUCTIVE}}
        assert resolve_effect(EffectClass.WRITE, table, {}) is EffectClass.WRITE

    def test_with_arguments_the_table_decides(self) -> None:
        table = {"method": {"GET": EffectClass.READ,
                            "DELETE": EffectClass.DESTRUCTIVE}}
        assert resolve_effect(
            EffectClass.WRITE, table, {"method": "GET"}) is EffectClass.READ
        assert resolve_effect(
            EffectClass.WRITE, table, {"method": "DELETE"}
        ) is EffectClass.DESTRUCTIVE

    def test_an_unlisted_value_keeps_the_cautious_class(self) -> None:
        """An unrecognised method must not fall through to a read."""
        table = {"method": {"GET": EffectClass.READ}}
        assert resolve_effect(
            EffectClass.WRITE, table, {"method": "PATCH"}) is EffectClass.WRITE


class TestEndToEnd:
    @pytest.mark.parametrize(
        ("method", "expected"),
        [("GET", "read"), ("POST", "write"), ("DELETE", "destructive")],
    )
    async def test_a_node_call_reaches_the_broker_classified(
        self, method: str, expected: str
    ) -> None:
        """``io.http_request`` is the one shipped user of ``effect_by``.

        Driven through a real dispatch rather than the predicate, because the
        defect was in the wiring between them and a unit test of either half
        would have passed throughout.
        """
        seen: list[tuple[str, str, dict]] = []

        class Spy:
            def __init__(self, inner):
                self._inner = inner

            async def dispatch(self, call, authority):
                if call.target == "io.http_request":
                    seen.append((call.target, call.effect.value,
                                 dict(call.arguments)))
                return await self._inner.dispatch(call, authority)

        @workflow(name="one_call")
        async def one_call(ctx: Context, _input) -> str:
            # Port 9 is discard: this never connects, and the dispatch has
            # already happened by the time it fails.
            with contextlib.suppress(Exception):
                await ctx.node("io.http_request",
                               {"url": "http://127.0.0.1:9/x", "method": method})
            return "done"

        rt = Runtime(store=MemoryStore(), broker=Spy(DirectBroker()))
        await rt.run(one_call, None)

        assert seen, "the node call never reached the broker"
        _, effect, arguments = seen[0]
        assert effect == expected
        assert arguments.get("method") == method, (
            "the broker must see the payload's values, not an empty mapping")
