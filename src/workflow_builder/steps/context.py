"""The lightweight context handed to step bodies.

Steps are leaves: they perform side effects and must not orchestrate. They therefore get
a much smaller surface than :class:`~workflow_builder.runtime.context.Context` — enough to
log, read credentials, see which attempt they are on, and cooperate with cancellation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflow_builder.connectors.credentials import CredentialStore
    from workflow_builder.observability.tracing import Span


@dataclass
class StepContext:
    """Runtime information available inside a step body."""

    run_id: str
    workflow: str
    step_name: str
    path: str
    """Journal path of this step, e.g. ``"4"`` or ``"4.2"`` inside an agent loop."""
    attempt: int = 1
    """1-based attempt counter; useful for adaptive backoff or logging."""
    max_attempts: int = 1
    deps: Any = None
    """The dependency object passed to the run, for injecting clients and config."""
    idempotency_key: str | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("workflow"))
    credentials: CredentialStore | None = None
    span: Span | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_retry(self) -> bool:
        return self.attempt > 1

    @property
    def is_final_attempt(self) -> bool:
        return self.attempt >= self.max_attempts

    async def credential(self, name: str) -> Any:
        """Resolve a named credential, refreshing OAuth tokens when needed."""
        from workflow_builder.core.exceptions import CredentialNotFound

        if self.credentials is None:
            raise CredentialNotFound(f"no credential store configured (requested '{name}')")
        return await self.credentials.get(name)
