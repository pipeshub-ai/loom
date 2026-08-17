"""Prompt templates an MCP client can invoke.

Pure string builders: they take already-fetched data and return the text to send
to a model. Registration lives in :mod:`.server`.
"""
from __future__ import annotations

import json
from typing import Any

# -----------------------------------------------------------------
# Prompt content generators -- testable without the ``mcp`` package.
# -----------------------------------------------------------------


def build_create_workflow_prompt(description: str, *, authoring_enabled: bool = False) -> str:
    """Build a prompt that asks the model to create a workflow.

    ``authoring_enabled=False`` (the default) returns the original bare
    template, unchanged — a client with no authoring tools registered would
    otherwise be told to call tools that do not exist. Pass ``True`` when the
    server also registered ``get_tool_contract``/``validate_workflow_code``/
    etc., for the full discover -> generate -> verify -> save ladder.
    """
    if not authoring_enabled:
        return (
            f"Create a LOOM workflow that does:\n\n"
            f"{description}\n\n"
            f"Use @workflow and @step decorators. "
            f"All API calls in @step functions. "
            f"Use ctx.step() for durable execution, "
            f"ctx.gather() for parallel, "
            f"ctx.wait_for_event() for external events."
        )
    return (
        f"Create a LOOM workflow that does:\n\n"
        f"{description}\n\n"
        "## Discover, generate, verify\n\n"
        "1. DISCOVER: search_toolsets(keyword) to find integrations the spec "
        "needs; show_toolset(id) to see their operations.\n"
        "2. INSPECT: get_tool_contract(\"toolset.op\") for the exact schema of "
        "each operation you will call; get_tool_docs(toolset_id) for import "
        "lines and worked examples where available.\n"
        "3. RESOLVE: call_read_operation(\"toolset.op\", args_json) to turn a "
        "name in the spec (a project, a user) into the id the code needs — "
        "never guess or invent one.\n"
        "4. GENERATE: write the complete Python file. Use @workflow for the "
        "entry point and @step for every I/O operation; ctx.step() for "
        "durable calls, ctx.gather() for parallel ones, "
        "ctx.wait_for_event()/ctx.wait_for_approval() for suspension. All API "
        "calls belong inside a @step, never directly in the workflow body. "
        "Never call datetime.now(), uuid4(), or random.* directly — use the "
        "ctx.* equivalents.\n"
        "5. VALIDATE: validate_workflow_code(code, spec=<the description "
        "above, verbatim>) — fix every error-severity issue before "
        "continuing. Pass spec every time: two of its stages judge the code "
        "against the request rather than against the language, and they "
        "report themselves skipped without it. `coverage` catches a fetch "
        "capped at 100 answering a spec that said 'all'; `resolution` catches "
        "a query built by fuzzy-matching a word from the spec instead of "
        "resolving it to an id. Read the `stages` array in the reply — a "
        "stage marked skipped has found nothing, which is not the same as "
        "passing.\n"
        "6. SMOKE: smoke_test_workflow(code) — runs it once in a sandbox with "
        "every toolset faked. Fix a real failure; a result with "
        "environmental=true is the sandbox having no credentials, not a bug "
        "in the code, and does not need a fix. The `replay` key reports a "
        "separate question: two more runs, compared, so nondeterminism the "
        "single run could not show up gets caught here rather than after the "
        "first crash in production.\n"
        "7. SAVE: save_workflow(code, \"flows/<name>.py\").\n"
        "8. RUN: run_workflow(name, input_json) to try it for real.\n\n"
        "## Pagination\n\n"
        "When get_tool_contract says pagination=true, the tool returns a "
        "Results list — a list subclass with .complete (False when more "
        "existed than max_results allowed), .total (how many matched), and "
        ".cursor (where the next page starts). Generated code must:\n"
        "- Set max_results high enough for the use case, or loop with the "
        "cursor to fetch everything.\n"
        "- Check .complete after the call; if false, either fetch more or "
        "report the coverage (e.g. 'showing 50 of 120').\n"
        "- Never silently drop results — a workflow that processes one page "
        "and says nothing is the same failure as fetching the wrong data.\n\n"
        "## Code or judgement\n\n"
        "For each piece of the spec: can a rule be written that is right for "
        "every input the spec allows? Yes -> @step. No, or unsure -> "
        "ctx.agent(\"...\"). An invented constant — a keyword list, a "
        "hand-written threshold nobody supplied — is the sign a rule should "
        "have been an agent call instead."
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
