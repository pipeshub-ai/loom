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

from loom.agents.tools import Tool, tool

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


#: ``toolset id -> (module, symbol)`` for the docs LOOM ships.
#:
#: A table rather than a stack of ``if`` blocks, because the stack is what let
#: five toolsets build docs that nothing registered. ``get_tool_docs`` is step
#: 3 of the coding agent's own process — "DOCS: get_tool_docs for exact imports
#: and signatures" — so an unregistered toolset answered that step with
#: ``{"error": "No tool docs registered for 'slack'"}`` while its
#: ``SLACK_TOOL_DOCS`` sat fully rendered one import away. The agent then wrote
#: against whatever ``show_toolset`` gave it, which is schemas rather than
#: calls.
#:
#: ``tests/test_toolset_docs_honest.py`` asserts this table covers every module
#: that defines a ``*_TOOL_DOCS`` symbol, so the next one cannot go missing
#: quietly.
BUILTIN_TOOL_DOCS: tuple[tuple[str, str, str], ...] = (
    ("jira", "loom.toolsets.jira.tools", "JIRA_TOOL_DOCS"),
    ("confluence", "loom.toolsets.confluence.tools", "CONFLUENCE_TOOL_DOCS"),
    ("slack", "loom.toolsets.slack.tools", "SLACK_TOOL_DOCS"),
    ("zoom", "loom.toolsets.zoom.tools", "ZOOM_TOOL_DOCS"),
    ("gmail", "loom.toolsets.google.gmail.tools", "GMAIL_TOOL_DOCS"),
    ("google_calendar", "loom.toolsets.google.calendar.tools", "CALENDAR_TOOL_DOCS"),
    ("google_drive", "loom.toolsets.google.drive.tools", "DRIVE_TOOL_DOCS"),
    ("google_meet", "loom.toolsets.google.meet.tools", "MEET_TOOL_DOCS"),
    # Not a toolset: guidance for generating LangChain-flavoured code.
    ("langchain", "loom.integrations.langchain_tools_docs", "LANGCHAIN_TOOL_DOCS"),
)


def _ensure_builtin_docs() -> None:
    """Lazily register built-in toolset docs if not already registered.

    An ``ImportError`` is passed over rather than raised: a toolset whose
    optional dependency is absent has no docs to offer, and that is a narrower
    problem than a coding agent that cannot start.
    """
    import importlib

    for toolset_id, module_name, symbol in BUILTIN_TOOL_DOCS:
        if toolset_id in _TOOL_DOCS_REGISTRY:
            continue
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        docs = getattr(module, symbol, None)
        if docs is not None:
            _TOOL_DOCS_REGISTRY[toolset_id] = docs


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _registry(override: Any | None = None) -> Any:
    """The registry these tools browse — an explicit one, else the global."""
    if override is not None:
        return override
    from loom.toolsets.registry import get_catalog

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
async def search_operations(
    query: str, toolset_id: str | None = None, limit: int = 10
) -> str:
    """Search individual operations across every toolset, by what they do.

    Use this when you know the task but not which operation performs it —
    "transition an issue", "send an email", "upload a file". search_toolsets
    answers "is there a Jira integration"; this answers "which of its
    operations does the thing".

    Args:
        query: What the operation should do, in your own words.
        toolset_id: Narrow to one toolset, once you know which.
        limit: How many matches to return.

    Returns JSON array of {toolset_id, op_id, summary, effect, resolves,
    import_line}. ``resolves`` naming an entity kind means this operation looks
    a name up — call it before filtering on that name.
    """
    matches = _registry().search_operations(query, limit=limit, toolset_id=toolset_id)
    return json.dumps([m.model_dump() for m in matches], indent=2)


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
    rendered: str = docs
    return rendered


# ---------------------------------------------------------------------------
# Node discovery — the same three tiers as toolsets, one difference at tier 3
# ---------------------------------------------------------------------------


def _nodes(override: Any | None = None) -> Any:
    if override is not None:
        return override
    from loom.nodes.registry import get_node_catalog, load_builtin_nodes

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
        from loom.nodes.spec import NodeCategory

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
    from loom.nodes.errors import NodeNotFound

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
    from loom.nodes.errors import NodeNotFound

    try:
        contract: str = _nodes(registry).contract(node_id)
        return contract
    except NodeNotFound as exc:
        return json.dumps({"error": str(exc), "suggestions": exc.suggestions})


def _default_validator() -> Any:
    from loom.agents.validator import CodeValidator

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
    third-party imports, and missing loom imports.

    Args:
        code: Python source code to validate.

    Returns "Valid: no issues found." or JSON array of issues.
    """
    return _format_issues(_default_validator().validate(code))


#: How much of an observation to spend on the model. A DOM census is structured
#: and repetitive, so the first few thousand characters carry the shape and the
#: rest is more of the same.
_OBSERVATION_CHARS = 6000


@tool
async def observe_target(target: str, hint: str = "", probe: str = "") -> str:
    """Look at a real system before writing code against it. **Read-only.**

    Use this when the spec names something outside loom whose shape you would
    otherwise guess at: a URL, an API endpoint, a page with a form on it. What
    comes back is what is actually there — a JSON response's real field names, a
    page's real controls — rather than what the spec's author remembered.

    Guessing is not a small risk here. A field name that does not exist produces
    a workflow that runs, completes, and returns nulls; a selector that matches
    nothing produces one that reports zero results and no error. Neither fails
    in a way any check can see.

    Nothing is changed by looking. This cannot submit a form, click a button,
    send a request that writes, or create anything.

    Args:
        target: What to look at — usually an ``https://`` URL.
        hint: What you are trying to find out. Shapes what is reported.
        probe: Which probe to use. Leave empty for the default. Pass
            ``"browser"`` for a page that builds its content in the browser —
            fetching such a page over HTTP returns the empty shell, and a
            selector written against that shell finds nothing at runtime.

    Returns:
        JSON with a one-line ``summary`` and a structured ``detail``, or an
        ``error`` explaining why the look did not happen.
    """
    return json.dumps({"error": "no probes are configured in this environment"})


def make_observe_tool(registry: Any) -> Tool:
    """Bind :func:`observe_target` to a probe registry.

    Returned only when the registry holds something. A tool that is present and
    always answers "not configured" spends context on every turn to say nothing,
    and teaches the model to distrust the one capability that would have told it
    the truth — the same reason ``ask_user`` is omitted rather than stubbed.
    """
    from loom.agents.probes.base import ProbeError

    async def bound(target: str, hint: str = "", probe: str = "") -> str:
        available = ", ".join(sorted(p.id for p in registry.all())) or "none"
        chosen = registry.get(probe) if probe else registry.for_target(target)
        if chosen is None:
            missing = f"no probe named {probe!r}" if probe else (
                f"nothing here can look at {target!r}"
            )
            return json.dumps({"error": missing, "probes": available})
        probe_id = chosen.id
        try:
            observation = await chosen.observe(target, hint=hint)
        except ProbeError as exc:
            # Not a defect in the code being written, and phrased so the model
            # does not try to repair one.
            return json.dumps({"error": str(exc), "observed": False})
        except Exception as exc:  # a third-party probe may raise anything
            return json.dumps(
                {"error": f"{probe_id} failed: {exc}", "observed": False}
            )

        payload: dict[str, Any] = {
            "target": observation.target,
            "probe": observation.probe or probe_id,
            "summary": observation.summary,
            "detail": observation.detail[:_OBSERVATION_CHARS],
        }
        if len(observation.detail) > _OBSERVATION_CHARS:
            payload["detail_truncated"] = True
        if observation.evidence:
            payload["evidence"] = [
                {"filename": a.filename, "mime": a.mime, "size": a.size}
                for a in observation.evidence
            ]
        return json.dumps(payload, indent=2)

    return replace(observe_target, fn=bound)


# ---------------------------------------------------------------------------
# Builder — returns the complete tool list for the coding agent
# ---------------------------------------------------------------------------




@tool
async def call_read_operation(
    op_path: str, arguments: dict[str, Any] | str | None = None
) -> str:
    """Execute a **read-only** toolset operation while authoring, and return its result.

    For resolving what a spec refers to before writing code that depends on it:
    which account id belongs to a named person, which statuses this board actually uses,
    whether project "PA" exists. Guessing those produces code that runs
    perfectly and returns nothing.

    Only operations declared ``read`` can be called. A write or destructive
    operation is refused — authoring must never change the system it is writing
    code about, and a model exploring an API should not be able to send mail or
    delete an issue by way of research.

    Args:
        op_path: Fully qualified operation, e.g. ``"jira.users.resolve"``.
        arguments: The operation's arguments, e.g. ``{"name": "<the name the spec used>"}``.

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
    from loom.toolsets.manifest import EffectClass

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
    except TypeError as exc:
        # A signature mismatch, not a failure of the operation. The manifest
        # already knows what it accepts, so answer with that rather than with
        # the raw TypeError and a note about credentials — which is what this
        # returned, and it left the model to spend a turn on
        # `get_tool_contract` to learn a name that was one attribute away.
        #
        # Narrowed to TypeError deliberately: a TypeError raised *inside* the
        # operation is not a signature problem, and claiming otherwise would
        # send the model to fix the wrong thing. `accepts` is offered as
        # information, and the original error is kept verbatim beside it.
        return json.dumps(
            {
                "error": f"TypeError: {exc}",
                "accepts": sorted(
                    (spec.input_schema or {}).get("properties", {})
                ),
                "required": list((spec.input_schema or {}).get("required", [])),
                "note": (
                    "That looks like the wrong argument names for "
                    f"{op_path}. The accepted ones are above — call it again "
                    "with those. If the error came from inside the operation "
                    "rather than from its signature, treat it as a real "
                    "failure."
                ),
            }
        )
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

    payload: dict[str, Any] = {"result": _plain(result)}
    # Coverage hoisted *beside* the rows rather than left inside them, because
    # the truncation two lines down cuts the tail off — and on a `Results` the
    # tail is exactly where `complete` and `total` live. This is the entity
    # resolution loop the prompt mandates, so an agent picking "the" account out
    # of a silently-capped, silently-truncated list bakes the wrong id into
    # generated code that then runs unchanged forever.
    coverage = _coverage(result)
    if coverage is not None:
        payload["coverage"] = coverage

    rendered = json.dumps(payload, indent=2, default=str)
    if len(rendered) <= _READ_RESULT_LIMIT:
        return rendered

    # Too big. Drop whole *rows*, never cut the *rendering*.
    #
    # This used to return `{"result": rendered[:LIMIT]}` — the serialized
    # payload, as a string, under the same key that otherwise holds the data.
    # Three things went wrong at once, and all of them reached the generated
    # workflow:
    #
    #   * The shape changed with size. Under the limit `result` was a list;
    #     over it, a string. One key, two types, and the model meets the second
    #     far more often because big results are what get looked up.
    #   * It nested the envelope inside itself, so the model read
    #     `result.result` — and then wrote `issues.result` in the workflow,
    #     against a `Results`, which is a list and has no such attribute. That
    #     is a smoke failure caused entirely by the shape of a *lookup* reply.
    #   * The cut landed mid-token, so what was left was not parseable JSON.
    #     The only thing a reader could do with it was pattern-match the
    #     prefix — which is exactly the wrong lesson to teach.
    #
    # Dropping rows keeps the shape invariant, keeps every row that is shown
    # whole and readable, and says plainly how many were left out. The comment
    # on `coverage` above already knew truncation destroys structure; this
    # applies the same reasoning to the rows it was protecting.
    rows = payload["result"]
    if isinstance(rows, list) and rows:
        kept = list(rows)
        while kept and len(
            json.dumps({**payload, "result": kept}, indent=2, default=str)
        ) > _READ_RESULT_LIMIT:
            kept.pop()
        dropped = len(rows) - len(kept)
        return json.dumps(
            {
                "result": kept,
                "truncated": True,
                "note": (
                    f"{dropped} of {len(rows)} rows were left out to fit "
                    f"{_READ_RESULT_LIMIT} characters. Narrow the query rather "
                    "than assuming the rest look like these."
                ),
                **({"coverage": coverage} if coverage is not None else {}),
            },
            indent=2,
            default=str,
        )

    # Not a list of rows — a single large value. There is nothing to drop, so
    # say what it was rather than handing back half of it under a key that
    # promises the whole.
    return json.dumps(
        {
            "result": None,
            "truncated": True,
            "note": (
                f"The result rendered to {len(rendered)} characters, over the "
                f"{_READ_RESULT_LIMIT} limit, and is not a list of rows that "
                "could be shortened. Ask for less: narrow the query, or read a "
                "single record instead."
            ),
            **({"coverage": coverage} if coverage is not None else {}),
        },
        indent=2,
        default=str,
    )


#: How much of a read a resolution turn may put in front of the model.
_READ_RESULT_LIMIT = 8000


def _coverage(value: Any) -> dict[str, Any] | None:
    """Whether *value* is a page, said where a cut cannot remove it."""
    if not hasattr(value, "complete"):
        return None
    return {
        "complete": bool(value.complete),
        "total": getattr(value, "total", None),
        "returned": len(value) if hasattr(value, "__len__") else None,
    }


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
    probes: Any | None = None,
    budget: int = 5,
    gate: Any | None = None,
    asked: list[Any] | None = None,
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
        Optional :class:`~loom.agents.interaction.UserInteraction`.
        When provided, an ``ask_user`` tool is included; when ``None`` the
        tool is omitted entirely, so the model cannot call something that
        will fail.
    probes:
        Optional :class:`~loom.agents.probes.registry.ProbeRegistry`. When it
        holds anything, an ``observe_target`` tool is included, and the agent
        can look at the system it is writing code against instead of guessing
        at its shape. Empty or ``None`` omits the tool, and the agent behaves
        exactly as it did before probes existed.
    budget:
        Maximum ``ask_user`` calls per generation. Ignored when *gate* is
        passed (the gate carries its own budget).
    asked:
        List that accumulates every ``AskedQuestion`` the agent puts to a
        person. Passed in rather than returned so the caller owns it and can
        report it on ``CodingResult`` — the answers are inputs to a build, and
        a build whose inputs are not recorded cannot be reproduced.
    gate:
        Optional :class:`~loom.agents.interaction.AskUserGate`
        the caller can flip off during repair. Created internally when
        *interaction* is set and this is omitted.

    The returned tools keep the schemas of the module-level originals; only the
    bound behaviour changes, so the model sees an identical interface.
    """
    from loom.agents.interaction import AskUserGate, make_ask_user_tool

    tools: list[Tool]
    if registry is None and validator is None and node_registry is None:
        tools = [
            search_toolsets,
            search_operations,
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

        async def bound_search_operations(
            query: str, toolset_id: str | None = None, limit: int = 10
        ) -> str:
            matches = _registry(registry).search_operations(
                query, limit=limit, toolset_id=toolset_id
            )
            return json.dumps([m.model_dump() for m in matches], indent=2)

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
            replace(search_operations, fn=bound_search_operations),
            replace(show_toolset, fn=bound_show),
            replace(get_tool_contract, fn=bound_contract),
            get_tool_docs,
            replace(call_read_operation, fn=bound_call),
            replace(search_nodes, fn=bound_search_nodes),
            replace(show_node, fn=bound_show_node),
            replace(node_contract, fn=bound_node_contract),
            replace(validate_code, fn=bound_validate),
        ]

    if probes:
        # Truthiness, not `is not None`: an empty registry means the same thing
        # as no registry, and offering a tool that can never look at anything
        # spends context every turn to say "not configured".
        tools.append(make_observe_tool(probes))

    if interaction is not None:
        tools.append(
            make_ask_user_tool(
                interaction,
                gate=gate or AskUserGate(budget=budget),
                record=asked,
            )
        )
    return tools
