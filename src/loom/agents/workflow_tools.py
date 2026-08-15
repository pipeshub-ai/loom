"""Workflow management tools for the ReAct agent.

These tools let an agent manage workflows: list, run, schedule, cancel, get
status. They take a :class:`~loom.facade.RuntimeFacade` — the same
port the CLI, the MCP server, and the HTTP API hold — rather than a
``Runtime``.

That distinction is the point. A ``Runtime`` is the interpreter: it can execute
a body in this process, and these tools reached into ``runtime._workflows``
and set ``runtime._dispatcher`` to do their work. A facade is data in and data
out, so an assistant that manages workflows can be handed one without being
handed the ability to run arbitrary code in the host process.

Usage::

    from loom.agents.workflow_tools import build_workflow_tools
    from loom.facade import LocalFacade

    tools = build_workflow_tools(LocalFacade(rt))
    rt.toolsets.register(Toolset.from_callables("workflow_manager", tools))
"""

from __future__ import annotations

import json
from typing import Any


def build_workflow_tools(facade: Any) -> list[Any]:
    """Build workflow management tools bound to a :class:`RuntimeFacade`.

    Each tool is a plain async callable that can be registered via
    ``Toolset.from_callables()``. Accepts a ``Runtime`` too, wrapping it, so
    existing callers keep working — but a facade is what to pass.
    """
    from loom.facade import LocalFacade, RuntimeFacade

    if not isinstance(facade, RuntimeFacade):
        facade = LocalFacade(facade)

    async def list_workflows() -> str:
        """List all registered workflows with their trigger info.

        Returns JSON array of workflow names and trigger summaries.
        """
        listed = await facade.workflows(published=False)
        return json.dumps(
            [
                {
                    "name": entry["name"],
                    "description": entry.get("description") or "",
                    "triggers": entry.get("triggers") or [],
                    "version": entry.get("version"),
                }
                for entry in listed
            ],
            indent=2,
            default=str,
        )

    async def get_workflow_info(name: str) -> str:
        """Get detailed info about a workflow.

        Args:
            name: Workflow name.
        """
        found = next(
            (e for e in await facade.workflows(published=False) if e["name"] == name),
            None,
        )
        if found is None:
            return json.dumps({"error": f"Workflow '{name}' not found"})
        return json.dumps({
            "name": found["name"],
            "description": found.get("description") or "",
            "version": found.get("version"),
            "triggers": found.get("triggers") or [],
            "input_schema": found.get("input_schema"),
        }, indent=2, default=str)

    async def run_workflow(name: str, input_json: str = "null") -> str:
        """Run a workflow immediately.

        Args:
            name: Workflow name.
            input_json: JSON-encoded input data (default: null).
        """
        input_data = json.loads(input_json)
        try:
            result = await facade.start(name, input_data)
            return json.dumps({
                "run_id": result["run_id"],
                "status": result["status"],
                "output": str(result["output"])[:500] if result.get("output") else None,
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
            runs = await facade.list_runs(workflow=name or None, limit=limit)
            return json.dumps([
                {
                    "run_id": r["run_id"],
                    "workflow": r["workflow"],
                    "status": r["status"],
                    "created_at": r.get("created_at"),
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
            record = await facade.get(run_id)
            if record is None:
                return json.dumps({"error": f"Run '{run_id}' not found"})
            return json.dumps({
                "run_id": record["run_id"],
                "workflow": record["workflow"],
                "status": record["status"],
                "output": str(record["output"])[:500] if record.get("output") else None,
                "error": record.get("error"),
                "created_at": record.get("created_at"),
                "finished_at": record.get("finished_at"),
            }, indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def cancel_run(run_id: str) -> str:
        """Cancel a running or suspended workflow run.

        Args:
            run_id: Run ID to cancel.
        """
        try:
            await facade.cancel(run_id)
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
        try:
            made = await facade.schedule(name, cron_expression, timezone=timezone)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

        return json.dumps({
            "scheduled": made["workflow"],
            "cron": cron_expression,
            "timezone": timezone,
            "trigger_id": made["trigger_id"],
            "next_fire": made.get("next_fire_at"),
        }, indent=2, default=str)

    async def list_artifacts() -> str:
        """List named artifacts, latest version of each."""
        try:
            items = await facade.list_artifacts()
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(items, indent=2, default=str)

    async def put_artifact(
        name: str,
        content_b64: str,
        mime: str = "application/octet-stream",
    ) -> str:
        """Publish small base64-encoded content as a named artifact.

        Args:
            name: Artifact name.
            content_b64: File bytes, base64-encoded.
            mime: Content type.
        """
        try:
            version = await facade.put_artifact(name, content_b64, mime=mime)
        except Exception as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(version, indent=2, default=str)

    return [
        list_workflows,
        get_workflow_info,
        run_workflow,
        list_runs,
        get_run_status,
        cancel_run,
        schedule_workflow,
        list_artifacts,
        put_artifact,
    ]


class WorkflowManagerAgent:
    """An agent that manages workflows: list, run, schedule, inspect, cancel.

    Shipped because it was previously a cookbook — every user rebuilt it, and
    each rebuild reached into the Runtime differently.

    It holds a :class:`RuntimeFacade`, never a ``Runtime``, so it can start a
    run and read a journal but has no path to executing arbitrary code in the
    host process. That is the whole reason the tools moved to the facade.

    ``executor`` swaps the turn loop for LangGraph, Agno, Pydantic AI, or a
    host's own; ``None`` uses LOOM's built-in ReAct loop.

        from loom.agents.workflow_tools import WorkflowManagerAgent
        from loom.facade import LocalFacade

        manager = WorkflowManagerAgent(LocalFacade(runtime), model=provider)
        print(await manager.chat("what ran today?"))
    """

    INSTRUCTIONS = (
        "You manage workflows. You can list them, inspect one, start a run, "
        "check or cancel a run, and schedule a workflow with a cron "
        "expression.\n\n"
        "Answer from the tools, never from memory: workflow names, run ids, "
        "and statuses change between questions. Report ids verbatim — they are "
        "what the person acts on. When something fails, say what you tried."
    )

    def __init__(
        self,
        facade: Any,
        *,
        model: Any,
        executor: Any = None,
        instructions: str | None = None,
        max_turns: int = 12,
    ) -> None:
        from loom.facade import LocalFacade, RuntimeFacade

        self.facade = facade if isinstance(facade, RuntimeFacade) else LocalFacade(facade)
        self._model = model
        self._executor = executor
        self._instructions = instructions or self.INSTRUCTIONS
        self._max_turns = max_turns

    def build_agent(self) -> Any:
        """The underlying :class:`Agent`, for callers that want to configure it."""
        from loom.agents.agent import Agent
        from loom.agents.limits import UsageLimits
        from loom.agents.tools import tool

        return Agent(
            name="workflow_manager",
            instructions=self._instructions,
            model=self._model,
            executor=self._executor,
            tools=[tool(fn) for fn in build_workflow_tools(self.facade)],
            limits=UsageLimits(max_turns=self._max_turns),
        )

    async def chat(self, message: str) -> str:
        """Answer one question, using the tools as needed."""
        result = await self.build_agent()(message)
        return result.text()
