"""Tools for the Workflow Coding Agent's ReAct loop.

These tools let the coding agent dynamically:
1. Discover available toolsets (``search_toolsets``)
2. Inspect toolset operations (``show_toolset``, ``get_tool_contract``)
3. Fetch tool docs with import paths and examples (``get_tool_docs``)
4. Validate generated code (``validate_code``)

All tools return ``str`` because tool results flow through the
conversation as text, consumed by the LLM on the next turn.
"""

from __future__ import annotations

import json
from typing import Any

from workflow_builder.agents.tools import Tool, tool

# ---------------------------------------------------------------------------
# Tool-docs registry — maps toolset_id to a callable returning docs
# ---------------------------------------------------------------------------

_TOOL_DOCS_REGISTRY: dict[str, Any] = {}


def register_tool_docs(toolset_id: str, docs: str | Any) -> None:
    """Register tool documentation for a toolset.

    Parameters
    ----------
    toolset_id:
        The toolset identifier (e.g. ``"jira"``).
    docs:
        Either a string of documentation, or a callable that
        returns a string (for lazy generation).
    """
    _TOOL_DOCS_REGISTRY[toolset_id] = docs


def _ensure_builtin_docs() -> None:
    """Lazily register built-in toolset docs if not already registered."""
    if "jira" not in _TOOL_DOCS_REGISTRY:
        try:
            from workflow_builder.toolsets.jira.tools import (
                JIRA_TOOL_DOCS,
            )
            _TOOL_DOCS_REGISTRY["jira"] = JIRA_TOOL_DOCS
        except ImportError:
            pass
    if "confluence" not in _TOOL_DOCS_REGISTRY:
        try:
            from workflow_builder.toolsets.confluence.tools import (
                CONFLUENCE_TOOL_DOCS,
            )
            _TOOL_DOCS_REGISTRY["confluence"] = CONFLUENCE_TOOL_DOCS
        except ImportError:
            pass
    if "langchain" not in _TOOL_DOCS_REGISTRY:
        try:
            from workflow_builder.integrations.langchain_tools_docs import (
                LANGCHAIN_TOOL_DOCS,
            )
            _TOOL_DOCS_REGISTRY["langchain"] = LANGCHAIN_TOOL_DOCS
        except ImportError:
            pass


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


@tool
async def search_toolsets(query: str) -> str:
    """Search registered toolsets by keyword.

    Args:
        query: Keywords to search for (e.g. "jira", "slack", "github").

    Returns JSON array of matching toolsets with id, summary, and groups.
    """
    from workflow_builder.toolsets.registry import get_catalog

    cards = get_catalog().search(query)
    return json.dumps(
        [c.model_dump() for c in cards],
        indent=2,
    )


@tool
async def show_toolset(
    toolset_id: str,
    group: str | None = None,
) -> str:
    """Show operations available in a toolset.

    Args:
        toolset_id: Toolset identifier (e.g. "jira").
        group: Optional group name to filter (e.g. "issues").

    Returns JSON table of operations with id, summary, and effect.
    """
    from workflow_builder.toolsets.registry import get_catalog

    try:
        table = get_catalog().show(toolset_id, group)
        return json.dumps(table.model_dump(), indent=2)
    except KeyError as exc:
        return json.dumps({"error": str(exc)})


@tool
async def get_tool_contract(op_path: str) -> str:
    """Get the typed contract for a specific operation.

    Args:
        op_path: Dotted path like "jira.issues.search".

    Returns JSON with input_schema, output_schema, effect, scopes, etc.
    """
    from workflow_builder.toolsets.registry import get_catalog

    try:
        contract = get_catalog().stub(op_path)
        return json.dumps(contract.model_dump(), indent=2)
    except (KeyError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


@tool
async def get_tool_docs(toolset_id: str) -> str:
    """Fetch full tool documentation for a toolset.

    Returns import paths, function signatures, parameter details,
    return types, and usage examples — everything the coding agent
    needs to generate correct workflow code.

    Args:
        toolset_id: Toolset identifier (e.g. "jira").
    """
    _ensure_builtin_docs()
    docs = _TOOL_DOCS_REGISTRY.get(toolset_id)
    if docs is None:
        return json.dumps({
            "error": f"No tool docs registered for '{toolset_id}'."
        })
    if callable(docs):
        docs = docs()
    return docs


@tool
async def validate_code(code: str) -> str:
    """Validate generated workflow code via AST analysis.

    Checks for syntax errors, missing @workflow/@step decorators,
    bare I/O in workflow bodies, nondeterministic calls, and
    missing workflow_builder imports.

    Args:
        code: Python source code to validate.

    Returns "Valid: no issues found." or JSON array of issues.
    """
    from workflow_builder.agents.validator import CodeValidator

    issues = CodeValidator().validate(code)
    if not issues:
        return "Valid: no issues found."
    return json.dumps(
        [
            {
                "category": i.category,
                "severity": i.severity,
                "message": i.message,
            }
            for i in issues
        ],
        indent=2,
    )


# ---------------------------------------------------------------------------
# Builder — returns the complete tool list for the coding agent
# ---------------------------------------------------------------------------


def build_coding_tools() -> list[Tool]:
    """Return the five ReAct tools for the workflow coding agent."""
    return [
        search_toolsets,
        show_toolset,
        get_tool_contract,
        get_tool_docs,
        validate_code,
    ]
