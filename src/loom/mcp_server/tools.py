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
    from loom.facade import RuntimeFacade

__all__ = [
    "MAX_RESPONSE_CHARS",
    "approve_run",
    "author_workflow",
    "cancel_run",
    "get_artifact_url",
    "get_run_journal",
    "get_run_status",
    "get_workflow_info",
    "list_artifacts",
    "list_runs",
    "list_workflows",
    "put_artifact",
    "replay_run",
    "retry_run",
    "run_workflow",
    "schedule_workflow",
    "search_toolsets",
    "send_event",
    "show_toolset",
]

MAX_RESPONSE_CHARS = 8_000
"""Ceiling on one tool's serialized response.

Not a client-side limit — a discipline on this server. An MCP client pays in
tokens for whatever a tool returns, and an unbounded ``list_runs`` or
``get_run_journal`` on a long-lived install would size a single tool call by
how much history exists rather than by what the caller asked to see. Every
list-shaped response below is capped to this and pages via ``next_offset``
instead; that also caps the total schema+response surface a model has to
reason over per turn, which is the same budget ``tests/test_mcp_server.py``
enforces on the *schemas* themselves.
"""


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _missing(run_id: str) -> str:
    return _json({"error": f"No run '{run_id}'."})


def _run_json(run: dict[str, Any]) -> str:
    """A single run response, annotated and capped.

    Every tool that hands back one run's state (as opposed to a list of
    them) funnels through here, so an oversized ``output`` gets the same
    ``MAX_RESPONSE_CHARS`` treatment ``list_runs``/``get_run_journal`` give
    an oversized list.
    """
    return _json(_cap_text(_annotate(run)))


def _cap_list(payload: dict[str, Any], list_key: str, offset: int) -> dict[str, Any]:
    """Shrink ``payload[list_key]`` until the response fits ``MAX_RESPONSE_CHARS``.

    Halves the list rather than trimming one item at a time — O(log n) calls
    to the JSON encoder instead of O(n) for a list that starts far over
    budget. Leaves the payload untouched when it already fits, so this costs
    one extra ``json.dumps`` on the common case rather than a rebuild.
    """
    items = payload.get(list_key)
    if not isinstance(items, list) or len(_json(payload)) <= MAX_RESPONSE_CHARS:
        return payload

    kept = items
    while len(kept) > 1:
        kept = kept[: (len(kept) + 1) // 2]
        if len(_json({**payload, list_key: kept})) <= MAX_RESPONSE_CHARS:
            break

    result = dict(payload)
    result[list_key] = kept
    result["truncated"] = True
    result["next_offset"] = offset + len(kept)
    return result


def _cap_text(payload: dict[str, Any]) -> dict[str, Any]:
    """Fallback cap for a single-object response with nothing to page.

    Only ``run_workflow``/``get_run_status`` and friends can hit this — an
    oversized ``output`` or error traceback is the one way a run-shaped
    response blows the budget with no list to trim. Replaces the body with a
    bounded preview rather than truncating the JSON text itself, which would
    hand the caller a string that fails to parse.
    """
    text = _json(payload)
    if len(text) <= MAX_RESPONSE_CHARS:
        return payload
    keys = list(payload)
    envelope = {
        "truncated": True,
        "total_chars": len(text),
        "note": "Full response exceeded the size budget; showing a preview. "
        "Fields present: " + ", ".join(keys),
        "preview": "",
    }
    # Room left for the preview once the envelope's own JSON is accounted
    # for, so the *whole* returned document — not just this one field —
    # respects the budget. The preview is a slice of *encoded* JSON text, so
    # it can itself contain quotes or backslashes that need re-escaping once
    # embedded as a string value — the trim loop accounts for that, rather
    # than assuming a 1:1 length budget.
    room = max(0, MAX_RESPONSE_CHARS - len(_json(envelope)))
    preview = text[:room]
    envelope["preview"] = preview
    overage = len(_json(envelope)) - MAX_RESPONSE_CHARS
    while overage > 0 and preview:
        preview = preview[: max(0, len(preview) - overage)]
        envelope["preview"] = preview
        overage = len(_json(envelope)) - MAX_RESPONSE_CHARS
    return envelope


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
    return _json(_cap_list({"workflows": workflows}, "workflows", 0))


async def get_workflow_info(facade: RuntimeFacade, workflow: str) -> str:
    """One workflow's description, version, triggers, and input schema.

    ``list_workflows`` is capped and pages, so on an install with many
    workflows the one being asked about may not be in the answer at all —
    and a model reading a truncated list has no way to tell "not here" from
    "does not exist". This answers for one by name, and names the others
    when there is no such workflow.
    """
    catalog = {entry["name"]: entry for entry in await facade.workflows()}
    found = catalog.get(workflow)
    if found is None:
        return _json(
            {"error": f"No workflow named '{workflow}'.", "available": sorted(catalog)}
        )
    return _json(_cap_text(found))


async def schedule_workflow(
    facade: RuntimeFacade, workflow: str, cron: str, timezone: str = "UTC"
) -> str:
    """Fire a workflow on a cron expression, durably.

    The trigger lives in the store, not in this process, so it survives the
    server restarting — which is the difference between a schedule and a
    reminder to start something.
    """
    if not any(entry["name"] == workflow for entry in await facade.workflows()):
        return _json({"error": f"No workflow named '{workflow}'."})
    try:
        return _json(await facade.schedule(workflow, cron, timezone=timezone))
    except Exception as exc:
        # A malformed cron expression and an unknown timezone both land here,
        # and both are things the caller fixes by calling again — a raise
        # would end the turn instead.
        return _json({"error": str(exc)})


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
    return _run_json(run)


async def get_run_status(facade: RuntimeFacade, run_id: str) -> str:
    """Status, input, output, and error for one run."""
    run = await facade.get(run_id)
    return _missing(run_id) if run is None else _run_json(run)


async def list_runs(
    facade: RuntimeFacade,
    workflow: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> str:
    """Recent runs, optionally filtered by workflow or status."""
    runs = await facade.list_runs(workflow=workflow, status=status, limit=limit)
    payload = {"runs": [_annotate(run) for run in runs], "count": len(runs)}
    return _json(_cap_list(payload, "runs", 0))


async def get_run_journal(facade: RuntimeFacade, run_id: str, offset: int = 0) -> str:
    """The durable operations a run recorded, in order.

    This is the artifact to read when asked why a run behaved as it did — each
    entry is a step that actually executed, with its status and attempt count.

    Pass ``offset`` to page through a long journal; a capped response's
    ``next_offset`` names where the next call should resume.
    """
    if await facade.get(run_id) is None:
        return _missing(run_id)
    entries = await facade.journal(run_id)
    page = entries[offset:]
    payload = {"run_id": run_id, "journal": page, "count": len(entries)}
    return _json(_cap_list(payload, "journal", offset))


async def get_run_progress(
    facade: RuntimeFacade, run_id: str, offset: int = 0
) -> str:
    """What a run has said about itself while running.

    Distinct from the journal: the journal is what a run durably did, this is
    what it narrated along the way. Read it to answer "what is it doing right
    now?" about a run that is still going — a step that takes minutes is one
    journal entry and can be many reports.

    Pass ``offset`` to get only what is new since the last read.
    """
    if await facade.get(run_id) is None:
        return _missing(run_id)
    reports = await facade.reports(run_id, offset)
    payload = {
        "run_id": run_id,
        "reports": reports,
        "count": len(reports),
        "next_offset": offset + len(reports),
    }
    return _json(_cap_list(payload, "reports", offset))


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
    return _run_json(await facade.get(run_id) or {})


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
    return _run_json(await facade.get(run_id) or {})


def _can_ask(facade: RuntimeFacade, interaction: Any) -> RuntimeFacade:
    """*facade*, able to put a question to the user through the MCP client.

    A **per-call copy**, never a mutation: ``base_facade`` is bound once when
    the server is built and is shared by every concurrent request, so setting
    an interaction on it would hand one client's question to another client's
    session.

    Silently a no-op when the client declared no ``elicitation`` capability, or
    when the facade has nowhere to put one — the tool is still offered, because
    ``author_workflow`` does much more than ask, and the agent simply never
    sees ``ask_user``.
    """
    import dataclasses

    if interaction is None or not getattr(interaction, "available", lambda: False)():
        return facade
    if not dataclasses.is_dataclass(facade) or not hasattr(facade, "user_interaction"):
        return facade
    return dataclasses.replace(facade, user_interaction=interaction)


async def author_workflow(
    facade: RuntimeFacade,
    spec: str,
    packages_json: str = "[]",
    workflow_input_json: str = "",
    observe: bool = True,
    interaction: Any = None,
) -> str:
    """Write a whole workflow from a description, using loom's own agent.

    The one-shot counterpart to the authoring primitives beside it. Those hand
    a caller the pieces — validate this code, smoke-test it, look this
    operation up — and expect the caller to drive the loop. This runs loom's
    loop: discovery, observation, generation, the whole verification pipeline,
    and repair.

    Errors come back as a payload rather than a raise, as everywhere else here:
    a raise aborts the model's turn, and "no model key is configured" is
    something to act on, not to crash over.
    """
    try:
        packages = json.loads(packages_json) if packages_json else []
    except json.JSONDecodeError as exc:
        return _json({"error": f"packages_json is not valid JSON: {exc}"})
    if not isinstance(packages, list):
        return _json({"error": "packages_json must be a JSON array of names"})

    facade = _can_ask(facade, interaction)

    workflow_input: Any = None
    if workflow_input_json:
        try:
            workflow_input = json.loads(workflow_input_json)
        except json.JSONDecodeError:
            # A bare string is the common case and demanding '"text"' for it is
            # hostile, exactly as `loom run -i` decided.
            workflow_input = workflow_input_json

    try:
        result = await facade.author(
            spec,
            packages=[str(p) for p in packages] or None,
            smoke_input=workflow_input,
            observe=observe,
        )
    except Exception as exc:
        return _json({"error": str(exc), "authored": False})

    return _json(result)


async def cancel_run(facade: RuntimeFacade, run_id: str) -> str:
    """Cancel a run. Already-finished runs are left alone."""
    if await facade.get(run_id) is None:
        return _missing(run_id)
    return _run_json(await facade.cancel(run_id))


async def retry_run(facade: RuntimeFacade, run_id: str) -> str:
    """Re-run a failed execution from its first failed step, against current code."""
    if await facade.get(run_id) is None:
        return _missing(run_id)
    return _run_json(await facade.retry(run_id))


async def replay_run(facade: RuntimeFacade, run_id: str) -> str:
    """Re-execute from the journal without repeating any side effect."""
    if await facade.get(run_id) is None:
        return _missing(run_id)
    return _run_json(await facade.replay(run_id))


async def list_artifacts(facade: RuntimeFacade) -> str:
    """List named artifacts, each at its latest version."""
    try:
        items = await facade.list_artifacts()
    except Exception as exc:
        return _json({"error": str(exc)})
    payload = {"artifacts": items}
    return _json(_cap_list(payload, "artifacts", 0))


async def get_artifact_url(
    facade: RuntimeFacade, name: str, version: int | None = None, expires_in: int = 3600
) -> str:
    """Mint a time-limited download URL for an artifact.

    Falls back to a base64 payload when the blob backend cannot sign URLs.

    Args:
        name: Artifact name.
        version: Specific version; omit for latest.
        expires_in: URL lifetime in seconds.
    """
    try:
        return _json(await facade.artifact_url(name, version, expires_in))
    except Exception as exc:
        try:
            payload = await facade.read_artifact(name, version)
        except Exception:
            return _json({"error": str(exc)})
        return _json(
            {
                "name": payload.get("name", name),
                "version": payload.get("version"),
                "mime": payload.get("mime"),
                "size": payload.get("size"),
                "content_b64": payload.get("content_b64"),
                "note": "backend cannot sign URLs; content is inline",
            }
        )


async def put_artifact(
    facade: RuntimeFacade,
    name: str,
    content_b64: str,
    mime: str = "application/octet-stream",
) -> str:
    """Publish small content as a named artifact.

    For large files, ask the operator to use a presigned upload URL instead.

    Args:
        name: Artifact name.
        content_b64: File bytes, base64-encoded.
        mime: Content type.
    """
    try:
        return _json(await facade.put_artifact(name, content_b64, mime=mime))
    except Exception as exc:
        return _json({"error": str(exc)})


def _catalog() -> Any:
    """The process-global toolset catalog these two tools browse.

    Not facade-scoped: which toolsets *exist* is server-wide metadata, not
    per-run data, and it is the same registry
    ``agents/coding_tools.py::search_toolsets``/``show_toolset`` already
    browse for the coding agent's own ReAct loop — one catalog, browsed by
    two different callers, rather than two copies that can drift.

    MCP seeds the shipped toolsets (and ``loom_toolset`` entry points) so a
    client searching for "jira" finds it without an extra register call.
    """
    from loom.toolsets.registry import (
        get_catalog,
        register_available_toolsets,
    )

    register_available_toolsets()
    return get_catalog()


async def search_toolsets(query: str) -> str:
    """Search registered toolsets (Jira, Gmail, Slack, ...) by keyword.

    Args:
        query: Keywords to search for, e.g. "jira" or "calendar".

    Pull integration detail on demand with this and show_toolset instead of
    preloading every toolset's operations into context up front.
    """
    cards = _catalog().search(query)
    payload = {"toolsets": [c.model_dump() for c in cards]}
    return _json(_cap_list(payload, "toolsets", 0))


async def show_toolset(toolset_id: str, group: str | None = None) -> str:
    """List the operations one toolset exposes, optionally filtered to a group.

    Args:
        toolset_id: A toolset id from search_toolsets, e.g. "jira".
        group: Only this operation group, e.g. "issues".
    """
    try:
        table = _catalog().show(toolset_id, group)
    except KeyError as exc:
        return _json({"error": str(exc)})
    payload = table.model_dump()
    return _json(_cap_list(payload, "ops", 0))


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
