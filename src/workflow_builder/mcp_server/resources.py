"""MCP resource definitions for the LOOM server."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow_builder.mcp_server.bridge import RuntimeBridge

# -----------------------------------------------------------------
# Resource handler functions -- testable without the ``mcp`` package.
# -----------------------------------------------------------------


async def handle_workflow_list(bridge: RuntimeBridge) -> str:
    """Return all workflows as JSON."""
    workflows = await bridge.list_workflows()
    return json.dumps(workflows, indent=2)


async def handle_workflow_detail(
    bridge: RuntimeBridge,
    workflow_id: str,
) -> str:
    """Return details for a single workflow."""
    workflows = await bridge.list_workflows()
    wf = next(
        (w for w in workflows if w["id"] == workflow_id),
        None,
    )
    if not wf:
        return json.dumps(
            {"error": f"Workflow '{workflow_id}' not found"},
        )
    return json.dumps(wf, indent=2)


async def handle_run_detail(
    bridge: RuntimeBridge,
    run_id: str,
) -> str:
    """Return details for a single run."""
    status = await bridge.get_run_status(run_id)
    return json.dumps(status, indent=2)


async def handle_run_journal(
    bridge: RuntimeBridge,
    run_id: str,
) -> str:
    """Return journal entries for a run as JSON."""
    journal = await bridge.get_run_journal(run_id)
    return json.dumps(journal, indent=2)


def register_resources(server: Any, bridge: Any) -> None:
    """Register resources with an MCP Server instance.

    Deferred: the actual ``mcp`` registration hooks will be
    wired when the package is available.
    """
