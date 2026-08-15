"""n8n workflow importer — JSON -> LOOM code + fidelity report.

Parses an n8n export and generates a LOOM workflow file. Nodes that
map cleanly to LOOM constructs get full translation; the rest become
opaque ``@step`` stubs the user fills in.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9_]")
_MULTI_UNDERSCORE = re.compile(r"_+")


def _sanitize_name(name: str) -> str:
    """Convert *name* to a valid Python snake_case identifier."""
    result = name.replace(" ", "_").replace("-", "_").lower()
    result = _NON_ALNUM.sub("", result)
    result = _MULTI_UNDERSCORE.sub("_", result)
    return result.strip("_")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class N8nNode(BaseModel):
    """Representation of a single n8n node."""

    id: str
    type: str
    name: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    position: list[float] = Field(default_factory=list)


class N8nConnection(BaseModel):
    """Edge between two n8n nodes."""

    source: str
    target: str
    source_output: int = 0
    target_input: int = 0


class FidelityReport(BaseModel):
    """How faithfully the import captured the original workflow."""

    total_nodes: int = 0
    mapped_nodes: int = 0
    partial_nodes: int = 0
    unmapped_nodes: int = 0
    warnings: list[str] = Field(default_factory=list)

    @property
    def fidelity_score(self) -> float:
        """Fraction of nodes with a full mapping (0.0 -- 1.0)."""
        if self.total_nodes == 0:
            return 0.0
        return self.mapped_nodes / self.total_nodes


class ImportResult(BaseModel):
    """Generated LOOM code together with a fidelity report."""

    code: str
    report: FidelityReport


# ---------------------------------------------------------------------------
# Node-type mapping
# ---------------------------------------------------------------------------

_N8N_TYPE_MAP: dict[str, tuple[str, str]] = {
    "n8n-nodes-base.httpRequest": ("effect", "full"),
    "n8n-nodes-base.if": ("switch", "full"),
    "n8n-nodes-base.merge": ("gather", "full"),
    "n8n-nodes-base.wait": ("sleep", "full"),
    "n8n-nodes-base.code": ("step", "full"),
    "n8n-nodes-base.set": ("pure", "full"),
    "n8n-nodes-base.webhook": ("trigger_webhook", "full"),
    "n8n-nodes-base.scheduleTrigger": ("trigger_schedule", "full"),
    "n8n-nodes-base.slack": ("effect", "partial"),
    "n8n-nodes-base.gmail": ("effect", "partial"),
    "n8n-nodes-base.postgres": ("effect", "partial"),
    "n8n-nodes-base.airtable": ("effect", "partial"),
    "n8n-nodes-base.googleSheets": ("effect", "partial"),
    "n8n-nodes-base.openAi": ("agent", "partial"),
}


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class N8nImporter:
    """Convert an n8n JSON export into LOOM workflow code."""

    # -- public API ---------------------------------------------------------

    def import_workflow(
        self, n8n_json: dict[str, Any]
    ) -> ImportResult:
        """Parse n8n JSON -> generated LOOM code + fidelity report."""
        name = n8n_json.get("name", "imported_flow")
        nodes = self._parse_nodes(n8n_json.get("nodes", []))
        connections = self._parse_connections(
            n8n_json.get("connections", {})
        )
        code = self._generate_code(nodes, connections, name)
        report = self._build_report(nodes)
        return ImportResult(code=code, report=report)

    # -- parsing ------------------------------------------------------------

    @staticmethod
    def _parse_nodes(raw_nodes: list[dict]) -> list[N8nNode]:  # type: ignore[type-arg]
        """Create :class:`N8nNode` instances from raw dicts."""
        return [N8nNode(**raw) for raw in raw_nodes]

    @staticmethod
    def _parse_connections(
        raw_conns: dict,  # type: ignore[type-arg]
    ) -> list[N8nConnection]:
        """Flatten n8n's nested connection format into a flat list."""
        result: list[N8nConnection] = []
        for source_name, outputs in raw_conns.items():
            for output_key, targets_list in outputs.items():
                source_output = (
                    int(output_key)
                    if output_key.isdigit()
                    else 0
                )
                for idx, targets in enumerate(targets_list):
                    for target in targets:
                        result.append(
                            N8nConnection(
                                source=source_name,
                                target=target["node"],
                                source_output=source_output,
                                target_input=target.get(
                                    "index", idx
                                ),
                            )
                        )
        return result

    # -- classification -----------------------------------------------------

    @staticmethod
    def _classify_node(node: N8nNode) -> tuple[str, str]:
        """Return ``(loom_construct, fidelity)`` for *node*."""
        return _N8N_TYPE_MAP.get(node.type, ("step", "none"))

    # -- code generation ----------------------------------------------------

    def _generate_code(
        self,
        nodes: list[N8nNode],
        connections: list[N8nConnection],
        name: str,
    ) -> str:
        """Emit a valid LOOM Python module from parsed n8n data."""
        safe_name = _sanitize_name(name)
        lines: list[str] = []

        # -- imports --------------------------------------------------------
        lines.append(
            '"""Auto-generated LOOM workflow '
            f'— imported from n8n ({name})."""'
        )
        lines.append("")
        lines.append("from __future__ import annotations")
        lines.append("")
        lines.append(
            "from loom import Context, step, workflow"
        )
        lines.append("")
        lines.append("")

        # -- step definitions -----------------------------------------------
        for nd in nodes:
            construct, fidelity = self._classify_node(nd)
            fn_name = _sanitize_name(nd.name)
            if not fn_name:
                fn_name = f"node_{nd.id}"

            lines.append("@step")
            lines.append(
                f"async def {fn_name}"
                f"(ctx: Context) -> dict:"
            )
            lines.append(
                f'    """Mapped from n8n node '
                f'``{nd.type}`` ({construct})."""'
            )
            if fidelity == "none":
                lines.append(
                    f"    # TODO: implement {nd.type}"
                )
            elif fidelity == "partial":
                lines.append(
                    f"    # TODO: complete {nd.type} "
                    "integration"
                )
            lines.append("    return {}")
            lines.append("")
            lines.append("")

        # -- workflow function ----------------------------------------------
        lines.append("@workflow")
        lines.append(
            f"async def {safe_name}(ctx: Context) -> None:"
        )
        lines.append(
            f'    """Workflow imported from n8n: {name}."""'
        )

        if not nodes:
            lines.append("    pass")
        else:
            # Build dependency order from connections
            node_names = {
                nd.name: _sanitize_name(nd.name) or f"node_{nd.id}"
                for nd in nodes
            }
            called: set[str] = set()
            for conn in connections:
                target_fn = node_names.get(conn.target, "")
                if target_fn and target_fn not in called:
                    lines.append(
                        f"    await ctx.step({target_fn})"
                    )
                    called.add(target_fn)
            # Call any nodes not reached via connections
            for nd in nodes:
                fn = node_names[nd.name]
                if fn not in called:
                    lines.append(
                        f"    await ctx.step({fn})"
                    )
                    called.add(fn)

        lines.append("")
        return "\n".join(lines)

    # -- report -------------------------------------------------------------

    def _build_report(
        self, nodes: list[N8nNode]
    ) -> FidelityReport:
        """Tally how many nodes are fully/partially/un-mapped."""
        mapped = 0
        partial = 0
        unmapped = 0
        warnings: list[str] = []

        for nd in nodes:
            _, fidelity = self._classify_node(nd)
            if fidelity == "full":
                mapped += 1
            elif fidelity == "partial":
                partial += 1
                warnings.append(
                    f"Node '{nd.name}' ({nd.type}): partial "
                    "mapping — review generated stub"
                )
            else:
                unmapped += 1
                warnings.append(
                    f"Node '{nd.name}' ({nd.type}): no mapping "
                    "— manual implementation required"
                )

        return FidelityReport(
            total_nodes=len(nodes),
            mapped_nodes=mapped,
            partial_nodes=partial,
            unmapped_nodes=unmapped,
            warnings=warnings,
        )
