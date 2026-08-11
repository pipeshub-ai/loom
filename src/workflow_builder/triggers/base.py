"""Trigger declarations.

A trigger is a *declarative spec* attached to a workflow, not executable machinery. The
spec describes how the outside world reaches the workflow; a host (the dev server, a
worker fleet, a serverless adapter) reads the specs and wires up the actual listeners.
Keeping them declarative means the same workflow file runs unchanged locally, in a queue
worker, and behind a load balancer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from workflow_builder.core.models import TriggerKind
from workflow_builder.core.types import JSONDict


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    """A single occurrence delivered by a trigger, before workflow input decoding."""

    kind: TriggerKind
    payload: Any
    trigger_name: str = ""
    headers: JSONDict = field(default_factory=dict)
    received_at: datetime | None = None
    idempotency_key: str | None = None
    """When present, the runtime deduplicates: the same key never starts two runs."""
    reply: Any = None


class TriggerSpec(ABC):
    """Base class for every trigger declaration."""

    kind: TriggerKind

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, unique within a workflow."""

    def describe(self) -> JSONDict:
        """Serializable description, used by hosts, the CLI, and deployment manifests."""
        return {"kind": self.kind.value, "name": self.name}

    def decode(self, event: TriggerEvent, input_type: Any = None) -> Any:
        """Turn a raw occurrence into the workflow's declared input type."""
        from workflow_builder.core.serde import decode as _decode

        return _decode(event.payload, input_type)

    def idempotency_key_for(self, event: TriggerEvent) -> str | None:
        return event.idempotency_key


@dataclass(frozen=True, slots=True)
class TriggerBinding:
    """A trigger spec paired with the workflow it starts."""

    spec: TriggerSpec
    workflow_name: str

    def describe(self) -> JSONDict:
        return {**self.spec.describe(), "workflow": self.workflow_name}
