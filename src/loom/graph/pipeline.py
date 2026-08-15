"""The commit-time pipeline: source file in, graph and description out.

This is the entry point the rest of ``graph/`` was written for. Extraction runs
two passes — the registry pass knows what a step *is* (kind, types, retry
policy), the AST pass knows where it sits in the flow — and they are merged so
each is authoritative about the half it can actually see.

The outputs are meant to be committed next to the source:

* ``<flow>.graph.json``     structural diff of the workflow, reviewable
* ``<flow>.description.md`` the same thing in prose, reviewable by non-engineers

Generating them at commit time rather than on demand is deliberate: a
description is then a cached fact about a specific commit, and its diff is a
changelog.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loom.graph.emitter import emit_graph_json, graph_changed
from loom.graph.explainer import (
    Explainer,
    SkeletonExplainer,
    verify_completeness,
)
from loom.graph.extractor import (
    ASTExtractor,
    RegistryCollector,
    extract_from_source,
    merge_passes,
)
from loom.graph.wgir import WGIRGraph

if TYPE_CHECKING:
    from loom.runtime.workflow import WorkflowDefinition
    from loom.steps.definition import StepDefinition


@dataclass
class CheckReport:
    """What one ``loom check`` pass found and wrote."""

    flow_id: str
    node_count: int = 0
    edge_count: int = 0
    graph_changed: bool = False
    written: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """Narration gaps and extraction warnings. Non-empty means exit non-zero."""


def build_graph(source_file: Path, *, flow_id: str = "") -> WGIRGraph:
    """Extract a WGIR graph from a workflow source file.

    Importing the module is what makes the registry pass possible — decorators
    only populate their metadata when they run. When the import fails (a missing
    third-party dependency, most often) extraction falls back to the AST pass
    alone, which still produces a usable skeleton.
    """
    source = source_file.read_text(encoding="utf-8")
    steps, workflows = _load_definitions(source_file)

    registry_nodes = RegistryCollector().collect_steps(steps) if steps else []
    ast_pass: ASTExtractor = extract_from_source(
        source, source_file=str(source_file)
    )

    resolved_id = flow_id or (workflows[0].name if workflows else source_file.stem)
    return merge_passes(
        registry_nodes,
        ast_pass.nodes,
        ast_pass.edges,
        flow_id=resolved_id,
        source_file=str(source_file),
    )


def check_file(
    source_file: Path,
    *,
    write: bool = True,
    explainer: Explainer | None = None,
) -> CheckReport:
    """Extract, narrate, and (optionally) write the artifacts beside the source."""
    graph = build_graph(source_file)
    report = CheckReport(
        flow_id=graph.flow_id,
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
    )

    graph_path = source_file.with_suffix(".graph.json")
    description_path = source_file.with_suffix(".description.md")
    report.graph_changed = graph_changed(graph_path, graph)

    narration = asyncio.run((explainer or SkeletonExplainer()).narrate(graph))

    # The model narrates a skeleton it was handed, so it cannot invent a step.
    # It can still omit one, which is what this catches.
    missing = verify_completeness(narration, graph)
    if missing:
        report.problems.append(
            f"narration omitted {len(missing)} node(s): {', '.join(missing[:5])}"
        )

    # A grant that names nothing permits nothing, and reads as a restriction
    # either way. Caught here because `loom check` is the pass that runs before
    # anyone deploys, and a startup failure at that point is a fixed typo
    # rather than an agent reporting a missing tool in production.
    report.problems.extend(_grant_problems(source_file))

    if not write:
        report.unchanged.extend([graph_path, description_path])
        return report

    if report.graph_changed:
        emit_graph_json(graph, graph_path)
        report.written.append(graph_path)
    else:
        report.unchanged.append(graph_path)

    new_text = narration.full_text
    if _text_changed(description_path, new_text):
        description_path.write_text(new_text, encoding="utf-8")
        report.written.append(description_path)
    else:
        report.unchanged.append(description_path)

    return report


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _text_changed(path: Path, new_text: str) -> bool:
    if not path.exists():
        return True
    return path.read_text(encoding="utf-8") != new_text


def _load_definitions(
    source_file: Path,
) -> tuple[list[StepDefinition[Any, Any]], list[WorkflowDefinition[Any, Any, Any]]]:
    """Import the module and return the step and workflow objects it declares.

    Returns empty lists when the module cannot be imported. Extraction degrades
    to the AST pass rather than failing outright, because ``loom check`` should
    still say something useful about a file whose dependencies are not installed.
    """
    from loom.runtime.workflow import WorkflowDefinition
    from loom.steps.definition import StepDefinition

    module_name = f"_loom_check_{source_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, source_file)
    if spec is None or spec.loader is None:
        return [], []

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        return [], []
    finally:
        sys.modules.pop(module_name, None)

    steps = [v for v in vars(module).values() if isinstance(v, StepDefinition)]
    workflows = [v for v in vars(module).values() if isinstance(v, WorkflowDefinition)]
    return steps, workflows


def _grant_problems(source_file: Path) -> list[str]:
    """Grant entries in *source_file* that name no registered toolset.

    Reads the source rather than importing it: `loom check` must not execute
    the workflow it is checking, and the entries are literals in the decorator.
    """
    from loom.agents.stages import _declared_grants
    from loom.toolsets.registry import get_catalog

    try:
        code = source_file.read_text(encoding="utf-8")
    except OSError:
        return []

    catalog = get_catalog()
    found: list[str] = []
    for grant in _declared_grants(code):
        found.extend(str(issue) for issue in grant.validate_against(toolsets=catalog))
    return found
