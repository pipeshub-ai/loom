"""What an MCP client can *author* with LOOM's coding toolchain.

Every function here is a plain coroutine — no ``mcp`` import — over the same
components :class:`~loom.agents.coding_agent.WorkflowCodingAgent`
uses internally via ``build_coding_tools()``: the toolset catalog, the code
validator, and the sandboxed smoke runner. The MCP wiring lives in
:mod:`.server` and does nothing but bind these to tool names and annotations.

The design is deliberately *not* a wrapper around
``WorkflowCodingAgent.generate()``. That would nest a second LLM inside the
host's own model turn, need a server-side API key, and fight MCP's tool-call
timeouts. Instead, each stage of the agent's own discover -> generate ->
validate -> smoke -> save pipeline is exposed as its own tool, so the host
model — Cursor, Claude — drives the loop with the model it already has, and
LOOM supplies only the verification the host model cannot do itself: real
toolset schemas, AST/import checks, and a real (sandboxed) execution.

Every tool returns JSON text with an ``error`` key on failure. None raise —
a raise aborts the calling model's turn; a payload it can read and act on.
"""

from __future__ import annotations

import json
from typing import Any

#: Same ceiling as ``mcp_server/tools.py`` — a discipline on what this server
#: hands back, not a client-side limit.
MAX_RESPONSE_CHARS = 8_000

__all__ = [
    "MAX_RESPONSE_CHARS",
    "STATIC_STAGE_NAMES",
    "call_read_operation",
    "get_tool_contract",
    "get_tool_docs",
    "save_workflow",
    "smoke_test_workflow",
    "validate_workflow_code",
]


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _catalog() -> Any:
    """The process-global toolset catalog these tools browse.

    Same registry ``mcp_server/tools.py::search_toolsets``/``show_toolset``
    and the coding agent's own ReAct tools browse — one catalog, multiple
    callers, so a client that just searched for "jira" finds the same
    toolset here without a second registration step.
    """
    from loom.toolsets.registry import get_catalog, register_available_toolsets

    register_available_toolsets()
    return get_catalog()


async def get_tool_contract(op_path: str) -> str:
    """Full typed contract for one toolset operation.

    Args:
        op_path: Dotted path like ``"jira.issues.search"`` — toolset id, then
            the operation id from ``show_toolset``.

    Returns JSON: ``op_id``, ``input_schema``, ``output_schema``, ``scopes``,
    ``effect``, ``description``, ``idempotent``, ``pagination``,
    ``import_line``, ``toolset_id``. ``import_line`` is the exact
    ``from ... import ...`` statement the generated code needs — it comes
    from the toolset's manifest, not the operation contract itself.
    """
    catalog = _catalog()
    try:
        contract = catalog.stub(op_path)
    except (KeyError, ValueError) as exc:
        return _json({"error": str(exc)})

    result = contract.model_dump()
    toolset_id = op_path.split(".", 1)[0]
    manifest = catalog.get(toolset_id)
    result["import_line"] = manifest.import_line() if manifest is not None else ""
    result["toolset_id"] = toolset_id
    if result.get("pagination"):
        result["pagination_note"] = (
            "This operation returns a Results list (a list subclass). "
            "Check .complete (False when max_results cut it short), "
            ".total (how many matched), and .cursor (continuation token). "
            "Set max_results high enough or loop with the cursor. "
            "Never silently drop results."
        )
    return _json(result)


async def get_tool_docs(toolset_id: str) -> str:
    """Usage documentation for a toolset: imports, signatures, examples.

    Args:
        toolset_id: e.g. ``"jira"``, ``"confluence"``.

    Not every toolset has hand-written docs — only ones registered via
    ``register_tool_docs`` (built-in: jira, confluence, langchain). Toolsets
    without them are still fully usable via ``get_tool_contract``; this is a
    denser, example-carrying supplement where it exists.
    """
    from loom.agents.coding_tools import _TOOL_DOCS_REGISTRY, _ensure_builtin_docs

    _ensure_builtin_docs()
    docs = _TOOL_DOCS_REGISTRY.get(toolset_id)
    if docs is None:
        return _json(
            {
                "error": f"No tool docs registered for '{toolset_id}'.",
                "available": sorted(_TOOL_DOCS_REGISTRY),
                "note": "get_tool_contract works for any registered toolset, "
                "with or without docs.",
            }
        )
    if callable(docs):
        docs = docs()
        _TOOL_DOCS_REGISTRY[toolset_id] = docs
    return docs if isinstance(docs, str) else _json(docs)


async def call_read_operation(
    op_path: str,
    arguments_json: str = "{}",
    *,
    seen: dict[str, int] | None = None,
) -> str:
    """Execute a READ-ONLY toolset operation, for resolving entities before
    writing code that depends on them.

    Use this to turn a name in a spec into the id generated code actually
    needs — which account id is "Vishwjeet", whether project "SAAS" exists —
    rather than guessing and shipping code that runs and returns nothing.

    Only operations declared ``read`` may be called; a write or destructive
    operation is refused. This performs a real call against connected
    credentials — a missing credential or network failure comes back as an
    ``error`` explaining that authoring cannot resolve it here, not a crash.

    Args:
        op_path: Fully qualified operation, e.g. ``"jira.projects.list"``.
        arguments_json: The operation's arguments, JSON-encoded object, e.g.
            ``'{"name": "Vishwjeet"}'``. Defaults to no arguments.
    """
    from loom.agents.coding_tools import _call_read_operation

    try:
        arguments = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as exc:
        return _json({"error": f"arguments_json is not valid JSON: {exc}"})

    result = await _call_read_operation(op_path, arguments, registry=None, seen=seen)
    return result[:MAX_RESPONSE_CHARS]


#: The non-executing half of ``agents/stages.py::default_stages()``. Every
#: stage here reads the source and nothing else — no subprocess that runs the
#: workflow, no second model, no API key — which is what makes the whole set
#: safe to run inside one MCP tool call. The executing stages (smoke, replay)
#: live behind :func:`smoke_test_workflow`, and ``critique`` needs a model this
#: server deliberately does not have.
STATIC_STAGE_NAMES = (
    "compile", "static", "grants", "coverage", "resolution", "lint", "types",
)

#: Stages that judge the code against the *request* rather than against the
#: language. With no spec they have nothing to compare and find nothing, which
#: is not the same as finding nothing wrong — see :func:`_stage_rows`.
_SPEC_STAGES = frozenset({"coverage", "resolution"})


def _static_pipeline() -> Any:
    """The pipeline :func:`validate_workflow_code` runs.

    Built from ``agents/stages.py`` rather than re-implemented, so a stage
    added for the coding agent reaches an MCP client too. Before this, the
    tool ran a hand-rolled ``CodeValidator`` call and a client got two of the
    seven — including neither of the two written from observed failures that
    compile and validate perfectly cleanly.
    """
    from loom.agents.checks import CheckPipeline
    from loom.agents.stages import (
        CompileStage,
        CoverageStage,
        GrantStage,
        LintStage,
        ResolutionStage,
        StaticStage,
        TypeStage,
    )

    return CheckPipeline(
        [
            CompileStage(),
            StaticStage(),
            GrantStage(_catalog()),
            CoverageStage(),
            ResolutionStage(),
            LintStage(),
            TypeStage(),
        ]
    )


async def validate_workflow_code(
    code: str,
    allowed_packages: str | None = None,
    spec: str = "",
) -> str:
    """Validate workflow code against LOOM's rules. Does not run it.

    Runs the same seven non-executing stages the coding agent's own pipeline
    runs, cheapest first, stopping at the first blocking failure: ``compile``,
    ``static`` (structure, determinism, imports, store choice, toolset
    availability), ``grants``, ``coverage``, ``resolution``, ``lint`` (ruff),
    ``types`` (mypy).

    Args:
        code: Complete Python source.
        allowed_packages: Comma-separated third-party package names the
            target environment has installed, e.g. ``"httpx,pandas"``. Omit
            to skip the allowlist check entirely.
        spec: The user's own words for what the workflow should do. Without
            it ``coverage`` and ``resolution`` are skipped — they compare the
            code against the request, and there is nothing to compare against.
            Both catch defects that pass every other stage: a fetch capped at
            100 answering a spec that said "all", and a query built by
            fuzzy-matching a word the spec supplied instead of resolving it.

    Returns JSON: ``{"valid": bool, "issues": [{"category", "severity",
    "message"}, ...], "stages": [{"name", "status", "reason"}, ...]}``.
    ``valid`` is false only when an issue's severity is ``"error"`` — warnings
    do not block. A stage's ``status`` is one of ``ok``, ``failed``,
    ``skipped`` (its tool is absent, or it had nothing to judge against), or
    ``not_run`` (an earlier blocking stage failed).
    """
    from loom.agents.checks import CheckContext

    packages = (
        {p.strip() for p in allowed_packages.split(",") if p.strip()}
        if allowed_packages
        else None
    )
    # An empty registry checks nothing — the same rule grant validation
    # follows. Toolsets load lazily and through entry points, so "none
    # registered" says nothing about what the target environment has, and
    # passing an empty set would make the validator reject every
    # ``loom.toolsets.*`` import in the file.
    available = set(_catalog().list_toolsets()) or None

    context = CheckContext(
        allowed_packages=packages,
        available_toolsets=available,
        toolset_modules=_toolset_modules(),
        spec=spec,
    )
    report = await _static_pipeline().run(code, context)

    payload: dict[str, Any] = {
        "valid": report.ok,
        "issues": [
            {"category": i.category, "severity": i.severity, "message": i.message}
            for i in report.issues
        ],
        "stages": _stage_rows(report, spec),
    }
    if not spec.strip():
        payload["note"] = (
            "coverage and resolution were skipped. Pass spec= (the user's own "
            "words for what this workflow should do) so they can check the "
            "code against the request, not just against the language."
        )
    return _json(payload)


def _stage_rows(report: Any, spec: str) -> list[dict[str, Any]]:
    """One row per stage, saying what it actually did.

    A stage that could not run has found nothing, and reporting that as a pass
    is how a client concludes its code cleared seven checks when it cleared
    five. ``not_run`` is the same honesty applied to the pipeline's own
    short-circuit: everything after a blocking failure would report only that
    failure's consequences, so it was never asked.
    """
    rows: list[dict[str, Any]] = []
    for name in STATIC_STAGE_NAMES:
        result = report.result(name)
        if result is None:
            rows.append(
                {
                    "name": name,
                    "status": "not_run",
                    "reason": "an earlier blocking stage failed; this would "
                    "only report its consequences",
                }
            )
        elif result.skipped:
            # ``GrantStage`` states its reason in ``skipped=`` rather than
            # ``reason=``; read whichever carried it, because a skip with no
            # stated reason is indistinguishable from a pass to anyone
            # skimming.
            reason = result.reason or (
                result.skipped if isinstance(result.skipped, str) else ""
            )
            rows.append({"name": name, "status": "skipped", "reason": reason})
        elif name in _SPEC_STAGES and not spec.strip():
            rows.append(
                {
                    "name": name,
                    "status": "skipped",
                    "reason": "no spec was passed, so there was nothing to "
                    "judge the code's intent against",
                }
            )
        else:
            rows.append(
                {"name": name, "status": "failed" if result.errors else "ok"}
            )
    return rows


async def smoke_test_workflow(
    code: str,
    workflow_input_json: str = "null",
    *,
    timeout: float = 30.0,
) -> str:
    """Run workflow code once in a sandboxed subprocess. Does not touch a
    real network or credential.

    The subprocess uses ``MemoryStore`` and a mock model provider, with every
    registered toolset's operations replaced by schema-generated fakes — the
    same sandbox ``WorkflowCodingAgent`` smoke-tests generated code in. A
    failure here is either a real bug (missing import, wrong step arity, a
    workflow body touching ``ctx`` incorrectly) or, per the ``environmental``
    flag, an artifact of the sandbox having no credentials — the latter is
    not something to fix in the code.

    Args:
        code: Complete Python source; must contain an ``@workflow`` function.
        workflow_input_json: Input for the workflow, JSON-encoded. ``"null"``
            (default) derives one from the workflow's declared input type.
        timeout: Seconds before the subprocess is killed and the run reported
            as failed (not a hang in this tool — a bounded wait).

    On a run that completes, it then runs the code *twice more* and compares
    the two outputs — the ``replay`` key. Nondeterminism is the one defect
    class a single run cannot see: a body reading ``datetime.now()`` or
    iterating a set passes once and diverges on the replay the engine performs
    after every crash, park, or retry. Reported separately from ``ok`` because
    it is a different question, and skipped when the first run did not
    complete, since there is then nothing to compare.

    Returns JSON: ``ok``, ``phase`` (``compile``/``import``/``run``/``done``),
    ``error``, ``traceback``, ``steps_executed``, ``output_preview``,
    ``workflows_found``, ``status``, ``environmental``, ``replay``.
    """
    import asyncio

    from loom.agents.smoke import smoke_run

    try:
        workflow_input = json.loads(workflow_input_json) if workflow_input_json else None
    except json.JSONDecodeError as exc:
        return _json({"error": f"workflow_input_json is not valid JSON: {exc}"})

    fakes = _fakes_for_registered_toolsets()

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: smoke_run(code, workflow_input, timeout=timeout, fakes=fakes),
    )
    return _json(
        {
            "ok": result.ok,
            "phase": result.phase,
            "error": result.error,
            "traceback": result.traceback[:2000] if result.traceback else "",
            "steps_executed": result.steps_executed,
            "output_preview": result.output_preview[:1000],
            "workflows_found": result.workflows_found,
            "status": result.status,
            "environmental": result.environmental,
            "replay": await _replay_report(
                code, workflow_input, fakes, timeout, result
            ),
        }
    )


async def _replay_report(
    code: str,
    workflow_input: Any,
    fakes: list[tuple[str, str]],
    timeout: float,
    smoked: Any,
) -> dict[str, Any]:
    """Did two runs of the same code produce the same output?

    Reuses ``ReplayStage`` rather than comparing two ``smoke_run`` calls here,
    so the determinism message a client repairs against is written in exactly
    one place. Best-effort throughout: a run that never completed is reported
    as ``skipped``, never as ``ok``, because claiming a determinism check that
    did not happen is the failure this whole payload shape exists to avoid.
    """
    if not smoked.ok:
        return {
            "status": "skipped",
            "reason": "the run did not complete, so there was no output for a "
            "second run to be compared against",
        }

    from loom.agents.checks import CheckContext
    from loom.agents.stages import ReplayStage

    result = await ReplayStage().run(
        code,
        CheckContext(workflow_input=workflow_input, fakes=fakes, timeout=timeout),
    )
    if result.skipped:
        return {"status": "skipped", "reason": result.reason}
    if result.errors:
        return {
            "status": "failed",
            "issues": [
                {"category": i.category, "severity": i.severity, "message": i.message}
                for i in result.issues
            ],
        }
    return {"status": "ok"}


async def save_workflow(code: str, path: str) -> str:
    """Write generated workflow code to a file.

    Refuses an absolute path, a ``..`` component, or a non-``.py`` extension
    — this tool writes to the host's filesystem, and those are exactly the
    ways a path escapes the project it was meant to land in. Compile-checks
    before writing, so a saved file is at least syntactically valid.

    Args:
        code: Complete Python source.
        path: Relative file path, e.g. ``"flows/overdue_tickets.py"``. Parent
            directories are created if missing.

    Returns JSON: ``{"saved": bool, "path": str, "workflows_found": [...]}``
    on success, or ``{"error": ...}``. ``workflows_found`` is best-effort —
    the code is imported once to look for ``@workflow`` functions, and an
    import failure there still leaves the file saved.
    """
    from pathlib import Path, PurePosixPath

    from loom.agents.smoke import compile_check

    posix = PurePosixPath(path)
    if posix.is_absolute():
        return _json({"error": "path must be relative, not absolute"})
    if ".." in posix.parts:
        return _json({"error": "path must not contain '..'"})
    if posix.suffix != ".py":
        return _json({"error": f"path must end in '.py', got '{posix.suffix}'"})

    compiled = compile_check(code)
    if not compiled.ok:
        return _json(
            {"error": f"code does not compile: {compiled.error}", "saved": False}
        )

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code, encoding="utf-8")

    return _json(
        {
            "saved": True,
            "path": str(target),
            "workflows_found": _find_workflow_names(code),
        }
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _toolset_modules() -> dict[str, str]:
    """Toolset id to its real importable module, for the validator's import
    check.

    Mirrors ``WorkflowCodingAgent._toolset_modules`` — a toolset's id and its
    module are not the same string (``google_calendar`` lives at
    ``loom.toolsets.google.calendar``), so the check needs the
    real path, not a name-based guess.
    """
    catalog = _catalog()
    modules: dict[str, str] = {}
    for toolset_id in catalog.list_toolsets():
        manifest = catalog.get(toolset_id)
        module = getattr(manifest, "tools_module", "") if manifest is not None else ""
        if module:
            modules[toolset_id] = module
    return modules


def _fakes_for_registered_toolsets() -> list[tuple[str, str]]:
    """``(tools_module, manifest_import_path)`` pairs for every registered
    toolset, for ``smoke_run``'s ``fakes=``.

    Mirrors ``WorkflowCodingAgent._check_context`` — ``smoke_run`` does not
    discover fakes on its own; the caller must resolve each manifest's own
    import path so the subprocess can re-import it.
    """
    from loom.agents.coding_agent import _manifest_path

    catalog = _catalog()
    fakes: list[tuple[str, str]] = []
    for toolset_id in catalog.list_toolsets():
        manifest = catalog.get(toolset_id)
        if manifest is None:
            continue
        tools_module = getattr(manifest, "tools_module", "")
        if not tools_module:
            continue
        manifest_path = _manifest_path(manifest)
        if manifest_path:
            fakes.append((tools_module, manifest_path))
    return fakes


def _find_workflow_names(code: str) -> list[str]:
    """Names of every ``@workflow`` function in *code*, best-effort.

    Imports *code* into a throwaway module — the same approach
    ``CodingResult.load()`` takes — rather than parsing the AST for a
    decorator name, since the true test of "is this a workflow" is the
    object the decorator produces, not what the source calls it.
    """
    import importlib.util
    import tempfile

    from loom.runtime.workflow import WorkflowDefinition

    names: list[str] = []
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False
        ) as tmp:
            tmp.write(code)
            tmp.flush()
            spec = importlib.util.spec_from_file_location("_loom_saved_workflow", tmp.name)
            if spec is None or spec.loader is None:
                return names
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        for value in vars(module).values():
            if isinstance(value, WorkflowDefinition):
                names.append(value.name)
    except Exception:
        pass  # best-effort — a failed import still leaves the file saved
    return names
