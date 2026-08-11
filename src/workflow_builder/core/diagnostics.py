"""Actionable error diagnostics for the LOOM SDK.

Maps machine-readable codes to human-friendly messages with concrete fix
suggestions.  Every runtime, agent, and determinism error that the SDK can
detect has a stable ``LOOM-XXXX`` code so that docs, logs, and IDE
integrations can link directly to the right guidance.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A single diagnostic entry.

    Attributes:
        code: Stable identifier, e.g. ``"LOOM-D001"``.
        message: One-line description of the problem.
        fix: Concrete remediation the developer should apply.
        location: Optional source location hint (file:line).
        docs_url: Optional link to extended documentation.
    """

    code: str
    message: str
    fix: str
    location: str = ""
    docs_url: str = ""


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

DIAGNOSTICS: dict[str, Diagnostic] = {
    # --- Determinism violations (D = determinism) ---
    "LOOM-D001": Diagnostic(
        code="LOOM-D001",
        message=(
            "datetime.now() called inside a workflow body. "
            "This breaks deterministic replay."
        ),
        fix="Replace datetime.now() with ctx.now().",
    ),
    "LOOM-D002": Diagnostic(
        code="LOOM-D002",
        message=(
            "uuid.uuid4() called inside a workflow body. "
            "This breaks deterministic replay."
        ),
        fix="Replace uuid.uuid4() with ctx.uuid4().",
    ),
    "LOOM-D003": Diagnostic(
        code="LOOM-D003",
        message=(
            "random.* called inside a workflow body. "
            "This breaks deterministic replay."
        ),
        fix="Replace random.* calls with ctx.random().",
    ),
    "LOOM-D004": Diagnostic(
        code="LOOM-D004",
        message=(
            "Direct I/O (file, network, subprocess) detected "
            "in a workflow body."
        ),
        fix="Wrap the I/O operation in a @step function.",
    ),
    "LOOM-D005": Diagnostic(
        code="LOOM-D005",
        message="Step function is not async.",
        fix="Add the 'async' keyword to the step function.",
    ),
    # --- Engine / runtime errors (E = engine) ---
    "LOOM-E001": Diagnostic(
        code="LOOM-E001",
        message="Store connection failed.",
        fix=(
            "Check the store URL and ensure the database "
            "is running and accessible."
        ),
    ),
    "LOOM-E002": Diagnostic(
        code="LOOM-E002",
        message="Workflow not found in registry.",
        fix=(
            "Ensure the function is decorated with @workflow "
            "and imported before the runtime starts."
        ),
    ),
    "LOOM-E003": Diagnostic(
        code="LOOM-E003",
        message="Agent model provider returned an error.",
        fix=(
            "Check the API key / environment variable for "
            "the configured model provider."
        ),
    ),
    # --- Agent errors (A = agent) ---
    "LOOM-A001": Diagnostic(
        code="LOOM-A001",
        message="Agent budget exceeded.",
        fix=(
            "Increase the budget via the 'budget' parameter "
            "on the agent or workflow."
        ),
    ),
}

# ------------------------------------------------------------------
# Public helpers
# ------------------------------------------------------------------


def lookup_diagnostic(code: str) -> Diagnostic | None:
    """Return the ``Diagnostic`` for *code*, or ``None`` if unknown."""
    return DIAGNOSTICS.get(code)


def format_error(code: str, **context: object) -> str:
    """Format a diagnostic for display, substituting *context* values.

    Returns a multi-line string that includes the code, message, and
    fix.  Any extra *context* keyword arguments are appended as
    ``key=value`` lines so the developer sees relevant runtime data.

    If *code* is not in the registry the function still returns a
    useful string rather than raising.
    """
    diag = lookup_diagnostic(code)
    if diag is None:
        detail = ", ".join(
            f"{k}={v}" for k, v in context.items()
        )
        return f"[{code}] Unknown diagnostic.{' ' + detail if detail else ''}"

    lines: list[str] = [
        f"[{diag.code}] {diag.message}",
        f"  Fix: {diag.fix}",
    ]
    if diag.location:
        lines.append(f"  Location: {diag.location}")
    if diag.docs_url:
        lines.append(f"  Docs: {diag.docs_url}")
    for key, value in context.items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)


__all__ = [
    "DIAGNOSTICS",
    "Diagnostic",
    "format_error",
    "lookup_diagnostic",
]
