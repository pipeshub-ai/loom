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
    flow_names,
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
    return _graph_of(source, source_file, steps, workflows, flow_id=flow_id)


def flows_in(source_file: Path) -> list[str]:
    """Every workflow *source_file* declares, in file order.

    The registry is authoritative -- a ``@workflow(name=…)`` is settled by the
    decorator running. The AST is the fallback for a module that will not
    import, which is the case the rest of this pipeline degrades to as well.
    """
    _, workflows = _load_definitions(source_file)
    if workflows:
        return [flow.name for flow in workflows]
    return flow_names(source_file.read_text(encoding="utf-8"))


def _graph_of(
    source: str,
    source_file: Path,
    steps: list[StepDefinition[Any, Any]],
    workflows: list[WorkflowDefinition[Any, Any, Any]],
    *,
    flow_id: str = "",
) -> WGIRGraph:
    """One workflow's graph, from definitions already loaded.

    Split out so ``check_module`` imports the module once and builds a graph per
    workflow, rather than paying for the import on every flow in the file.
    """
    registry_nodes = RegistryCollector().collect_steps(steps) if steps else []

    names = [flow.name for flow in workflows] or flow_names(source)
    resolved_id = flow_id or (names[0] if names else source_file.stem)

    # The AST pass is told which flow it is extracting. Without this a module
    # holding two workflows produced one graph, named after the first and
    # containing the second's steps too.
    ast_pass: ASTExtractor = extract_from_source(
        source, flow_id=resolved_id, source_file=str(source_file)
    )

    return merge_passes(
        registry_nodes,
        ast_pass.nodes,
        ast_pass.edges,
        flow_id=resolved_id,
        source_file=str(source_file),
    )


def check_module(
    source_file: Path,
    *,
    write: bool = True,
    explainer: Explainer | None = None,
) -> list[CheckReport]:
    """Check every workflow in *source_file* -- one report, and one pair of
    artifacts, per flow.

    A module holding two workflows holds two graphs. It used to produce one,
    named after the first and containing both bodies' nodes, which is exactly
    what the committed artifact exists to make impossible: the diff is only
    evidence that no step was added or hidden if each graph covers one flow.

    Artifacts stay at ``<stem>.graph.json`` while a module declares a single
    workflow -- the common case, and what is on disk today. A module declaring
    several qualifies each by flow, and the now-stale unqualified file is
    reported rather than deleted: ``loom check`` writes what a commit should
    contain and does not remove what a commit contains.
    """
    steps, workflows = _load_definitions(source_file)
    source = source_file.read_text(encoding="utf-8")
    names = [flow.name for flow in workflows] or flow_names(source)
    qualified = len(names) > 1

    reports = [
        check_file(
            source_file,
            write=write,
            explainer=explainer,
            flow_id=name,
            qualified=qualified,
            loaded=(steps, workflows),
        )
        for name in names or [""]
    ]

    stale = source_file.with_suffix(".graph.json")
    if qualified and stale.exists():
        reports[0].problems.append(
            f"{stale.name} is stale: this module declares {len(names)} workflows, "
            f"which are checked as {', '.join(f'{source_file.stem}.{n}' for n in names)}"
        )
    return reports


def check_file(
    source_file: Path,
    *,
    write: bool = True,
    explainer: Explainer | None = None,
    flow_id: str = "",
    qualified: bool = False,
    loaded: tuple[
        list[StepDefinition[Any, Any]], list[WorkflowDefinition[Any, Any, Any]]
    ]
    | None = None,
) -> CheckReport:
    """Extract, narrate, and (optionally) write the artifacts beside the source.

    One workflow: *flow_id* selects it, defaulting to the first in the file.
    Pass *qualified* to name the artifacts after the flow as well as the file,
    which is what :func:`check_module` does when there is more than one.
    """
    steps, workflows = loaded if loaded is not None else _load_definitions(source_file)
    graph = _graph_of(
        source_file.read_text(encoding="utf-8"),
        source_file,
        steps,
        workflows,
        flow_id=flow_id,
    )
    report = CheckReport(
        flow_id=graph.flow_id,
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
    )

    stem = f"{source_file.stem}.{graph.flow_id}" if qualified else source_file.stem
    graph_path = source_file.with_name(f"{stem}.graph.json")
    description_path = source_file.with_name(f"{stem}.description.md")
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
