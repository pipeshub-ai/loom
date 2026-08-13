"""HTTP surface for a :class:`Runtime`.

Workflow *authoring* is Python — the durability guarantees come from re-entering
a Python function body, and that does not survive a language boundary. Workflow
*operation* does not need to be: starting runs, delivering events, and reading
history are ordinary requests. Putting them behind HTTP is what lets a Go
service start a workflow or a TypeScript UI watch one, without either of them
embedding a Python interpreter.

Requires the ``api`` extra::

    pip install workflow-builder[api]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from workflow_builder.core.exceptions import (
    AdmissionRejected,
    RegistryError,
)
from workflow_builder.core.models import ExecutionStatus
from workflow_builder.core.serde import decode, encode
from workflow_builder.security.rbac import AuthorizationError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from workflow_builder.runtime.engine import Runtime


class StartRunRequest(BaseModel):
    """Body for ``POST /runs``."""

    workflow: str
    input: Any = None
    idempotency_key: str | None = None
    """Reuse to make a retried request return the original run instead of a new one."""
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    wait: bool = False
    """Block until the run reaches a terminal state or parks. Off by default,
    because a workflow that sleeps for a day should not hold a socket open."""


class EventRequest(BaseModel):
    """Body for ``POST /runs/{run_id}/events``."""

    name: str
    payload: Any = None


class RunView(BaseModel):
    """A run as seen over the wire."""

    run_id: str
    workflow: str
    status: ExecutionStatus
    input: Any = None
    output: Any = None
    error: str | None = None
    created_at: str | None = None
    finished_at: str | None = None


class WorkflowView(BaseModel):
    """A workflow as seen over the wire."""

    name: str
    version: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    triggers: list[str] = Field(default_factory=list)
    executable: bool = True
    """Whether *this* process can run it. A published workflow that the serving
    process did not import is listed but cannot be started here — better to say
    so than to omit it and look like it does not exist."""
    code_hash: str = ""
    source_file: str = ""


def _view(record: Any) -> RunView:
    return RunView(
        run_id=record.run_id,
        workflow=record.workflow,
        status=record.status,
        input=decode(record.input),
        output=decode(record.output),
        error=record.error.message if record.error else None,
        created_at=record.created_at.isoformat() if record.created_at else None,
        finished_at=record.finished_at.isoformat() if record.finished_at else None,
    )


def create_app(runtime: Runtime, *, title: str = "LOOM") -> FastAPI:
    """Build a FastAPI app serving *runtime*.

    The app owns no state of its own — every route delegates to the Runtime, so
    what a client sees over HTTP and what embedded Python sees are the same
    execution history rather than two views that can drift.
    """
    from fastapi import FastAPI, HTTPException

    app = FastAPI(title=title)

    def _fail(exc: Exception) -> HTTPException:
        """Translate SDK errors into the status codes they actually mean."""
        if isinstance(exc, AuthorizationError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, RegistryError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, AdmissionRejected):
            # 429 for "come back later", 409 for "this will never be admitted".
            code = 429 if exc.retryable else 409
            return HTTPException(status_code=code, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/workflows", response_model=list[WorkflowView])
    async def list_workflows(published: bool = True) -> list[WorkflowView]:
        """Workflows this process imported, plus published ones when asked.

        Set ``published=false`` to see only what can be started here.
        """
        from workflow_builder.core.serde import json_schema_for

        views: dict[str, WorkflowView] = {}
        for definition in runtime.workflows.values():
            schema: dict[str, Any] = {}
            if definition.input_type is not None:
                try:
                    schema = json_schema_for(definition.input_type)
                except Exception:
                    schema = {}
            views[definition.name] = WorkflowView(
                name=definition.name,
                version=definition.version,
                description=definition.description,
                input_schema=schema,
                triggers=[spec.name for spec in definition.triggers],
                executable=True,
                code_hash=definition.code_hash,
            )

        if published:
            for record in await runtime.published():
                # An imported definition wins: it is the one that would run.
                if record.name in views:
                    views[record.name].source_file = record.source_file
                    continue
                views[record.name] = WorkflowView(
                    name=record.name,
                    version=record.version,
                    description=record.description,
                    input_schema=record.input_schema,
                    triggers=record.triggers,
                    executable=False,
                    code_hash=record.code_hash,
                    source_file=record.source_file,
                )
        return sorted(views.values(), key=lambda v: v.name)

    @app.post("/runs", response_model=RunView, status_code=202)
    async def start_run(body: StartRunRequest) -> RunView:
        try:
            if body.wait:
                result = await runtime.run(
                    body.workflow,
                    body.input,
                    idempotency_key=body.idempotency_key,
                    tags=body.tags,
                    metadata=body.metadata,
                )
                run_id = result.run_id
            else:
                run_id = await runtime.submit(
                    body.workflow,
                    body.input,
                    idempotency_key=body.idempotency_key,
                    metadata=body.metadata,
                )
        except Exception as exc:
            raise _fail(exc) from exc

        record = await runtime.get(run_id)
        if record is None:  # pragma: no cover - would mean the store lost it
            raise HTTPException(status_code=500, detail=f"run {run_id} vanished")
        return _view(record)

    @app.get("/runs", response_model=list[RunView])
    async def list_runs(
        workflow: str | None = None,
        status: ExecutionStatus | None = None,
        limit: int = 50,
    ) -> list[RunView]:
        try:
            records = await runtime.list_runs(
                workflow=workflow, status=status, limit=limit
            )
        except Exception as exc:
            raise _fail(exc) from exc
        return [_view(record) for record in records]

    @app.get("/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str) -> RunView:
        try:
            record = await runtime.get(run_id)
        except Exception as exc:
            raise _fail(exc) from exc
        if record is None:
            raise HTTPException(status_code=404, detail=f"no run '{run_id}'")
        return _view(record)

    @app.get("/runs/{run_id}/journal")
    async def get_journal(run_id: str) -> list[dict[str, Any]]:
        try:
            if await runtime.get(run_id) is None:
                raise HTTPException(status_code=404, detail=f"no run '{run_id}'")
            entries = await runtime.history(run_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _fail(exc) from exc
        return [
            {
                "seq": entry.seq,
                "name": entry.name,
                "kind": entry.kind,
                "status": entry.status.value,
                "attempts": entry.attempts,
                "output": encode(entry.output),
                "error": entry.error.message if entry.error else None,
            }
            for entry in entries
        ]

    @app.post("/runs/{run_id}/events", status_code=202)
    async def send_event(run_id: str, body: EventRequest) -> dict[str, Any]:
        try:
            if await runtime.get(run_id) is None:
                raise HTTPException(status_code=404, detail=f"no run '{run_id}'")
            await runtime.send_event(run_id, body.name, body.payload)
        except HTTPException:
            raise
        except Exception as exc:
            raise _fail(exc) from exc
        return {"run_id": run_id, "event": body.name, "delivered": True}

    @app.post("/runs/{run_id}/cancel", response_model=RunView)
    async def cancel_run(run_id: str) -> RunView:
        try:
            if await runtime.get(run_id) is None:
                raise HTTPException(status_code=404, detail=f"no run '{run_id}'")
            await runtime.cancel(run_id)
            record = await runtime.get(run_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _fail(exc) from exc
        assert record is not None
        return _view(record)

    @app.post("/runs/{run_id}/retry", response_model=RunView)
    async def retry_run(run_id: str) -> RunView:
        """Re-run a failed execution from its first failed step.

        Distinct from replay: replay rehearses against the recorded journal and
        repeats nothing, while this prunes the failure and does the work again
        against current code.
        """
        try:
            if await runtime.get(run_id) is None:
                raise HTTPException(status_code=404, detail=f"no run '{run_id}'")
            await runtime.retry(run_id)
            record = await runtime.get(run_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _fail(exc) from exc
        assert record is not None
        return _view(record)

    @app.post("/runs/{run_id}/replay", response_model=RunView)
    async def replay_run(run_id: str) -> RunView:
        try:
            if await runtime.get(run_id) is None:
                raise HTTPException(status_code=404, detail=f"no run '{run_id}'")
            result = await runtime.replay(run_id)
            record = await runtime.get(result.run_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _fail(exc) from exc
        assert record is not None
        return _view(record)

    return app
