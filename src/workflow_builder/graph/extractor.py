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
import textwrap
from typing import Any

from workflow_builder.graph.wgir import (
    EdgeKind,
    NodeKind,
    SourceRange,
    WGIREdge,
    WGIRGraph,
    WGIRNode,
)
from workflow_builder.steps.definition import StepClass, StepDefinition

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
    "emit": NodeKind.EMIT,
    "signal": NodeKind.EMIT,
    "checkpoint": NodeKind.ARTIFACT,
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
    """Extract control flow and ``ctx.*`` calls from workflow body source."""

    def __init__(self, source_file: str = "") -> None:
        self.nodes: list[WGIRNode] = []
        self.edges: list[WGIREdge] = []
        self._source_file = source_file
        self._counter: dict[str, int] = {}
        self._last_node_id: str | None = None
        # Track variable → defining node id for data edges
        self._var_defs: dict[str, str] = {}

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
        # Add control edge from previous node
        if self._last_node_id is not None:
            self.edges.append(WGIREdge(
                source=self._last_node_id,
                target=node.id,
                kind=EdgeKind.CONTROL,
            ))
        self._last_node_id = node.id
        return node.id

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        nid = self._alloc_id("switch")
        self._add_node(
            WGIRNode(id=nid, kind=NodeKind.SWITCH, label="if"), node
        )
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        nid = self._alloc_id("loop")
        self._add_node(
            WGIRNode(id=nid, kind=NodeKind.LOOP, label="for"), node
        )
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:  # noqa: N802
        nid = self._alloc_id("loop")
        self._add_node(
            WGIRNode(id=nid, kind=NodeKind.LOOP, label="while"), node
        )
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
        nid = self._alloc_id("return")
        self._add_node(
            WGIRNode(id=nid, kind=NodeKind.RETURN, label="return"), node
        )

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
                nid = self._alloc_id(label)
                self._add_node(
                    WGIRNode(id=nid, kind=kind, label=label), node
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
                return first_arg.value
        if method == "wait_for_event" and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(
                first_arg.value, str
            ):
                return f"wait:{first_arg.value}"
        if method in ("sleep", "sleep_until"):
            return "sleep"
        return method


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
