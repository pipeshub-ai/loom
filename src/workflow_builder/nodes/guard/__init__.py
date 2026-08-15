"""Guardrail nodes: one abstraction, three attachment points.

The four verdicts are the existing ones — :class:`GuardrailAction` is imported
from ``agents.guardrails``, not redefined. What is new is where a guard may
attach:

1. **Standalone** — ``await ctx.guard("guard.pii", payload)``, drawn as its own
   node so a reviewer reading the graph can see the check.
2. **Around a node** — ``NodeSpec.guards`` or ``ctx.node(..., guards=[...])``.
3. **Around agent tool calls** — where they already ran, unchanged.

    from workflow_builder.nodes.guard import GuardVerdict

    clean = await ctx.guard("guard.pii", draft)          # REPLACE redacts
    await ctx.node("io.http_request", req, guards=["guard.policy"])
"""

from __future__ import annotations

from workflow_builder.agents.guardrails import Guardrail, GuardrailAction, guardrail
from workflow_builder.nodes.guard.nodes import (
    BudgetGuard,
    BudgetIn,
    ContentGuard,
    ContentIn,
    PiiGuard,
    PiiIn,
    PolicyGuard,
    PolicyIn,
    SchemaGuard,
    SchemaIn,
)
from workflow_builder.nodes.guard.runner import (
    GuardInput,
    GuardVerdict,
    apply_guards,
    as_verdict,
    enforce,
)

__all__ = [
    "BudgetGuard",
    "BudgetIn",
    "ContentGuard",
    "ContentIn",
    "GuardInput",
    "GuardVerdict",
    "Guardrail",
    "GuardrailAction",
    "PiiGuard",
    "PiiIn",
    "PolicyGuard",
    "PolicyIn",
    "SchemaGuard",
    "SchemaIn",
    "apply_guards",
    "as_verdict",
    "enforce",
    "guardrail",
]
