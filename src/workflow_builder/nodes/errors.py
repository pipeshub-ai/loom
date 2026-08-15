"""Failures specific to nodes.

Every one of these exists because the alternative is a silent degradation. A
node id that does not resolve, a payload that does not fit, a capability that
was never configured — each has a plausible-looking wrong behaviour available
(skip it, coerce it, park forever) and each raises instead.
"""

from __future__ import annotations

from workflow_builder.core.exceptions import (
    ConfigurationError,
    ValidationError,
    WorkflowError,
)

__all__ = [
    "GuardrailRejected",
    "HumanChannelMissing",
    "HumanRequestExpired",
    "NodeContractError",
    "NodeNotFound",
]


class NodeNotFound(WorkflowError):  # noqa: N818 - a lookup failure, not a "-Error" state
    """No node is registered under this id.

    Carries near matches, because a wrong id is the single most likely failure
    when a model writes a node call and the fix is almost always one character
    away.
    """

    def __init__(self, node_id: str, *, suggestions: list[str] | None = None) -> None:
        self.node_id = node_id
        self.suggestions = suggestions or []
        message = f"no node {node_id!r} is registered"
        if self.suggestions:
            message += f" — did you mean {', '.join(repr(s) for s in self.suggestions)}?"
        super().__init__(message)


class NodeContractError(ValidationError):
    """A node's declared contract and reality disagree.

    Raised at registration when the class is malformed, and on replay when a
    journalled entry was produced by a different version of the contract than
    the one now installed. The second case is the load-bearing one: decoding an
    old payload into a new model would let a node upgrade quietly change what an
    old run replays to.
    """


class GuardrailRejected(WorkflowError):  # noqa: N818 - names the verdict, not a state
    """A guardrail returned REJECT outside an agent loop.

    In an agent loop REJECT hands the model an explanation so it can adapt. In a
    workflow body there is nobody to adapt, so a falsy return value would simply
    be ignored by the caller and the guarded work would proceed. It raises.
    """

    def __init__(self, guard: str, message: str, *, info: object = None) -> None:
        self.guard = guard
        self.info = info
        super().__init__(f"guardrail {guard!r} rejected the call: {message}")


class HumanChannelMissing(ConfigurationError):  # noqa: N818 - names the absence
    """A ``human.*`` node was reached on a Runtime with no ``HumanChannel``.

    Raised *before* the run parks. A run parked with nobody listening is the
    worst available outcome here, because it is indistinguishable from patience
    — it looks like the workflow is waiting rather than that it is stuck.
    """

    def __init__(self, node_id: str) -> None:
        super().__init__(
            f"{node_id} needs somewhere to deliver the request, and this Runtime "
            "has no human channel. Pass Runtime(human=...) — LogChannel() records "
            "requests without delivering them, ConsoleChannel() prompts on stdin, "
            "WebhookChannel(url) posts them to your own service."
        )


class HumanRequestExpired(WorkflowError):  # noqa: N818 - names the event
    """A response arrived after the request's deadline had already been decided.

    The timeout decision stands. Accepting a late answer would mean the run's
    outcome depends on delivery timing, which is exactly what the journal exists
    to remove.
    """
