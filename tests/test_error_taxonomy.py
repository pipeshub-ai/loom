"""Tests for the exception hierarchy."""

from __future__ import annotations

from loom.core.exceptions import (
    AgentError,
    ApprovalRejected,
    AuthExpired,
    BackendCapabilityError,
    BudgetExceeded,
    ConfigurationError,
    ContractChanged,
    ControlSignal,
    GrantDenied,
    GuardrailTripwire,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRetry,
    NondeterminismError,
    OutputValidationError,
    RegistryError,
    ResourceUnavailable,
    RetriesExhausted,
    SerializationError,
    SessionExhausted,
    StepError,
    Suspend,
    ToolNotFound,
    UsageLimitExceeded,
    ValidationError,
    WorkflowCancelled,
    WorkflowError,
)


class TestHierarchy:
    def test_workflow_error_is_exception(self) -> None:
        assert issubclass(WorkflowError, Exception)

    def test_control_signal_is_base_exception(self) -> None:
        assert issubclass(ControlSignal, BaseException)
        assert not issubclass(ControlSignal, Exception)

    def test_suspend_not_caught_by_except_exception(self) -> None:
        """Suspend must not be swallowed by ``except Exception`` in user code."""
        assert issubclass(Suspend, BaseException)
        assert not issubclass(Suspend, Exception)

    def test_cancelled_not_caught_by_except_exception(self) -> None:
        assert issubclass(WorkflowCancelled, BaseException)
        assert not issubclass(WorkflowCancelled, Exception)

    def test_agent_errors_are_workflow_errors(self) -> None:
        agent_errors = [
            AgentError,
            MaxTurnsExceeded,
            ModelBehaviorError,
            OutputValidationError,
            ToolNotFound,
            UsageLimitExceeded,
            GuardrailTripwire,
            ApprovalRejected,
            SessionExhausted,
        ]
        for cls in agent_errors:
            assert issubclass(cls, AgentError), f"{cls.__name__} should be AgentError"
            assert issubclass(cls, WorkflowError), f"{cls.__name__} should be WorkflowError"

    def test_step_errors_are_workflow_errors(self) -> None:
        assert issubclass(StepError, WorkflowError)
        assert issubclass(RetriesExhausted, StepError)

    def test_config_errors_are_workflow_errors(self) -> None:
        config_errors = [
            ConfigurationError,
            ValidationError,
            SerializationError,
            RegistryError,
        ]
        for cls in config_errors:
            assert issubclass(cls, WorkflowError), f"{cls.__name__} should be WorkflowError"


class TestNewExceptions:
    def test_backend_capability_error(self) -> None:
        err = BackendCapabilityError(
            "timers not supported", capability="timers", backend="mock"
        )
        assert err.capability == "timers"
        assert err.backend == "mock"
        assert isinstance(err, WorkflowError)

    def test_contract_changed(self) -> None:
        err = ContractChanged("signature changed")
        assert isinstance(err, WorkflowError)
        assert str(err) == "signature changed"

    def test_resource_unavailable(self) -> None:
        err = ResourceUnavailable("pool exhausted")
        assert isinstance(err, WorkflowError)

    def test_auth_expired(self) -> None:
        err = AuthExpired("token expired")
        assert isinstance(err, WorkflowError)

    def test_grant_denied(self) -> None:
        err = GrantDenied("denied", grant="read", required="write")
        assert err.grant == "read"
        assert err.required == "write"
        assert isinstance(err, WorkflowError)

    def test_session_exhausted(self) -> None:
        err = SessionExhausted("max turns")
        assert isinstance(err, AgentError)

    def test_budget_exceeded(self) -> None:
        err = BudgetExceeded("over", budget_type="tokens", limit=1000, actual=1500)
        assert err.budget_type == "tokens"
        assert err.limit == 1000
        assert err.actual == 1500
        assert isinstance(err, WorkflowError)


class TestExistingExceptions:
    def test_nondeterminism_error(self) -> None:
        err = NondeterminismError("diverged", seq=3, expected="step:a", actual="step:b")
        assert err.seq == 3
        assert err.expected == "step:a"
        assert err.actual == "step:b"

    def test_step_error(self) -> None:
        cause = ValueError("inner")
        err = StepError("failed", step_name="fetch", attempts=3, cause=cause)
        assert err.step_name == "fetch"
        assert err.attempts == 3
        assert err.__cause__ is cause

    def test_usage_limit_exceeded(self) -> None:
        err = UsageLimitExceeded("too many", limit_name="tokens", limit=1000, actual=2000)
        assert err.limit_name == "tokens"

    def test_guardrail_tripwire(self) -> None:
        err = GuardrailTripwire("blocked", guardrail_name="pii_filter", info={"field": "ssn"})
        assert err.guardrail_name == "pii_filter"
        assert err.info == {"field": "ssn"}

    def test_model_retry_is_exception_not_base(self) -> None:
        """ModelRetry should be catchable by except Exception."""
        assert issubclass(ModelRetry, Exception)

    def test_suspend(self) -> None:
        from datetime import UTC, datetime

        wake = datetime.now(UTC)
        s = Suspend("waiting", path="0.1", wake_at=wake, awaiting_event="payment")
        assert s.reason == "waiting"
        assert s.path == "0.1"
        assert s.wake_at is wake
        assert s.awaiting_event == "payment"
