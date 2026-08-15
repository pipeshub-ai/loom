"""Exception hierarchy for the workflow SDK.

Control-flow signals (:class:`Suspend`, :class:`WorkflowCancelled`) intentionally derive
from :class:`BaseException` so that ``except Exception`` inside user workflow code cannot
swallow them and strand a durable execution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime


class WorkflowError(Exception):
    """Base class for every error raised by the SDK."""


# --------------------------------------------------------------------------------------
# Authoring / configuration
# --------------------------------------------------------------------------------------


class ConfigurationError(WorkflowError):
    """A workflow, step, agent, or trigger was declared incorrectly."""


class ValidationError(WorkflowError):
    """Input or output failed schema validation."""


class InputMismatch(ValidationError):  # noqa: N818 - names the mismatch, not an error class
    """A payload does not fit the workflow's declared input.

    Raised at the door, before a run record exists. A run that could never
    have started is not a failed run: counting it as one skews every
    reliability number, consumes an admission slot for work that cannot
    happen, and leaves something in history that ``retry()`` will fail
    identically forever. This says "you sent the wrong input" rather than
    "this workflow is broken", which is the distinction the two audiences
    need.
    """

    def __init__(self, message: str, *, workflow: str = "", path: str = "") -> None:
        super().__init__(message)
        self.workflow = workflow
        self.path = path
        """Dotted location of the offending field, when one field is at fault."""


class SerializationError(WorkflowError):
    """A value could not be journaled because it is not serializable."""


class RegistryError(WorkflowError):
    """A named workflow, step, tool, or trigger could not be resolved."""


# --------------------------------------------------------------------------------------
# Durable execution
# --------------------------------------------------------------------------------------


class NondeterminismError(WorkflowError):
    """Replay diverged from the recorded journal.

    Raised when a workflow issues a different durable operation than the one recorded at
    the same position on a previous attempt, which usually means the orchestration body
    read a clock, a random source, or mutable global state directly instead of going
    through the context.
    """

    def __init__(self, message: str, *, seq: int, expected: str, actual: str) -> None:
        super().__init__(message)
        self.seq = seq
        self.expected = expected
        self.actual = actual


class StepError(WorkflowError):
    """A step raised an exception."""

    def __init__(
        self,
        message: str,
        *,
        step_name: str,
        attempts: int = 1,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.step_name = step_name
        self.attempts = attempts
        self.__cause__ = cause


class RetriesExhausted(StepError):  # noqa: N818
    """A step exhausted its retry budget."""


class NonRetryableError(WorkflowError):
    """Marker for failures that must not be retried, regardless of the retry policy."""


class TimeoutExceeded(WorkflowError):  # noqa: N818
    """A step, agent, or workflow exceeded its configured deadline."""


class ConcurrencyLimitExceeded(WorkflowError):  # noqa: N818
    """A semaphore or queue admission limit rejected the work."""


class AdmissionRejected(WorkflowError):  # noqa: N818
    """A flow-control policy declined to start a run.

    Carries the controller's decision so a caller can tell "come back in 200ms"
    (``delay``, ``debounce``, ``batch``) apart from "this will never run"
    (``skip``), which need different handling at the trigger.
    """

    def __init__(self, message: str, *, decision: str, delay_seconds: float = 0.0) -> None:
        super().__init__(message)
        self.decision = decision
        self.delay_seconds = delay_seconds

    @property
    def retryable(self) -> bool:
        """Whether resubmitting after ``delay_seconds`` could succeed."""
        return self.decision != "skip"


class DeterminismViolation(WorkflowError):  # noqa: N818
    """Orchestration code touched a non-deterministic API directly."""


class BackendCapabilityError(WorkflowError):
    """The target durability backend does not support a requested feature."""

    def __init__(self, message: str, *, capability: str, backend: str) -> None:
        super().__init__(message)
        self.capability = capability
        self.backend = backend


class ContractChanged(WorkflowError):  # noqa: N818
    """A step's input/output contract changed between the journal recording and replay.

    The journal entry was recorded with a different type signature. Replay cannot safely
    reuse the stored output because the data shape may have changed.
    """


class ResourceUnavailable(WorkflowError):  # noqa: N818
    """A required resource (connection pool, external service, lock) is unavailable."""


# --------------------------------------------------------------------------------------
# Control-flow signals (BaseException on purpose)
# --------------------------------------------------------------------------------------


class ControlSignal(BaseException):
    """Base class for non-error control flow that unwinds a workflow."""


class Suspend(ControlSignal):
    """Raised to durably park an execution until a timer fires or an event arrives."""

    def __init__(
        self,
        reason: str,
        *,
        path: str,
        wake_at: datetime | None = None,
        awaiting_event: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.path = path
        """Journal path of the operation that parked the run."""
        self.wake_at = wake_at
        self.awaiting_event = awaiting_event


class WorkflowCancelled(ControlSignal):
    """Raised inside a workflow when cancellation has been requested."""


class ContinueAsNew(ControlSignal):
    """Raised to rotate a forever-flow: current run completes, new run starts."""

    def __init__(self, seed: Any) -> None:
        super().__init__(f"continue_as_new with seed={seed!r}")
        self.seed = seed


# --------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------


class AgentError(WorkflowError):
    """Base class for agent failures."""


class MaxTurnsExceeded(AgentError):  # noqa: N818
    """The agent loop hit its turn budget without producing a final output."""


class ModelBehaviorError(AgentError):
    """The model returned something structurally invalid (bad tool name, bad JSON)."""


class OutputValidationError(AgentError):
    """The model's final output failed validation after exhausting output retries."""


class ToolNotFound(AgentError):  # noqa: N818
    """The model called a tool that is not registered on the agent."""


class UsageLimitExceeded(AgentError):  # noqa: N818
    """A request, token, or cost budget was exhausted."""

    def __init__(self, message: str, *, limit_name: str, limit: Any, actual: Any) -> None:
        super().__init__(message)
        self.limit_name = limit_name
        self.limit = limit
        self.actual = actual


class GuardrailTripwire(AgentError):  # noqa: N818
    """A guardrail rejected the input, output, or a tool call."""

    def __init__(self, message: str, *, guardrail_name: str, info: Any = None) -> None:
        super().__init__(message)
        self.guardrail_name = guardrail_name
        self.info = info


class ModelRetry(Exception):  # noqa: N818 - deliberate: this is a signal, not a failure
    """Raised by a tool or validator to send corrective feedback back to the model.

    This is caught by the agent runner and appended to the conversation as a retry
    prompt, rather than surfacing as an execution failure.
    """


class ApprovalRejected(AgentError):  # noqa: N818
    """A human reviewer denied a tool call that the agent required."""


# --------------------------------------------------------------------------------------
# Triggers, credentials, transport
# --------------------------------------------------------------------------------------


class TriggerError(WorkflowError):
    """A trigger failed to activate or to decode its payload."""


class CredentialNotFound(WorkflowError):  # noqa: N818
    """No credential is registered under the requested name."""


class AuthExpired(WorkflowError):  # noqa: N818
    """A connection token or credential has expired. The run should park until refresh.

    ``name`` is the credential name (``CredentialStore``'s key) whose expiry
    caused this, when raised from inside a run — ``runtime/engine.py`` reads
    it to build the ``credential:<name>`` event a parked run resumes on.
    Empty when raised outside a run (the CLI's own login/connect flow, which
    catches this itself and never lets the engine see it).
    """

    def __init__(self, message: str, *, name: str = "") -> None:
        super().__init__(message)
        self.name = name


class GrantDenied(WorkflowError):  # noqa: N818
    """The operation is outside the allowed grant set — the gateway rejected the call."""

    def __init__(self, message: str, *, grant: str, required: str) -> None:
        super().__init__(message)
        self.grant = grant
        self.required = required


class InsufficientScope(WorkflowError):  # noqa: N818
    """A caller's token does not hold a scope a LOOM surface operation requires.

    Distinct from :class:`GrantDenied`: this is checked against a
    :class:`~loom.identity.principal.Principal` *before* a
    facade operation runs (start a run, cancel one, publish a workflow) —
    inbound identity, not what a running workflow may call outbound.
    ``server/app.py`` maps this to HTTP 403 with the required scope named,
    not a bare 401 — 401 makes a client retry the whole login when the
    problem is that this *particular* token was never going to be enough.
    """

    def __init__(self, message: str, *, required: str, held: list[str]) -> None:
        super().__init__(message)
        self.required = required
        self.held = held


class SessionExhausted(AgentError):  # noqa: N818
    """An agent session reached its TTL or maximum turn cap."""


class BudgetExceeded(WorkflowError):  # noqa: N818
    """A token, cost, or turn budget was exhausted at the workflow level.

    Distinct from :class:`UsageLimitExceeded` which is agent-scoped.
    """

    def __init__(self, message: str, *, budget_type: str, limit: Any, actual: Any) -> None:
        super().__init__(message)
        self.budget_type = budget_type
        self.limit = limit
        self.actual = actual


__all__ = [
    "AgentError",
    "ApprovalRejected",
    "AuthExpired",
    "BackendCapabilityError",
    "BudgetExceeded",
    "ConcurrencyLimitExceeded",
    "ConfigurationError",
    "ContinueAsNew",
    "ContractChanged",
    "ControlSignal",
    "CredentialNotFound",
    "DeterminismViolation",
    "GrantDenied",
    "GuardrailTripwire",
    "InsufficientScope",
    "MaxTurnsExceeded",
    "ModelBehaviorError",
    "ModelRetry",
    "NonRetryableError",
    "NondeterminismError",
    "OutputValidationError",
    "RegistryError",
    "ResourceUnavailable",
    "RetriesExhausted",
    "SerializationError",
    "SessionExhausted",
    "StepError",
    "Suspend",
    "TimeoutExceeded",
    "ToolNotFound",
    "TriggerError",
    "UsageLimitExceeded",
    "ValidationError",
    "WorkflowCancelled",
    "WorkflowError",
]
