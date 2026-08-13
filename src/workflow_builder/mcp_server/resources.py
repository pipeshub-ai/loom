"""What an MCP client can *read* from a Runtime.

Resources are the read-only half of MCP: addressable documents a client can pull
into context without taking an action. Tools do; resources describe.

The split matters for a workflow engine. ``loom://workflows`` is stable
reference material a client can cache, while ``run_workflow`` has side effects
and must not be something a client fetches speculatively.

As with :mod:`.tools`, nothing here imports ``mcp``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow_builder.facade import RuntimeFacade

__all__ = [
    "RESOURCES",
    "read_run",
    "read_run_journal",
    "read_workflow",
    "read_workflows",
]

#: URI templates this server serves, for documentation and registration.
RESOURCES: dict[str, str] = {
    "loom://workflows": "Every workflow this server can run",
    "loom://workflows/{name}": "One workflow's definition and input schema",
    "loom://runs/{run_id}": "One run's status, input, output, and error",
    "loom://runs/{run_id}/journal": "The durable operations a run recorded",
}


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


async def read_workflows(facade: RuntimeFacade) -> str:
    """``loom://workflows``"""
    return _json({"workflows": await facade.workflows()})


async def read_workflow(facade: RuntimeFacade, name: str) -> str:
    """``loom://workflows/{name}``"""
    match = next(
        (entry for entry in await facade.workflows() if entry["name"] == name), None
    )
    if match is None:
        return _json({"error": f"No workflow named '{name}'."})
    return _json(match)


async def read_run(facade: RuntimeFacade, run_id: str) -> str:
    """``loom://runs/{run_id}``"""
    run = await facade.get(run_id)
    return _json(run if run is not None else {"error": f"No run '{run_id}'."})


async def read_run_journal(facade: RuntimeFacade, run_id: str) -> str:
    """``loom://runs/{run_id}/journal``"""
    if await facade.get(run_id) is None:
        return _json({"error": f"No run '{run_id}'."})
    return _json({"run_id": run_id, "journal": await facade.journal(run_id)})
