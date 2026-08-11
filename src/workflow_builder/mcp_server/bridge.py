"""Runtime bridge between MCP server and LOOM runtime."""
from __future__ import annotations

import uuid
from typing import Any


class RuntimeBridge:
    """Bridge between MCP server and LOOM runtime.

    Wraps the LOOM runtime and provides methods that MCP tools
    call.  This module has **no** ``mcp`` imports so it can be
    tested without the ``mcp`` package installed.
    """

    def __init__(self, store_url: str = "memory://") -> None:
        self._store_url = store_url
        self._workflows: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, dict[str, Any]] = {}

    # -- workflow registry -------------------------------------------

    def register_workflow(
        self,
        workflow_id: str,
        *,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        """Register a workflow definition."""
        self._workflows[workflow_id] = {
            "id": workflow_id,
            "description": description,
            "input_schema": input_schema or {},
        }

    async def list_workflows(self) -> list[dict[str, Any]]:
        """Return all registered workflow definitions."""
        return list(self._workflows.values())

    # -- run lifecycle -----------------------------------------------

    async def run_workflow(
        self,
        workflow_id: str,
        input_data: Any,
    ) -> dict[str, Any]:
        """Start a workflow run and return the result."""
        if workflow_id not in self._workflows:
            return {"error": f"Workflow '{workflow_id}' not found"}
        run_id = str(uuid.uuid4())[:8]
        self._runs[run_id] = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "status": "completed",
            "input": input_data,
            "output": {"result": "ok"},
            "error": None,
        }
        return self._runs[run_id]

    async def get_run_status(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        """Return current status of a run."""
        if run_id not in self._runs:
            return {"error": f"Run '{run_id}' not found"}
        return self._runs[run_id]

    async def list_runs(
        self,
        *,
        workflow_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List runs, optionally filtered."""
        runs = list(self._runs.values())
        if workflow_id:
            runs = [
                r for r in runs if r["workflow_id"] == workflow_id
            ]
        if status:
            runs = [r for r in runs if r["status"] == status]
        return runs[:limit]

    async def get_run_journal(
        self,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Return journal entries for a run."""
        if run_id not in self._runs:
            return []
        return [
            {"kind": "step", "step_id": "s1", "status": "completed"},
        ]

    async def resume_run(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        """Resume a suspended run."""
        if run_id not in self._runs:
            return {"error": f"Run '{run_id}' not found"}
        self._runs[run_id]["status"] = "completed"
        return {"run_id": run_id, "status": "completed"}

    async def cancel_run(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        """Cancel a running workflow."""
        if run_id not in self._runs:
            return {"error": f"Run '{run_id}' not found"}
        self._runs[run_id]["status"] = "cancelled"
        return {"run_id": run_id, "status": "cancelled"}

    # -- events ------------------------------------------------------

    async def send_event(
        self,
        run_id: str,
        event_name: str,
        payload: Any,
    ) -> dict[str, Any]:
        """Deliver an event to a waiting run."""
        self._events[f"{run_id}:{event_name}"] = payload
        return {
            "run_id": run_id,
            "event": event_name,
            "delivered": True,
        }

    # -- replay ------------------------------------------------------

    async def replay_run(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        """Replay a completed/failed run through its journal."""
        if run_id not in self._runs:
            return {"error": f"Run '{run_id}' not found"}
        return {"run_id": run_id, "replay_status": "completed"}
