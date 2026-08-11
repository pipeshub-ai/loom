"""Tracing abstraction, shaped so an OpenTelemetry exporter drops straight in.

The SDK never imports OpenTelemetry itself. It emits spans through this protocol; an
adapter forwards them. That keeps the dependency optional while making every step, model
call, tool call, and guardrail visible in whatever backend a team already runs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Span(Protocol):
    """A unit of timed, attributed work."""

    def set_attribute(self, key: str, value: Any) -> None: ...

    def set_status(self, status: str, description: str = "") -> None: ...

    def record_exception(self, error: BaseException) -> None: ...

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None: ...

    def end(self) -> None: ...


@runtime_checkable
class Tracer(Protocol):
    def start_span(self, name: str, *, attributes: dict[str, Any] | None = None) -> Span: ...


@dataclass
class NoopSpan:
    """Zero-cost span used when tracing is disabled."""

    name: str = ""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, status: str, description: str = "") -> None:
        return None

    def record_exception(self, error: BaseException) -> None:
        return None

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        return None

    def end(self) -> None:
        return None


class NoopTracer:
    def start_span(self, name: str, *, attributes: dict[str, Any] | None = None) -> Span:
        return NoopSpan(name)


@dataclass
class RecordedSpan:
    """A span captured in memory. Useful in tests and in the local dev viewer."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    status: str = "unset"
    description: str = ""
    error: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    duration_ms: float | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: str, description: str = "") -> None:
        self.status = status
        self.description = description

    def record_exception(self, error: BaseException) -> None:
        self.status = "error"
        self.error = f"{type(error).__name__}: {error}"

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append((name, attributes or {}))

    def end(self) -> None:
        if self.duration_ms is None:
            self.duration_ms = (time.perf_counter() - self.started_at) * 1000


class InMemoryTracer:
    """Collects spans in a list so tests can assert on what actually ran."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    def start_span(self, name: str, *, attributes: dict[str, Any] | None = None) -> Span:
        span = RecordedSpan(name=name, attributes=dict(attributes or {}))
        self.spans.append(span)
        return span

    def named(self, name: str) -> list[RecordedSpan]:
        return [span for span in self.spans if span.name == name]

    def clear(self) -> None:
        self.spans.clear()


class LoggingTracer:
    """Emits one log line per span. The zero-configuration option for local runs."""

    def __init__(self, logger: logging.Logger | None = None, level: int = logging.DEBUG) -> None:
        self._logger = logger or logging.getLogger("workflow.trace")
        self._level = level

    def start_span(self, name: str, *, attributes: dict[str, Any] | None = None) -> Span:
        logger, level = self._logger, self._level

        class _LoggingSpan(RecordedSpan):
            def end(self) -> None:
                super().end()
                logger.log(
                    level,
                    "span %s %.1fms %s",
                    self.name,
                    self.duration_ms or 0,
                    self.status,
                )

        return _LoggingSpan(name=name, attributes=dict(attributes or {}))


@contextmanager
def span_scope(tracer: Tracer, name: str, **attributes: Any) -> Iterator[Span]:
    span = tracer.start_span(name, attributes=attributes)
    try:
        yield span
    except BaseException as error:
        span.record_exception(error)
        raise
    finally:
        span.end()
