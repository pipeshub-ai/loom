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

import importlib
import json
from dataclasses import replace
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


def _registry(override: Any | None = None) -> Any:
    """The registry these tools browse — an explicit one, else the global."""
    if override is not None:
        return override
    from workflow_builder.toolsets.registry import get_catalog

    return get_catalog()


@tool
async def search_toolsets(query: str) -> str:
    """Search registered toolsets by keyword.

    Args:
        query: Keywords to search for (e.g. "jira", "slack", "github").

    Returns JSON array of matching toolsets with id, summary, and groups.
    """
    cards = _registry().search(query)
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
    try:
        table = _registry().show(toolset_id, group)
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
    try:
        contract = _registry().stub(op_path)
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


# ---------------------------------------------------------------------------
# Node discovery — the same three tiers as toolsets, one difference at tier 3
# ---------------------------------------------------------------------------


def _nodes(override: Any | None = None) -> Any:
    if override is not None:
        return override
    from workflow_builder.nodes.registry import get_node_catalog, load_builtin_nodes

    load_builtin_nodes()
    return get_node_catalog()


@tool
async def search_nodes(
    query: str = "", category: str | None = None, limit: int = 10
) -> str:
    """Find reusable nodes — typed units called with ``await ctx.node(id, Input(...))``.

    Prefer a catalogued node over hand-written code when one covers the work: it
    is typed, versioned, and already tested.

    Args:
        query: Keywords, e.g. "approval", "classify", "http". May be empty.
        category: One of human, guard, control, transform, io, agent, custom.
            An empty query with a category lists that category.
        limit: Most results to return.

    Returns JSON array of {id, category, summary, suspends, requires}.
    """
    return await _search_nodes(query, category, limit, registry=None)


async def _search_nodes(
    query: str, category: str | None, limit: int, *, registry: Any | None
) -> str:
    try:
        cards = _nodes(registry).search(query or "", category=category, limit=limit)
    except ValueError:
        from workflow_builder.nodes.spec import NodeCategory

        return json.dumps(
            {
                "error": f"no category {category!r}",
                "categories": [c.value for c in NodeCategory],
            }
        )
    if not cards:
        catalog = _nodes(registry)
        return json.dumps(
            {
                "error": f"nothing matched {query!r}"
                + (f" in category {category!r}" if category else ""),
                "categories": {
                    c.value: n for c, n in sorted(catalog.categories().items())
                },
            }
        )
    return json.dumps([c.model_dump(mode="json") for c in cards], indent=2)


@tool
async def show_node(node_id: str) -> str:
    """Show one node in full: schemas, examples, effect, and what it requires.

    For the code to write, call ``node_contract`` instead — it returns the
    invocation rather than a description of it.

    Args:
        node_id: A node id, e.g. "human.approval".
    """
    return await _show_node(node_id, registry=None)


async def _show_node(node_id: str, *, registry: Any | None) -> str:
    from workflow_builder.nodes.errors import NodeNotFound

    try:
        return json.dumps(_nodes(registry).show(node_id).model_dump(mode="json"), indent=2)
    except NodeNotFound as exc:
        return json.dumps({"error": str(exc), "suggestions": exc.suggestions})


@tool
async def node_contract(node_id: str) -> str:
    """Get the exact code to call a node: import line, call, and result type.

    Returns runnable Python, not a schema — copy it and fill in the values. The
    header says whether the node parks the run and what the Runtime must have
    configured.

    Args:
        node_id: A node id, e.g. "human.approval".
    """
    return await _node_contract(node_id, registry=None)


async def _node_contract(node_id: str, *, registry: Any | None) -> str:
    from workflow_builder.nodes.errors import NodeNotFound

    try:
        return _nodes(registry).contract(node_id)
    except NodeNotFound as exc:
        return json.dumps({"error": str(exc), "suggestions": exc.suggestions})


def _default_validator() -> Any:
    from workflow_builder.agents.validator import CodeValidator

    return CodeValidator()


def _format_issues(issues: list[Any]) -> str:
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


@tool
async def validate_code(code: str) -> str:
    """Validate generated workflow code via AST analysis.

    Checks for syntax errors, missing @workflow/@step decorators,
    bare I/O in workflow bodies, nondeterministic calls, disallowed
    third-party imports, and missing workflow_builder imports.

    Args:
        code: Python source code to validate.

    Returns "Valid: no issues found." or JSON array of issues.
    """
    return _format_issues(_default_validator().validate(code))


# ---------------------------------------------------------------------------
# Builder — returns the complete tool list for the coding agent
# ---------------------------------------------------------------------------




@tool
async def call_read_operation(
    op_path: str, arguments: dict[str, Any] | str | None = None
) -> str:
    """Execute a **read-only** toolset operation while authoring, and return its result.

    For resolving what a spec refers to before writing code that depends on it:
    which account id is "Vishwjeet", which statuses this board actually uses,
    whether project "PA" exists. Guessing those produces code that runs
    perfectly and returns nothing.

    Only operations declared ``read`` can be called. A write or destructive
    operation is refused — authoring must never change the system it is writing
    code about, and a model exploring an API should not be able to send mail or
    delete an issue by way of research.

    Args:
        op_path: Fully qualified operation, e.g. ``"jira.users.resolve"``.
        arguments: The operation's arguments, e.g. ``{"name": "Vishwjeet"}``.

    Returns:
        JSON with the result, or an error explaining what to do instead.
    """
    return await _call_read_operation(op_path, arguments, registry=None)


#: How many times one lookup may be repeated before the tool stops pretending
#: a fresh answer is coming. Two is enough to recover from a malformed first
#: attempt; beyond that the call is not being retried, it is being re-asked.
_REPEAT_LIMIT = 2


async def _call_read_operation(
    op_path: str,
    arguments: dict[str, Any] | str | None,
    *,
    registry: Any | None,
    seen: dict[str, int] | None = None,
) -> str:
    from workflow_builder.toolsets.manifest import EffectClass

    toolset_id, _, operation_id = op_path.partition(".")
    if not operation_id:
        return json.dumps(
            {"error": f"expected '<toolset>.<operation>', got {op_path!r}"}
        )

    catalog = _registry(registry)
    manifest = catalog.get(toolset_id)
    if manifest is None:
        return json.dumps(
            {"error": f"no toolset {toolset_id!r}", "available": catalog.list_toolsets()}
        )

    spec = manifest.find_operation(operation_id)
    if spec is None:
        return json.dumps(
            {
                "error": f"no operation {operation_id!r} on {toolset_id!r}",
                "available": [op.id for op in manifest.all_operations()],
            }
        )

    if spec.effect is not EffectClass.READ:
        return json.dumps(
            {
                "error": (
                    f"{op_path} is {spec.effect.value}, and authoring may only "
                    "read. Write the call into the generated workflow instead of "
                    "performing it here."
                )
            }
        )

    if not spec.function or not manifest.tools_module:
        return json.dumps(
            {"error": f"{op_path} declares no callable function to invoke"}
        )

    # Accept the object a model naturally emits, and the JSON string it
    # sometimes sends instead. Rejecting either shape turns one mistake into a
    # retry loop that burns the whole turn budget without executing anything.
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"arguments is not valid JSON: {exc}"})
    if not isinstance(arguments, dict):
        return json.dumps(
            {"error": f"arguments must be an object, got {type(arguments).__name__}"}
        )

    # A repeated identical lookup cannot produce a different answer, and a model
    # that keeps asking is not converging — it is stuck on an entity the data
    # does not resolve. Signature taken after normalising, because the encoding
    # varies ({}, null, "{}", omitted) while the call does not.
    if seen is not None:
        signature = f"{op_path}:{json.dumps(arguments, sort_keys=True, default=str)}"
        seen[signature] = seen.get(signature, 0) + 1
        if seen[signature] > _REPEAT_LIMIT:
            return json.dumps(
                {
                    "error": (
                        f"You have already called {op_path} with these arguments "
                        f"{seen[signature] - 1} times and the answer has not "
                        "changed. It will not. If the entity is still ambiguous, "
                        "stop looking and resolve it at run time: emit a "
                        "ctx.agent() step that picks between the candidates you "
                        "have, then return the workflow."
                    )
                }
            )

    try:
        module = importlib.import_module(manifest.tools_module)
        fn = getattr(module, spec.function)
        # A @step is callable directly; this is deliberately outside a Runtime,
        # since authoring is not a durable execution.
        result = await fn(**arguments)
    except Exception as exc:
        # Credentials missing at authoring time is normal and not a code
        # problem: say so plainly rather than looking like a broken operation.
        return json.dumps(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "note": (
                    "If this is a credentials or network failure, the operation "
                    "is fine — you simply cannot resolve it here. Generate code "
                    "that resolves it at runtime instead."
                ),
            }
        )

    return json.dumps({"result": _plain(result)}, indent=2, default=str)[:8000]


def _plain(value: Any) -> Any:
    """Render a result as JSON-friendly data, models included."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def build_coding_tools(
    *,
    registry: Any | None = None,
    validator: Any | None = None,
    node_registry: Any | None = None,
    interaction: Any | None = None,
    budget: int = 5,
    gate: Any | None = None,
) -> list[Tool]:
    """Return the ReAct tools for the workflow coding agent.

    Parameters
    ----------
    registry:
        The :class:`ToolsetRegistry` the discovery tools browse. Pass the
        Runtime's own (``rt.toolsets``) so the agent discovers exactly the
        toolsets the generated workflow will be able to call — the default
        global registry can be a superset.
    validator:
        A configured :class:`CodeValidator`, typically carrying an
        ``allowed_packages`` allowlist. Defaults to an unrestricted one.
    node_registry:
        The :class:`NodeRegistry` the node tools browse. Pass the Runtime's own
        (``rt.nodes``) so the agent discovers exactly the nodes the generated
        workflow will be able to call.
    interaction:
        Optional :class:`~workflow_builder.agents.interaction.UserInteraction`.
        When provided, an ``ask_user`` tool is included; when ``None`` the
        tool is omitted entirely, so the model cannot call something that
        will fail.
    budget:
        Maximum ``ask_user`` calls per generation. Ignored when *gate* is
        passed (the gate carries its own budget).
    gate:
        Optional :class:`~workflow_builder.agents.interaction.AskUserGate`
        the caller can flip off during repair. Created internally when
        *interaction* is set and this is omitted.

    The returned tools keep the schemas of the module-level originals; only the
    bound behaviour changes, so the model sees an identical interface.
    """
    from workflow_builder.agents.interaction import AskUserGate, make_ask_user_tool

    tools: list[Tool]
    if registry is None and validator is None and node_registry is None:
        tools = [
            search_toolsets,
            show_toolset,
            get_tool_contract,
            get_tool_docs,
            call_read_operation,
            search_nodes,
            show_node,
            node_contract,
            validate_code,
        ]
    else:
        # Per-instance ledger: the count spans one generation and resets with the
        # next, so a repeated lookup is detected within a run and not across runs.
        seen_lookups: dict[str, int] = {}

        async def bound_call(
            op_path: str, arguments: dict[str, Any] | str | None = None
        ) -> str:
            return await _call_read_operation(
                op_path, arguments, registry=registry, seen=seen_lookups
            )

        async def bound_search(query: str) -> str:
            cards = _registry(registry).search(query)
            return json.dumps([c.model_dump() for c in cards], indent=2)

        async def bound_show(toolset_id: str, group: str | None = None) -> str:
            try:
                table = _registry(registry).show(toolset_id, group)
                return json.dumps(table.model_dump(), indent=2)
            except KeyError as exc:
                return json.dumps({"error": str(exc)})

        async def bound_contract(op_path: str) -> str:
            try:
                return json.dumps(_registry(registry).stub(op_path).model_dump(), indent=2)
            except (KeyError, ValueError) as exc:
                return json.dumps({"error": str(exc)})

        async def bound_validate(code: str) -> str:
            return _format_issues((validator or _default_validator()).validate(code))

        async def bound_search_nodes(
            query: str = "", category: str | None = None, limit: int = 10
        ) -> str:
            return await _search_nodes(query, category, limit, registry=node_registry)

        async def bound_show_node(node_id: str) -> str:
            return await _show_node(node_id, registry=node_registry)

        async def bound_node_contract(node_id: str) -> str:
            return await _node_contract(node_id, registry=node_registry)

        tools = [
            replace(search_toolsets, fn=bound_search),
            replace(show_toolset, fn=bound_show),
            replace(get_tool_contract, fn=bound_contract),
            get_tool_docs,
            replace(call_read_operation, fn=bound_call),
            replace(search_nodes, fn=bound_search_nodes),
            replace(show_node, fn=bound_show_node),
            replace(node_contract, fn=bound_node_contract),
            replace(validate_code, fn=bound_validate),
        ]

    if interaction is not None:
        tools.append(
            make_ask_user_tool(
                interaction,
                gate=gate or AskUserGate(budget=budget),
            )
        )
    return tools
