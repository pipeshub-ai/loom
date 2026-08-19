"""Deprecated. Use :class:`loom.facade.LocalFacade`.

``RuntimeBridge`` predated the CLI and grew its own copy of the same ten
operations the CLI needed, under different key names. Both are now
:class:`RuntimeFacade`, so there is one port to fix bugs in rather than two.

This module survives as a compatibility shim: it maps the old key names onto
the shared facade and warns once per call site. It will be removed in a future
release.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

from loom.facade import LocalFacade

if TYPE_CHECKING:
    from loom.runtime.engine import Runtime
    from loom.runtime.workflow import WorkflowDefinition

__all__ = ["RuntimeBridge"]

_DEPRECATION = (
    "RuntimeBridge is deprecated; use loom.facade.LocalFacade "
    "(or RemoteFacade), which the CLI and MCP server both use."
)


def _store_from_url(url: str) -> Any:
    from loom.stores.factory import from_url

    return from_url(url)


class RuntimeBridge:
    """Adapts a :class:`Runtime` to the flat dict shapes the old MCP handlers used.

    .. deprecated::
        Use :class:`~loom.facade.LocalFacade`. The key names differ:
        this class emits ``workflow_id`` and ``id`` where the facade emits
        ``workflow`` and ``name``.
    """

    def __init__(
        self,
        store_url: str = "memory://",
        *,
        runtime: Runtime | None = None,
    ) -> None:
        warnings.warn(_DEPRECATION, DeprecationWarning, stacklevel=2)

        if runtime is None:
            from loom.runtime.engine import Runtime as _Runtime

            runtime = _Runtime(store=_store_from_url(store_url))
        self._facade = LocalFacade(runtime)
        self._store_url = store_url
        self._schemas: dict[str, dict[str, Any]] = {}

    @property
    def runtime(self) -> Runtime:
        """The wrapped Runtime, for callers that need the full API."""
        # `LocalFacade.runtime` is declared `Any` so the facade module need not
        # import the engine. Naming the type here is what stops that `Any`
        # leaking out through a property this shim advertises as a `Runtime`.
        runtime: Runtime = self._facade.runtime
        return runtime

    @property
    def facade(self) -> LocalFacade:
        """The replacement object, for callers migrating off this shim."""
        return self._facade

    # -- workflow registry -------------------------------------------

    def register_workflow(
        self,
        workflow: WorkflowDefinition[Any, Any, Any],
        *,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self._facade.runtime.register(workflow)
        if description:
            workflow.description = description
        if input_schema is not None:
            self._schemas[workflow.name] = input_schema

    async def list_workflows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": entry["name"],
                "description": entry.get("description", ""),
                "version": entry.get("version", "1"),
                "input_schema": self._schemas.get(entry["name"], {}),
            }
            for entry in await self._facade.workflows()
        ]

    # -- run lifecycle -----------------------------------------------

    async def run_workflow(self, workflow_id: str, input_data: Any) -> dict[str, Any]:
        if workflow_id not in self._facade.runtime.workflows:
            return {"error": f"Workflow '{workflow_id}' not found"}
        return _rename(await self._facade.start(workflow_id, input_data))

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        run = await self._facade.get(run_id)
        return _rename(run) if run else {"error": f"Run '{run_id}' not found"}

    async def list_runs(
        self,
        *,
        workflow_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return [
            _rename(run)
            for run in await self._facade.list_runs(
                workflow=workflow_id, status=status, limit=limit
            )
        ]

    async def get_run_journal(self, run_id: str) -> list[dict[str, Any]]:
        if await self._facade.get(run_id) is None:
            return []
        return await self._facade.journal(run_id)

    async def resume_run(self, run_id: str) -> dict[str, Any]:
        if await self._facade.get(run_id) is None:
            return {"error": f"Run '{run_id}' not found"}
        await self._facade.runtime.resume(run_id)
        run = await self._facade.get(run_id) or {}
        return {"run_id": run_id, "status": run.get("status", "")}

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        if await self._facade.get(run_id) is None:
            return {"error": f"Run '{run_id}' not found"}
        run = await self._facade.cancel(run_id)
        return {"run_id": run_id, "status": run.get("status", "")}

    # -- events ------------------------------------------------------

    async def send_event(
        self, run_id: str, event_name: str, payload: Any
    ) -> dict[str, Any]:
        if await self._facade.get(run_id) is None:
            return {"error": f"Run '{run_id}' not found", "delivered": False}
        await self._facade.send_event(run_id, event_name, payload)
        return {"run_id": run_id, "event": event_name, "delivered": True}

    # -- replay ------------------------------------------------------

    async def replay_run(self, run_id: str) -> dict[str, Any]:
        if await self._facade.get(run_id) is None:
            return {"error": f"Run '{run_id}' not found"}
        result = await self._facade.replay(run_id)
        return {
            "run_id": run_id,
            "replay_run_id": result["run_id"],
            "replay_status": result["status"],
        }


def _rename(run: dict[str, Any]) -> dict[str, Any]:
    """Translate facade keys to the names this shim has always emitted."""
    renamed = dict(run)
    renamed["workflow_id"] = renamed.pop("workflow", "")
    return renamed
