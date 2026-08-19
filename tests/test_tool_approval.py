"""``needs_approval`` on a tool is enforced.

It was a documented constructor parameter with a worked example in the `@tool`
docstring — `needs_approval=lambda args: args["amount_cents"] > 50_00` — and
`Tool.requires_approval` read it, failing closed on a raising predicate. Neither
was called by anything in src, tests, examples or docs.

A declaration that silently does nothing is worse than no declaration: somebody
writes that lambda and believes the refund is gated.

Enforced through the refusal contract that already exists — `EffectDenied` with
`needs="approval"`, the same shape `TaintBroker` returns — rather than as a
second mechanism beside the effect hooks.
"""

from __future__ import annotations

import pytest

from loom.agents.runner import _dispatch_tool
from loom.agents.tools import tool
from loom.runtime.effects import EffectDenied


@tool(needs_approval=lambda args: args["amount_cents"] > 50_00)
async def refund(order_id: str, amount_cents: int) -> str:
    """Refund part or all of an order.

    Args:
        order_id: The order to refund.
        amount_cents: How much to refund, in cents.
    """
    return f"refunded {amount_cents}"


@tool
async def lookup(order_id: str) -> str:
    """Look an order up.

    Args:
        order_id: The order to look up.
    """
    return "found"


class _Call:
    def __init__(self, **arguments: object) -> None:
        self.arguments = arguments


class TestNeedsApprovalIsEnforced:
    @pytest.mark.asyncio
    async def test_the_documented_example_refuses_above_the_threshold(self) -> None:
        with pytest.raises(EffectDenied) as denied:
            await _dispatch_tool(
                refund, _Call(order_id="o1", amount_cents=99_00), None, None
            )
        assert denied.value.needs == "approval"

    @pytest.mark.asyncio
    async def test_and_permits_below_it(self) -> None:
        """A per-call predicate, not a per-tool flag — the point of allowing a
        callable at all."""
        result = await _dispatch_tool(
            refund, _Call(order_id="o1", amount_cents=100), None, None
        )
        assert result == "refunded 100"

    @pytest.mark.asyncio
    async def test_a_tool_that_declares_nothing_is_unaffected(self) -> None:
        assert await _dispatch_tool(lookup, _Call(order_id="o1"), None, None) == "found"

    @pytest.mark.asyncio
    async def test_it_applies_without_a_broker(self) -> None:
        """Outside a workflow LOOM deliberately invents no policy — but this is
        not an invented one, it is the tool author's own, and it is exactly
        outside a durable run that nobody can approve."""
        with pytest.raises(EffectDenied):
            await _dispatch_tool(
                refund, _Call(order_id="o1", amount_cents=99_00), None, context=None
            )

    @pytest.mark.asyncio
    async def test_a_raising_predicate_asks_rather_than_assumes(self) -> None:
        """`Tool.requires_approval` fails closed: a crash in a risk check must
        not become an authorisation. Pinned here because that guarantee is only
        worth anything now that something calls it."""

        @tool(needs_approval=lambda args: args["missing_key"] > 0)
        async def brittle(order_id: str) -> str:
            """A tool whose risk check raises.

            Args:
                order_id: Anything.
            """
            return "done"

        with pytest.raises(EffectDenied):
            await _dispatch_tool(brittle, _Call(order_id="o1"), None, None)


class TestEveryPathToTheCallableIsGated:
    """The gate is only worth having if it does not depend on which backend a
    deployment happens to use.

    The first fix covered the built-in agent loop and missed all three backend
    adapters, which hand `tool.fn` straight to a third-party framework that
    knows nothing about LOOM. A gate you can step around by setting
    `Runtime(agent_backend=LangChainBackend())` is not a gate.
    """

    def test_no_backend_reaches_tool_fn_without_enforcing(self) -> None:
        """Structural, so a backend added later is caught too.

        By AST, not by counting occurrences. A first version of this compared
        substring counts and **passed** when a guard was deleted — the same
        shape as CERT-04, which claimed to require an explicit effect class and
        could never fail. A check that cannot fail has found nothing.
        """
        import ast
        from pathlib import Path

        def calls_fn(node: ast.AST) -> bool:
            return any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "fn"
                for child in ast.walk(node)
            )

        def guards(node: ast.AST) -> bool:
            return any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "enforce_approval"
                for child in ast.walk(node)
            )

        unguarded: list[str] = []
        for path in sorted(Path("src/loom/agents/backends").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "tool.fn" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                    continue
                # only the innermost wrapper that actually calls fn(...)
                inner = [
                    c
                    for c in ast.walk(node)
                    if isinstance(c, ast.AsyncFunctionDef | ast.FunctionDef)
                    and c is not node
                    and calls_fn(c)
                ]
                if inner or not calls_fn(node):
                    continue
                if not guards(node):
                    unguarded.append(f"{path.name}::{node.name}")

        assert not unguarded, (
            "these wrappers call fn(...) without Tool.enforce_approval, so "
            f"needs_approval is bypassed on that backend: {unguarded}"
        )

    def test_the_rule_lives_in_one_place(self) -> None:
        """Four call sites reproducing the same `raise` is how they drift."""
        from loom.agents.tools import Tool

        assert hasattr(Tool, "enforce_approval")

    @pytest.mark.asyncio
    async def test_a_langchain_converted_tool_still_refuses(self) -> None:
        pytest.importorskip("langchain_core")
        from loom.agents.backends.langchain import _loom_tool_to_langchain

        converted = _loom_tool_to_langchain(refund)
        with pytest.raises(EffectDenied):
            await converted.ainvoke({"order_id": "o1", "amount_cents": 99_00})
