"""WGIR extraction — three-pass graph extraction from workflow code.

Pass 1 — **Registry**: Collects decorated steps and workflow definitions.
         Provides exact identity, types, and configuration.

Pass 2 — **AST**: Visits the workflow body to find ``ctx.*`` calls,
         control flow (if/for/while), and variable assignments that
         create data-dependency edges.

Pass 3 — **Merge**: Combines both passes.  Registry wins on identity,
         AST wins on source ranges, unresolved items become opaque nodes.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from typing import Any

from loom.graph.wgir import (
    EdgeKind,
    NodeKind,
    SourceRange,
    WGIREdge,
    WGIRGraph,
    WGIRNode,
)
from loom.steps.definition import StepClass, StepDefinition

_TRUE_LABEL = "True"
_FALSE_LABEL = "False"
_LOOP_LABEL = "loop"
_DONE_LABEL = "done"

_LABEL_PREVIEW_LIMIT = 60
_ID_SAFE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ID_WORD = re.compile(r"[A-Za-z0-9]+")


def _preview(text: str, limit: int = _LABEL_PREVIEW_LIMIT) -> str:
    """Collapse whitespace and cut free text to `limit` chars on a word
    boundary, e.g. a ``ctx.agent("Write exactly one short, ...")`` prompt.

    The full text still reaches the graph -- as the node's `description`,
    which the canvas already renders in a hover tooltip and the inspector
    panel -- so the *label* only has to read as a short human title, not
    reproduce a paragraph a node card has no room for.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    head = collapsed[: limit - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return f"{head}…"


def _slug_id(text: str, *, limit: int = 48) -> str:
    """Deterministic, compact identifier fragment for `_alloc_id`.

    Most labels are already id-shaped -- a function name, a dotted tool id
    (``jira.create_issue``), or a keyword (``if``, ``return``) -- and pass
    through untouched, since the dot in a tool id is a signal a renderer
    relies on (see ``reactflow.py::_display_name``). Only free text needs
    slugifying: an agent's literal prompt, left alone, makes the node *id* a
    paragraph -- showing up unslugged in edge ids, DOM data attributes, and
    ``SET_LAYOUT`` position keys.
    """
    if text and _ID_SAFE.match(text) and len(text) <= limit:
        return text
    slug = "_".join(_ID_WORD.findall(text.lower()))[:limit].rstrip("_")
    return slug or "node"

# Map ctx method names to node kinds
_CTX_CALL_MAP: dict[str, NodeKind] = {
    "step": NodeKind.EFFECT,  # resolved from step's klass later
    "map": NodeKind.MAP,
    "gather": NodeKind.PARALLEL,
    "sleep": NodeKind.WAIT,
    "sleep_until": NodeKind.WAIT,
    "wait_for_event": NodeKind.WAIT,
    "wait_for_approval": NodeKind.HUMAN,
    "child": NodeKind.SUBFLOW,
    "agent": NodeKind.AGENT,
    "publish": NodeKind.EMIT,
    "emit": NodeKind.EMIT,  # deprecated alias for publish, still drawable
    "signal": NodeKind.EMIT,
    "checkpoint": NodeKind.ARTIFACT,
    "put_artifact": NodeKind.ARTIFACT,
    "get_artifact": NodeKind.ARTIFACT,
    "artifact_versions": NodeKind.ARTIFACT,
    "stage_artifact": NodeKind.ARTIFACT,
    "commit_staged": NodeKind.ARTIFACT,
    "discard_staged": NodeKind.ARTIFACT,
    "artifact_url": NodeKind.ARTIFACT,
}


# ---------------------------------------------------------------------------
# Pass 1: Registry
# ---------------------------------------------------------------------------


class RegistryCollector:
    """Collect WGIR nodes from decorated step/workflow definitions."""

    def collect_steps(
        self, steps: list[StepDefinition[Any, Any]]
    ) -> list[WGIRNode]:
        """Extract nodes from a list of step definitions."""
        nodes: list[WGIRNode] = []
        for defn in steps:
            kind = self._step_kind(defn)
            source = self._source_range(defn.fn)
            doc = (defn.description or "").strip()
            if not doc and defn.fn.__doc__:
                doc = defn.fn.__doc__.strip().split("\n")[0]

            # Extract type names from annotations
            hints = self._get_type_hints(defn.fn)

            nodes.append(WGIRNode(
                id=defn.name,
                kind=kind,
                label=defn.name,
                description=doc,
                input_type=hints.get("input"),
                output_type=hints.get("return"),
                source=source,
                step_class=defn.klass.value,
                retry_policy=(
                    {"max_attempts": defn.retry.max_attempts}
                    if defn.retry and defn.retry.max_attempts > 0
                    else None
                ),
                timeout=str(defn.timeout) if defn.timeout else None,
            ))
        return nodes

    @staticmethod
    def _step_kind(defn: StepDefinition[Any, Any]) -> NodeKind:
        match defn.klass:
            case StepClass.PURE:
                return NodeKind.PURE
            case StepClass.EFFECT:
                return NodeKind.EFFECT
            case StepClass.AGENT:
                return NodeKind.AGENT
            case _:
                return NodeKind.EFFECT

    @staticmethod
    def _source_range(fn: Any) -> SourceRange | None:
        try:
            source_file = inspect.getfile(fn)
            lines, start = inspect.getsourcelines(fn)
            return SourceRange(
                file=source_file,
                start_line=start,
                end_line=start + len(lines) - 1,
            )
        except (TypeError, OSError):
            return None

    @staticmethod
    def _get_type_hints(fn: Any) -> dict[str, str]:
        """Extract type hint names from function annotations."""
        hints: dict[str, str] = {}
        try:
            annotations = fn.__annotations__
        except AttributeError:
            return hints
        # Skip 'self', 'ctx' params — look at the rest
        params = list(annotations.items())
        for name, ann in params:
            if name == "return":
                hints["return"] = getattr(ann, "__name__", str(ann))
            elif name not in ("self", "ctx") and "input" not in hints:
                hints["input"] = getattr(ann, "__name__", str(ann))
        return hints


# ---------------------------------------------------------------------------
# Pass 2: AST
# ---------------------------------------------------------------------------


class ASTExtractor(ast.NodeVisitor):
    #: ``visit_*`` methods below keep their CamelCase suffixes because that is
    #: the name ``ast.NodeVisitor`` dispatches on — renaming one silently stops
    #: it being called. Suppressed per line, with the reason here rather than
    #: repeated: stating it inline pushed every signature past the line limit,
    #: and shortening those lines is how the suppressions were lost once already.

    """Extract control flow and ``ctx.*`` calls from workflow body source.

    Extraction is scoped to workflow bodies. Walking a whole module instead
    would pull in the ``if __name__ == "__main__"`` guard, helper functions, and
    the insides of every ``@step`` — producing a skeleton with nodes the flow
    does not have, which defeats the point of narrating a *verified* skeleton.
    """

    def __init__(self, source_file: str = "") -> None:
        self.nodes: list[WGIRNode] = []
        self.edges: list[WGIREdge] = []
        self._source_file = source_file
        self._counter: dict[str, int] = {}
        self._last_node_id: str | None = None
        # Track variable → defining node id for data edges
        self._var_defs: dict[str, str] = {}
        self._terminal_node_ids: set[str] = set()
        """Node ids that end the flow (currently: `return` statements).

        A branch tail in this set has no path to whatever follows it in
        source order -- treating it as a live predecessor in `visit_If`
        would wire an edge from an already-returned node to unrelated code,
        which is what made two independent `return` statements (one per
        branch, `if`/no-`else`) look like a linear chain instead of two
        branches of one switch.
        """
        self._depth = 0
        """Nesting depth inside a flow body; 0 means we are not in one."""
        self._pending_edge_label: str | None = None
        """Label for the *next* control edge `_add_node` emits, then cleared.

        Set before entering a branch (`if`/`else`) so the first node inside it
        records which branch it came from, without threading a parameter
        through every `visit_*` method on the path there.
        """
        self._pending_edge_condition: str | None = None
        """Same mechanism as `_pending_edge_label`, for the edge's `condition`
        (the actual source text of the test), consumed alongside the label."""
        self._pending_join_ids: list[str] = []
        """Extra predecessors for the *next* node `_add_node` emits.

        WGIR's AST pass tracks a single `_last_node_id` "current tip" of the
        chain being built. An `if`/`else` produces two tips — one per branch
        — and whatever statement follows the `if` is reachable from either,
        so it needs a control edge from both, not just the one carried in
        `_last_node_id`.
        """

    # -- scoping -------------------------------------------------------------

    def visit_Module(self, node: ast.Module) -> None:  # noqa: N802
        """Descend only into the module's workflow functions."""
        for fn in _flow_functions(node):
            # Each flow starts its own control chain rather than continuing the
            # previous one, so two workflows in a file do not appear connected.
            self._last_node_id = None
            self._depth = 1
            for stmt in fn.body:
                self.visit(stmt)
            self._depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_nested_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_nested_def(node)

    def _visit_nested_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """A function declared inside a flow body is a step, not flow structure.

        Its internals belong to that step's node, not to the graph, so we stop
        here. At module level this is unreachable — ``visit_Module`` selects the
        flows directly.
        """
        return

    def _alloc_id(self, prefix: str) -> str:
        count = self._counter.get(prefix, 0)
        self._counter[prefix] = count + 1
        return f"{prefix}_{count}" if count > 0 else prefix

    def _add_node(self, node: WGIRNode, ast_node: ast.AST) -> str:
        if hasattr(ast_node, "lineno"):
            node.source = SourceRange(
                file=self._source_file,
                start_line=ast_node.lineno,
                end_line=getattr(ast_node, "end_lineno", ast_node.lineno),
                start_col=getattr(ast_node, "col_offset", 0),
                end_col=getattr(ast_node, "end_col_offset", 0),
            )
        self.nodes.append(node)
        label = self._pending_edge_label or ""
        condition = self._pending_edge_condition
        self._pending_edge_label = None
        self._pending_edge_condition = None
        if self._last_node_id is not None:
            self.edges.append(WGIREdge(
                source=self._last_node_id,
                target=node.id,
                kind=EdgeKind.CONTROL,
                label=label,
                condition=condition,
            ))
        for extra_source in self._pending_join_ids:
            self.edges.append(WGIREdge(
                source=extra_source,
                target=node.id,
                kind=EdgeKind.CONTROL,
            ))
        self._pending_join_ids = []
        self._last_node_id = node.id
        return node.id

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        switch_id = self._alloc_id("switch")
        condition_text = _unparse(node.test)
        self._add_node(
            WGIRNode(
                id=switch_id,
                kind=NodeKind.SWITCH,
                label="if",
                description=f"Branches on `{condition_text}`." if condition_text else "",
            ),
            node,
        )

        # Both branches start fresh from the switch — not from wherever the
        # previous branch left off — so "True" and "False" each label an
        # edge that actually leaves the switch node.
        self._last_node_id = switch_id
        self._pending_edge_label = _TRUE_LABEL
        self._pending_edge_condition = condition_text
        for stmt in node.body:
            self.visit(stmt)
        true_tail = self._last_node_id
        true_live = None if true_tail in self._terminal_node_ids else true_tail

        false_tail = switch_id
        false_live: str | None = switch_id
        if node.orelse:
            self._last_node_id = switch_id
            self._pending_edge_label = _FALSE_LABEL
            self._pending_edge_condition = (
                f"not ({condition_text})" if condition_text else None
            )
            for stmt in node.orelse:
                self.visit(stmt)
            false_tail = self._last_node_id
            false_live = None if false_tail in self._terminal_node_ids else false_tail

        # WGIR has no explicit join node, so whatever follows the `if`
        # connects from every branch tail that can still reach it — a
        # branch that returned is excluded, since there is no path from it
        # to code after the `if`.
        live_tails = [tail for tail in (true_live, false_live) if tail is not None]

        if not live_tails:
            # Both branches end the flow — nothing after the `if` is
            # reachable, so nothing should be wired to it.
            self._last_node_id = None
            self._pending_join_ids = []
        elif len(live_tails) == 1:
            (only_live,) = live_tails
            self._last_node_id = only_live
            self._pending_join_ids = []
            # No `else`, and the true branch returned: the implicit false
            # path is now the *only* way to reach whatever follows, so it
            # earns the same branch label the explicit-`else` case gets —
            # instead of the unlabeled join edge this would otherwise be.
            if only_live is switch_id and true_live is None:
                self._pending_edge_label = _FALSE_LABEL
                self._pending_edge_condition = (
                    f"not ({condition_text})" if condition_text else None
                )
        else:
            self._last_node_id = true_live
            self._pending_join_ids = [tail for tail in live_tails if tail != true_live]

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        target, iterable = _unparse(node.target), _unparse(node.iter)
        description = (
            f"Repeats for each `{target}` in `{iterable}`." if target and iterable else ""
        )
        self._visit_loop(node, label="for", description=description)

    def visit_While(self, node: ast.While) -> None:  # noqa: N802
        condition_text = _unparse(node.test)
        description = f"Repeats while `{condition_text}`." if condition_text else ""
        self._visit_loop(node, label="while", description=description)

    def _visit_loop(
        self, node: ast.For | ast.While, *, label: str, description: str = ""
    ) -> None:
        loop_id = self._alloc_id("loop")
        self._add_node(
            WGIRNode(id=loop_id, kind=NodeKind.LOOP, label=label, description=description), node
        )

        self._last_node_id = loop_id
        for stmt in node.body:
            self.visit(stmt)
        body_tail = self._last_node_id
        if body_tail != loop_id:
            # Back-edge: the body's last statement loops back to re-evaluate
            # the loop condition, closing the cycle `layout.py` expects to
            # find and route around rather than count as forward progress.
            self.edges.append(WGIREdge(
                source=body_tail,
                target=loop_id,
                kind=EdgeKind.CONTROL,
                label=_LOOP_LABEL,
            ))

        # The exit test happens at the loop node, so whatever follows
        # connects from there — not from the end of the body.
        self._last_node_id = loop_id
        self._pending_edge_label = _DONE_LABEL
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
        nid = self._alloc_id("return")
        value_text = _unparse(node.value) if node.value is not None else None
        description = (
            f"Ends the workflow, returning `{value_text}`."
            if value_text
            else "Ends the workflow."
        )
        self._add_node(
            WGIRNode(id=nid, kind=NodeKind.RETURN, label="return", description=description),
            node,
        )
        self._terminal_node_ids.add(nid)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        # Track which variable is assigned by which node
        self.generic_visit(node)
        # After visiting the value side, the last_node_id is the producer
        if self._last_node_id and node.targets:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._var_defs[target.id] = self._last_node_id

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        call_name = self._resolve_call_name(node)
        if call_name and call_name.startswith("ctx."):
            method = call_name.split(".", 1)[1]
            if method in _CTX_CALL_MAP:
                kind = _CTX_CALL_MAP[method]
                label = self._extract_label(node, method)
                description = self._extract_description(node, method) or ""
                nid = self._alloc_id(_slug_id(label))
                self._add_node(
                    WGIRNode(id=nid, kind=kind, label=label, description=description),
                    node,
                )
        self.generic_visit(node)

    @staticmethod
    def _resolve_call_name(node: ast.Call) -> str | None:
        """Resolve ``ctx.step``, ``ctx.map``, etc."""
        func = node.func
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                return f"{func.value.id}.{func.attr}"
            if isinstance(func.value, ast.Attribute):
                # ctx.nested("x").step(...)
                pass
        return None

    @staticmethod
    def _extract_label(node: ast.Call, method: str) -> str:
        """Try to extract a meaningful label from a ctx call."""
        if method == "step":
            # `ctx.step(fn, name="jira.create_issue")` overrides the step's
            # journaled identity (`Context.step`'s `step_name = name or
            # definition.name`) -- a host that dispatches many operations
            # through one generic bridge function relies on exactly this to
            # tell them apart, so the graph's label must agree with what
            # actually got journaled rather than showing every call under
            # the bridge function's own name.
            for kw in node.keywords:
                if (
                    kw.arg == "name"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    return kw.value.value
        if method == "step" and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Name):
                return first_arg.id
            if isinstance(first_arg, ast.Attribute):
                return first_arg.attr
        if method in ("child", "agent") and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Name):
                return first_arg.id
            if isinstance(first_arg, ast.Constant) and isinstance(
                first_arg.value, str
            ):
                return _preview(first_arg.value)
        if method == "wait_for_event" and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(
                first_arg.value, str
            ):
                return f"wait:{first_arg.value}"
        if method in ("sleep", "sleep_until"):
            return "sleep"
        return method

    @staticmethod
    def _extract_description(node: ast.Call, method: str) -> str | None:
        """The literal prompt behind an inline ``ctx.agent``/``ctx.child``
        call, in full -- `_extract_label` only keeps a short preview of it."""
        if method in ("child", "agent") and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(
                first_arg.value, str
            ):
                return " ".join(first_arg.value.split())
        return None


def _unparse(node: ast.expr) -> str | None:
    """Best-effort source text for a condition, for the edge's `condition`
    field (a tooltip's worth of context beyond the "True"/"False" label).
    `None` rather than a raised error for anything `ast.unparse` itself
    cannot handle, since a missing condition string degrades to the label
    alone and is not worth failing extraction over."""
    try:
        return ast.unparse(node)
    except (ValueError, RecursionError):
        return None


_FLOW_DECORATORS = frozenset({"workflow", "flow"})

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _decorator_name(decorator: ast.expr) -> str:
    """Bare name of a decorator, whether or not it was called with arguments."""
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Call):
        return _decorator_name(decorator.func)
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    return ""


def _is_flow(node: FunctionNode) -> bool:
    return any(_decorator_name(d) in _FLOW_DECORATORS for d in node.decorator_list)


def _takes_ctx(node: FunctionNode) -> bool:
    args = node.args.args
    return bool(args) and args[0].arg == "ctx"


def _flow_functions(module: ast.Module) -> list[FunctionNode]:
    """The functions whose bodies make up the graph.

    Prefers ``@workflow``-decorated functions. Falls back to async functions
    whose first parameter is ``ctx`` so a bare body — a snippet, or a generated
    file inspected before its decorators are settled — still extracts.
    """
    decorated = [
        node
        for node in ast.walk(module)
        if isinstance(node, FunctionNode) and _is_flow(node)
    ]
    if decorated:
        return decorated
    return [
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and _takes_ctx(node)
    ]


def extract_from_source(
    source: str, *, flow_id: str = "", source_file: str = ""
) -> ASTExtractor:
    """Parse source code and extract WGIR nodes via the AST pass."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return ASTExtractor(source_file)

    extractor = ASTExtractor(source_file)
    extractor.visit(tree)
    return extractor


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def merge_passes(
    registry_nodes: list[WGIRNode],
    ast_nodes: list[WGIRNode],
    ast_edges: list[WGIREdge],
    *,
    flow_id: str,
    source_file: str = "",
) -> WGIRGraph:
    """Merge registry and AST passes into a final WGIR graph.

    - Registry wins on identity (step id, kind, types)
    - AST wins on source ranges
    - Unresolved AST nodes kept as-is (control flow structures)
    """
    nodes: dict[str, WGIRNode] = {}

    # Registry pass — authoritative on identity
    for n in registry_nodes:
        nodes[n.id] = n

    # AST pass — adds source ranges, control flow, new nodes
    for n in ast_nodes:
        if n.id in nodes:
            # Merge source range from AST
            if n.source is not None:
                nodes[n.id].source = n.source
        else:
            nodes[n.id] = n

    graph = WGIRGraph(
        flow_id=flow_id,
        nodes=list(nodes.values()),
        edges=ast_edges,
        source_file=source_file,
    )
    return graph.finalize()
