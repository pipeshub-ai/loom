"""Graph emission — write graph.json and detect changes.

``graph.json`` is committed alongside the workflow source file.
Change detection uses the extraction hash to avoid noisy commits.
"""

from __future__ import annotations

from pathlib import Path

from workflow_builder.graph.wgir import WGIRGraph


def emit_graph_json(graph: WGIRGraph, output_path: Path) -> None:
    """Write the graph as JSON to *output_path*."""
    content = graph.model_dump_json(indent=2)
    output_path.write_text(content, encoding="utf-8")


def load_graph_json(path: Path) -> WGIRGraph:
    """Load a WGIR graph from a JSON file."""
    return WGIRGraph.model_validate_json(path.read_text(encoding="utf-8"))


def graph_changed(old_path: Path, new_graph: WGIRGraph) -> bool:
    """Check if the graph changed from the previously committed version.

    Compares extraction hashes — the hash covers structural content
    (node IDs, kinds, edges) but not timestamps or descriptions.
    """
    if not old_path.exists():
        return True
    try:
        old = load_graph_json(old_path)
    except Exception:
        return True
    return old.extraction_hash != new_graph.extraction_hash
