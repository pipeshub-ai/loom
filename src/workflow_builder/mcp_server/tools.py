"""What an MCP client can *do* with a Runtime.

Every function here is a plain coroutine over a :class:`RuntimeFacade`. None of
them import ``mcp``, so they are unit-testable without a client, a transport, or
a protocol handshake — the MCP wiring lives in :mod:`.server` and does nothing
but adapt these.

Results are JSON text. A model reasons better over a structure it can parse than
over prose, and the failure cases carry an ``error`` key rather than raising, so
a tool call returns something the model can act on instead of aborting the turn.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow_builder.facade import RuntimeFacade

__all__ = [
    "approve_run",
    "cancel_run",
    "get_run_journal",
    "get_run_status",
    "list_runs",
    "list_workflows",
    "replay_run",
    "retry_run",
    "run_workflow",
    "send_event",
]


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _missing(run_id: str) -> str:
    return _json({"error": f"No run '{run_id}'."})


async def list_workflows(facade: RuntimeFacade) -> str:
    """Every workflow this server can run, with input schemas."""
    workflows = await facade.workflows()
    if not workflows:
        return _json(
            {
                "workflows": [],
                "hint": "No workflows are loaded. Start the server with "
                "--module <file.py>, or list modules under [tool.loom] in "
                "pyproject.toml.",
            }
        )
    return _json({"workflows": workflows})


async def run_workflow(
    facade: RuntimeFacade,
    workflow: str,
    input_json: str = "null",
    *,
    idempotency_key: str | None = None,
) -> str:
    """Start a workflow and wait for it to finish or park."""
    try:
        payload = json.loads(input_json) if input_json else None
    except json.JSONDecodeError:
        # A bare string is a legitimate workflow input; only reject it if the
        # caller clearly meant JSON.
        payload = input_json

    catalog = {entry["name"]: entry for entry in await facade.workflows()}
    if workflow not in catalog:
        return _json(
            {
                "error": f"No workflow named '{workflow}'.",
                "available": sorted(catalog),
            }
        )

    mismatch = _shape_error(catalog[workflow].get("input_schema"), payload)
    if mismatch is not None:
        return _json(mismatch)

    run = await facade.start(workflow, payload, idempotency_key=idempotency_key)
    return _json(_annotate(run))


async def get_run_status(facade: RuntimeFacade, run_id: str) -> str:
    """Status, input, output, and error for one run."""
    run = await facade.get(run_id)
    return _missing(run_id) if run is None else _json(_annotate(run))


async def list_runs(
    facade: RuntimeFacade,
    workflow: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> str:
    """Recent runs, optionally filtered by workflow or status."""
    runs = await facade.list_runs(workflow=workflow, status=status, limit=limit)
    return _json({"runs": [_annotate(run) for run in runs], "count": len(runs)})


async def get_run_journal(facade: RuntimeFacade, run_id: str) -> str:
    """The durable operations a run recorded, in order.

    This is the artifact to read when asked why a run behaved as it did — each
    entry is a step that actually executed, with its status and attempt count.
    """
    if await facade.get(run_id) is None:
        return _missing(run_id)
    entries = await facade.journal(run_id)
    return _json({"run_id": run_id, "journal": entries, "count": len(entries)})


async def approve_run(
    facade: RuntimeFacade, run_id: str, subject: str, approved: bool = True
) -> str:
    """Answer a human approval a run is parked on."""
    run = await facade.get(run_id)
    if run is None:
        return _missing(run_id)

    awaiting = str(run.get("awaiting_event") or "")
    if awaiting and awaiting != f"approval:{subject}":
        return _json(
            {
                "error": f"Run '{run_id}' is waiting on '{awaiting}', not "
                f"'approval:{subject}'.",
                "awaiting": awaiting,
            }
        )

    await facade.send_event(run_id, f"approval:{subject}", {"approved": approved})
    return _json(_annotate(await facade.get(run_id) or {}))


async def send_event(
    facade: RuntimeFacade, run_id: str, event: str, payload_json: str = "null"
) -> str:
    """Deliver an event to a run parked on it."""
    if await facade.get(run_id) is None:
        return _missing(run_id)
    try:
        payload = json.loads(payload_json) if payload_json else None
    except json.JSONDecodeError:
        payload = payload_json

    await facade.send_event(run_id, event, payload)
    return _json(_annotate(await facade.get(run_id) or {}))


async def cancel_run(facade: RuntimeFacade, run_id: str) -> str:
    """Cancel a run. Already-finished runs are left alone."""
    if await facade.get(run_id) is None:
        return _missing(run_id)
    return _json(_annotate(await facade.cancel(run_id)))


async def retry_run(facade: RuntimeFacade, run_id: str) -> str:
    """Re-run a failed execution from its first failed step, against current code."""
    if await facade.get(run_id) is None:
        return _missing(run_id)
    return _json(_annotate(await facade.retry(run_id)))


async def replay_run(facade: RuntimeFacade, run_id: str) -> str:
    """Re-execute from the journal without repeating any side effect."""
    if await facade.get(run_id) is None:
        return _missing(run_id)
    return _json(_annotate(await facade.replay(run_id)))


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}

_EXAMPLES = {
    "object": '{"field": ...}',
    "array": "[...]",
    "string": '"some text"',
    "integer": "42",
    "number": "42.0",
    "boolean": "true",
}


def _shape_error(schema: Any, payload: Any) -> dict[str, Any] | None:
    """Reject an input whose shape the workflow cannot accept.

    Without this the mismatch surfaces as an AttributeError from deep inside a
    step — a caller reading that has no way to tell a wrong input from a broken
    workflow, and the wasted run sits in the run list looking like a defect.
    Only an unambiguous declared type is checked; anything expressive enough to
    be uncertain about is left to the workflow.
    """
    if not isinstance(schema, dict):
        return None
    declared = schema.get("type")
    if not isinstance(declared, str) or declared not in _JSON_TYPES:
        return None

    accepted = _JSON_TYPES[declared]
    if isinstance(payload, accepted) and not (
        declared in {"integer", "number"} and isinstance(payload, bool)
    ):
        return None

    return {
        "error": (
            f"This workflow takes {declared}, but input_json was "
            f"{type(payload).__name__}. Nothing was run."
        ),
        "expected_schema": schema,
        "example_input_json": _EXAMPLES.get(declared, "null"),
    }


def _annotate(run: dict[str, Any]) -> dict[str, Any]:
    """Add the next action a parked run needs.

    A suspended run is the one state a model reliably misreads as failure. Saying
    what it is waiting for, and which tool answers it, turns a dead end into an
    obvious next call.
    """
    if run.get("status") != "suspended":
        return run

    awaiting = str(run.get("awaiting_event") or "")
    annotated = dict(run)
    if awaiting.startswith("approval:"):
        subject = awaiting.split(":", 1)[1]
        annotated["waiting_for"] = f"human approval '{subject}'"
        annotated["next_action"] = (
            f"approve_run(run_id='{run.get('run_id')}', subject='{subject}')"
        )
    elif awaiting:
        annotated["waiting_for"] = f"event '{awaiting}'"
        annotated["next_action"] = (
            f"send_event(run_id='{run.get('run_id')}', event='{awaiting}')"
        )
    else:
        annotated["waiting_for"] = "a timer"
        annotated["next_action"] = "wait; it resumes on its own"
    annotated["note"] = "Suspended is not failure — the run costs nothing while parked."
    return annotated
