"""Workflow management tools for the ReAct agent.

These tools let an agent manage workflows: list, run, schedule,
cancel, get status. They operate on a ``Runtime`` instance and
form a ``Toolset`` via the standard registration pattern.

Usage::

    from workflow_builder.agents.workflow_tools import build_workflow_tools

    tools = build_workflow_tools(rt)
    rt.toolsets.register(Toolset.from_callables("workflow_manager", tools))
"""

from __future__ import annotations

import json
from typing import Any


def build_workflow_tools(runtime: Any) -> list[Any]:
    """Build workflow management tools bound to a Runtime.

    Each tool is a plain async callable that can be registered
    via ``Toolset.from_callables()``.
    """

    async def list_workflows() -> str:
        """List all registered workflows with their trigger info.

        Returns JSON array of workflow names and trigger summaries.
        """
        workflows = []
        for name, defn in runtime._workflows.items():
            triggers = []
            for t in defn.triggers:
                triggers.append(t.describe())
            workflows.append({
                "name": name,
                "description": defn.description or "",
                "triggers": triggers,
                "version": defn.version,
            })
        return json.dumps(workflows, indent=2, default=str)

    async def get_workflow_info(name: str) -> str:
        """Get detailed info about a workflow.

        Args:
            name: Workflow name.
        """
        defn = runtime._workflows.get(name)
        if defn is None:
            return json.dumps({"error": f"Workflow '{name}' not found"})
        return json.dumps({
            "name": defn.name,
            "description": defn.description or "",
            "version": defn.version,
            "triggers": [t.describe() for t in defn.triggers],
            "tags": list(defn.tags),
        }, indent=2, default=str)

    async def run_workflow(name: str, input_json: str = "null") -> str:
        """Run a workflow immediately.

        Args:
            name: Workflow name.
            input_json: JSON-encoded input data (default: null).
        """
        input_data = json.loads(input_json)
        try:
            result = await runtime.run(name, input_data)
            return json.dumps({
                "run_id": result.run_id,
                "status": result.status.value,
                "output": str(result.output)[:500] if result.output else None,
            }, indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def list_runs(
        name: str = "", limit: int = 10
    ) -> str:
        """List recent workflow runs.

        Args:
            name: Workflow name (empty for all workflows).
            limit: Max runs to return.
        """
        try:
            runs = await runtime.list_runs(
                workflow=name or None, limit=limit
            )
            return json.dumps([
                {
                    "run_id": r.run_id,
                    "workflow": r.workflow,
                    "status": r.status.value,
                    "created_at": str(r.created_at),
                }
                for r in runs
            ], indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def get_run_status(run_id: str) -> str:
        """Get the status and output of a specific run.

        Args:
            run_id: Run ID to check.
        """
        try:
            record = await runtime.get(run_id)
            if record is None:
                return json.dumps({"error": f"Run '{run_id}' not found"})
            return json.dumps({
                "run_id": record.run_id,
                "workflow": record.workflow,
                "status": record.status.value,
                "output": str(record.output)[:500] if record.output else None,
                "error": record.error.message if record.error else None,
                "created_at": str(record.created_at),
                "finished_at": str(record.finished_at),
            }, indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def cancel_run(run_id: str) -> str:
        """Cancel a running or suspended workflow run.

        Args:
            run_id: Run ID to cancel.
        """
        try:
            await runtime.cancel(run_id)
            return json.dumps({"cancelled": run_id})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def schedule_workflow(
        name: str,
        cron_expression: str,
        timezone: str = "UTC",
    ) -> str:
        """Schedule a workflow with a cron trigger.

        Args:
            name: Workflow name.
            cron_expression: Cron expression (e.g. "0 9 * * 1-5").
            timezone: Timezone for cron (default UTC).
        """
        from workflow_builder.runtime.dispatcher import TriggerDispatcher
        from workflow_builder.triggers.specs import Schedule

        defn = runtime._workflows.get(name)
        if defn is None:
            return json.dumps({"error": f"Workflow '{name}' not found"})

        try:
            sched = Schedule(cron_expression, timezone=timezone)
        except Exception as exc:
            return json.dumps({"error": f"Invalid cron: {exc}"})

        dispatcher = getattr(runtime, "_dispatcher", None)
        if dispatcher is None:
            dispatcher = TriggerDispatcher(runtime)
            runtime._dispatcher = dispatcher

        from datetime import UTC, datetime

        from workflow_builder.core.ids import new_id
        from workflow_builder.core.models import TriggerKind, TriggerRecord

        trigger = TriggerRecord(
            trigger_id=new_id("trg"),
            workflow=name,
            kind=TriggerKind.SCHEDULE,
            spec=sched.describe(),
            next_fire_at=sched.next_fire(datetime.now(UTC)),
            timezone=timezone,
        )
        await dispatcher._store.save_trigger(trigger)

        return json.dumps({
            "scheduled": name,
            "cron": cron_expression,
            "timezone": timezone,
            "trigger_id": trigger.trigger_id,
            "next_fire": str(trigger.next_fire_at),
        }, indent=2, default=str)

    return [
        list_workflows,
        get_workflow_info,
        run_workflow,
        list_runs,
        get_run_status,
        cancel_run,
        schedule_workflow,
    ]
