"""Event routing — pub/sub fan-out for triggers and mid-flow waits.

The ``EventRouter`` connects two worlds:

1. **Trigger subscriptions** — workflows that start on a named event.
   A matching event creates a new run.
2. **Mid-flow waits** — runs parked on ``ctx.wait_for_event(name)``.
   A matching event resumes the run with the event payload.

Filter matching via :class:`FilterSpec` is applied before fan-out.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loom.triggers.filter import FilterSpec

logger = logging.getLogger("workflow.routing")


@dataclass(frozen=True, slots=True)
class RoutingEvent:
    """An event flowing through the router."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    key: str = ""
    """Correlation key for directed mid-flow waits."""
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )
    """When the event arrived.

    A ``default_factory`` cannot reach a ``Clock``, so a caller inside a
    Runtime on virtual time must pass ``timestamp=rt.clock.now()`` — otherwise
    this one field lands on the wall clock while every other timestamp on the
    run it triggers lands on the test's timeline, and comparing them reads as
    an event that arrived before the system existed."""


@dataclass
class Subscription:
    """A workflow subscribing to events of a given name."""

    event_name: str
    workflow_name: str
    filter: FilterSpec | None = None
    """Optional filter applied before creating a run."""


class EventRouter:
    """Routes events to trigger subscriptions and waiting runs.

    This is the in-process router for embedded mode.  A distributed
    implementation (Phase 5) would use a message broker.
    """

    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []
        self._routed: list[tuple[RoutingEvent, list[str]]] = []

    def subscribe(
        self,
        event_name: str,
        workflow_name: str,
        *,
        filter: FilterSpec | None = None,
    ) -> Subscription:
        """Register a workflow as a subscriber for an event name."""
        sub = Subscription(
            event_name=event_name,
            workflow_name=workflow_name,
            filter=filter,
        )
        self._subscriptions.append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a subscription."""
        with contextlib.suppress(ValueError):
            self._subscriptions.remove(subscription)

    def subscriptions_for(self, event_name: str) -> list[Subscription]:
        """Return all subscriptions matching an event name."""
        return [s for s in self._subscriptions if s.event_name == event_name]

    async def route(self, event: RoutingEvent) -> list[str]:
        """Route an event.  Returns list of workflow names that matched.

        In the full implementation this would create runs via the Runtime
        and resume waiting runs via the ExecutionStore.  This base
        implementation handles subscription matching and filtering;
        subclasses or integration code handle the actual run creation.
        """
        matched: list[str] = []

        for sub in self._subscriptions:
            if sub.event_name != event.name:
                continue
            if sub.filter and not sub.filter.matches(event.payload):
                logger.debug(
                    "Event '%s' filtered out for workflow '%s'",
                    event.name,
                    sub.workflow_name,
                )
                continue
            matched.append(sub.workflow_name)
            logger.info(
                "Event '%s' matched subscription for workflow '%s'",
                event.name,
                sub.workflow_name,
            )

        self._routed.append((event, matched))
        return matched

    @property
    def routed_events(self) -> list[tuple[RoutingEvent, list[str]]]:
        """History of routed events (for testing)."""
        return list(self._routed)

    def clear_history(self) -> None:
        """Clear routed event history."""
        self._routed.clear()
