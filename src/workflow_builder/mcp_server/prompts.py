"""MCP prompt templates for the LOOM server."""
from __future__ import annotations

import json
from typing import Any

# -----------------------------------------------------------------
# Prompt content generators -- testable without the ``mcp`` package.
# -----------------------------------------------------------------


def build_create_workflow_prompt(description: str) -> str:
    """Build a prompt that asks the model to create a workflow."""
    return (
        f"Create a LOOM workflow that does:\n\n"
        f"{description}\n\n"
        f"Use @workflow and @step decorators. "
        f"All API calls in @step functions. "
        f"Use ctx.step() for durable execution, "
        f"ctx.gather() for parallel, "
        f"ctx.wait_for_event() for external events."
    )


def build_debug_run_prompt(
    status: dict[str, Any],
    journal: list[dict[str, Any]],
) -> str:
    """Build a prompt to debug a failed workflow run."""
    return (
        f"Debug this workflow run:\n\n"
        f"Status: {status.get('status', 'unknown')}\n"
        f"Error: {status.get('error', 'none')}\n\n"
        f"Journal:\n{json.dumps(journal[-20:], indent=2)}\n\n"
        f"What failed and why? How to fix it?"
    )


def build_explain_workflow_prompt(
    workflow_id: str,
    details: dict[str, Any] | None,
) -> str:
    """Build a prompt to explain what a workflow does."""
    detail_str = (
        json.dumps(details, indent=2) if details else "Not found"
    )
    return (
        f"Explain workflow '{workflow_id}':\n\n"
        f"Details: {detail_str}\n\n"
        f"What does it do? What are the steps? "
        f"What triggers it? What could go wrong?"
    )


def build_optimize_prompt(workflow_id: str) -> str:
    """Build a prompt with optimisation suggestions."""
    return (
        f"Optimize workflow '{workflow_id}':\n\n"
        f"1. Can steps run in parallel with ctx.gather()?\n"
        f"2. Are retries configured for external API calls?\n"
        f"3. Any nondeterminism violations?\n"
        f"4. Missing timeout configurations?"
    )


def build_review_prompt(workflow_code: str) -> str:
    """Build a prompt to review workflow code."""
    return (
        f"Review this LOOM workflow:\n\n"
        f"```python\n{workflow_code}\n```\n\n"
        f"Check: correctness, security, durability, "
        f"error handling, performance, best practices."
    )


def register_prompts(server: Any, bridge: Any) -> None:
    """Register prompts with an MCP Server instance.

    Deferred: the actual ``mcp`` registration hooks will be
    wired when the package is available.
    """
