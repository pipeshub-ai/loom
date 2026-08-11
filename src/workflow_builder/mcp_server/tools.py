"""MCP tool definitions for the LOOM server."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow_builder.mcp_server.bridge import RuntimeBridge

# -----------------------------------------------------------------
# Tool handler functions -- testable without the ``mcp`` package.
# Each accepts a RuntimeBridge and returns a formatted string.
# -----------------------------------------------------------------


async def handle_list_workflows(bridge: RuntimeBridge) -> str:
    """List all registered workflows."""
    workflows = await bridge.list_workflows()
    if not workflows:
        return "No workflows registered."
    lines = [
        f"- **{w['id']}**: {w.get('description', 'No description')}"
        for w in workflows
    ]
    return "\n".join(lines)


async def handle_run_workflow(
    bridge: RuntimeBridge,
    workflow_id: str,
    input_data: str = "{}",
) -> str:
    """Start a workflow run."""
    try:
        parsed = json.loads(input_data)
    except json.JSONDecodeError:
        return "Error: input_data must be valid JSON."
    result = await bridge.run_workflow(workflow_id, parsed)
    if result.get("error"):
        return result["error"]
    return f"Run {result['run_id']}: {result['status']}"


async def handle_get_run_status(
    bridge: RuntimeBridge,
    run_id: str,
) -> str:
    """Get the status of a workflow run."""
    result = await bridge.get_run_status(run_id)
    if result.get("error"):
        return result["error"]
    parts = [
        f"Run: {result['run_id']}",
        f"Workflow: {result['workflow_id']}",
        f"Status: {result['status']}",
    ]
    if result.get("output"):
        parts.append(f"Output: {json.dumps(result['output'])}")
    if result.get("error"):
        parts.append(f"Error: {result['error']}")
    return "\n".join(parts)


async def handle_list_runs(
    bridge: RuntimeBridge,
    workflow_id: str = "",
    status: str = "",
    limit: int = 10,
) -> str:
    """List workflow runs with optional filters."""
    runs = await bridge.list_runs(
        workflow_id=workflow_id or None,
        status=status or None,
        limit=limit,
    )
    if not runs:
        return "No runs found."
    lines = [
        f"- {r['run_id']} | {r['workflow_id']} | {r['status']}"
        for r in runs
    ]
    return "\n".join(lines)


async def handle_resume_run(
    bridge: RuntimeBridge,
    run_id: str,
) -> str:
    """Resume a suspended workflow run."""
    result = await bridge.resume_run(run_id)
    if result.get("error"):
        return result["error"]
    return f"Run {run_id} resumed. Status: {result['status']}"


async def handle_cancel_run(
    bridge: RuntimeBridge,
    run_id: str,
) -> str:
    """Cancel a running workflow."""
    result = await bridge.cancel_run(run_id)
    if result.get("error"):
        return result["error"]
    return f"Run {run_id} cancelled."


async def handle_send_event(
    bridge: RuntimeBridge,
    run_id: str,
    event_name: str,
    payload: str = "{}",
) -> str:
    """Deliver an event to a waiting workflow run."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return "Error: payload must be valid JSON."
    await bridge.send_event(run_id, event_name, parsed)
    return f"Event '{event_name}' delivered to run {run_id}."


async def handle_get_run_logs(
    bridge: RuntimeBridge,
    run_id: str,
) -> str:
    """Retrieve journal entries for a workflow run."""
    journal = await bridge.get_run_journal(run_id)
    if not journal:
        return f"No journal entries for run {run_id}."
    lines = [
        f"- [{e.get('kind', '?')}] {e.get('step_id', '?')}"
        f" | status={e.get('status', '?')}"
        for e in journal
    ]
    return "\n".join(lines)


async def handle_replay_run(
    bridge: RuntimeBridge,
    run_id: str,
) -> str:
    """Replay a completed or failed workflow run."""
    result = await bridge.replay_run(run_id)
    if result.get("error"):
        return result["error"]
    return f"Replay of {run_id}: {result['replay_status']}"


def register_tools(server: Any, bridge: Any) -> None:
    """Register tools with an MCP Server instance.

    Requires the ``mcp`` package to be installed.  The actual
    registration uses ``server.tool()`` decorator at call time
    so no top-level ``mcp`` import is needed.
    """
    # Deferred: registration hooks will be wired when the mcp
    # package is available and the server is started.
