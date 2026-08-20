"""What can go wrong driving a page, as types a workflow can branch on.

Types rather than messages, because a replay reconstructs recorded failures by
type and a workflow that says ``except AmbiguousTarget`` is doing something
different from one that says ``except ActionFailed``. Losing the type sends a
replay down a different branch from the run it is rehearsing — the divergence
journaling exists to prevent.
"""

from __future__ import annotations

from loom.core.exceptions import WorkflowError

__all__ = [
    "ActionFailed",
    "AmbiguousTarget",
    "BrowserUnavailable",
    "SelectorDrift",
    "SessionLost",
    "TargetNotFound",
]


class BrowserUnavailable(WorkflowError):  # noqa: N818 - names the condition
    """No provider is configured, or the one configured cannot start.

    About the deployment, never about the workflow — the same separation
    ``ProbeError`` makes, and for the same reason: a model told something is
    wrong will try to fix it, and there is nothing in the code to fix.
    """


class TargetNotFound(WorkflowError):  # noqa: N818
    """Nothing on the page matched.

    The message names what was looked for *and* what the page does carry,
    because "button named 'Send'" and "no button on this page has any
    accessible name" call for entirely different repairs.
    """

    def __init__(self, message: str, *, target: str = "", snapshot: str = "") -> None:
        super().__init__(message)
        self.target = target
        self.snapshot = snapshot


class AmbiguousTarget(WorkflowError):  # noqa: N818
    """Several controls matched, so none was chosen.

    **Refusing is the feature.** Picking the first match is how an automation
    clicks the wrong button and reports success — the failure this whole design
    keeps naming. A caller that knows the page repeats a control says so with
    ``Target.ordinal``; a caller that does not should find out here rather than
    in the confirmation email.
    """

    def __init__(self, message: str, *, target: str = "", matches: int = 0) -> None:
        super().__init__(message)
        self.target = target
        self.matches = matches


class ActionFailed(WorkflowError):  # noqa: N818
    """The control was found and would not take the action.

    Disabled, covered by an overlay, detached mid-click. Distinct from
    :class:`TargetNotFound` because the page *has* what was asked for, so the
    repair is about timing or state rather than about the target.
    """

    def __init__(self, message: str, *, target: str = "", url: str = "") -> None:
        super().__init__(message)
        self.target = target
        self.url = url


class SessionLost(WorkflowError):  # noqa: N818
    """A session that should have survived did not.

    Raised by ``reattach``. A workflow branches on this to decide between
    starting over and giving up, which is why it is its own type and not a bare
    ``ActionFailed``.
    """


class SelectorDrift(WorkflowError):  # noqa: N818
    """A journaled plan no longer matches the page it was recorded against.

    Reserved for 13.2, where drift is repaired silently for a read and refused
    for a write. Defined here so the taxonomy is complete in one place rather
    than growing a type per phase.
    """

    def __init__(self, message: str, *, recorded: str = "", found: str = "") -> None:
        super().__init__(message)
        self.recorded = recorded
        self.found = found
