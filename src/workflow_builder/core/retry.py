"""Retry policies and failure-handling modes."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from workflow_builder.core.exceptions import (
    AuthExpired,
    GuardrailTripwire,
    NonRetryableError,
    ValidationError,
)
from workflow_builder.core.types import Duration, to_seconds

RetryPredicate = Callable[[BaseException], bool]

#: Failures that are pointless to retry: they will fail identically every time.
#: AuthExpired included deliberately — retrying within a step's backoff window
#: (seconds) cannot produce the human reauthorization the credential needs, so
#: retrying only delays the moment the run parks. Excluded, it would surface
#: to the engine wrapped in RetriesExhausted instead of bare, and the engine's
#: AuthExpired -> Suspend mapping would never see it.
PERMANENT_ERRORS: tuple[type[BaseException], ...] = (
    NonRetryableError,
    ValidationError,
    GuardrailTripwire,
    AuthExpired,
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    NotImplementedError,
)


class OnError(StrEnum):
    """What the engine does once a step has exhausted its retries.

    Mirrors n8n's per-node error modes, which are the single most-used reliability knob
    in that product, but as typed values rather than dropdown strings.
    """

    RAISE = "raise"
    """Fail the workflow (default)."""

    CONTINUE = "continue"
    """Swallow the error and return ``fallback`` (default ``None``)."""

    ROUTE = "route"
    """Return a ``Failure`` result so orchestration code can branch on the error."""


@dataclass(frozen=True, slots=True)
class Retry:
    """Exponential backoff with full jitter.

    ``retry_on`` accepts exception classes or a predicate. Regardless of what it allows,
    :data:`PERMANENT_ERRORS` are never retried unless ``retry_on`` names them explicitly.
    """

    max_attempts: int = 3
    initial_delay: Duration = 0.5
    max_delay: Duration = 30.0
    multiplier: float = 2.0
    jitter: bool = True
    retry_on: Sequence[type[BaseException]] | RetryPredicate | None = None
    non_retryable: Sequence[type[BaseException]] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

    def should_retry(self, error: BaseException, attempt: int) -> bool:
        """Decide whether ``error`` on ``attempt`` (1-based) warrants another try."""
        if attempt >= self.max_attempts:
            return False
        if self.non_retryable and isinstance(error, tuple(self.non_retryable)):
            return False

        if self.retry_on is None:
            return not isinstance(error, PERMANENT_ERRORS)
        if callable(self.retry_on) and not isinstance(self.retry_on, type):
            return bool(self.retry_on(error))
        return isinstance(error, tuple(self.retry_on))  # type: ignore[arg-type]

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Delay in seconds before attempt ``attempt + 1`` (``attempt`` is 1-based)."""
        base = to_seconds(self.initial_delay) * (self.multiplier ** max(0, attempt - 1))
        capped = min(base, to_seconds(self.max_delay))
        if not self.jitter:
            return capped
        return (rng or random).uniform(0.0, capped)


#: Try exactly once; surface the first failure.
NO_RETRY = Retry(max_attempts=1)

#: Sensible default for network-bound side effects.
DEFAULT_RETRY = Retry(max_attempts=3, initial_delay=0.5, max_delay=30.0)


@dataclass(frozen=True, slots=True)
class Failure:
    """Error branch value produced by steps configured with :attr:`OnError.ROUTE`.

    This is the code-first answer to n8n's "continue using error output" edge: instead of
    a second wire on the canvas, orchestration code gets a value it can pattern match on.
    """

    step: str
    error_type: str
    message: str
    attempts: int

    def __bool__(self) -> bool:
        return False
