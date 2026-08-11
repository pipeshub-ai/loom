"""OpenTelemetry adapter for the ``Tracer`` protocol.

Wraps the OTel Python SDK so the engine, steps, and agent runtime emit
proper spans without importing ``opentelemetry`` at the protocol level.
The import is deferred — if the SDK is not installed, importing this
module raises ``ImportError`` at construction time, not at import time.

GenAI semantic conventions are applied where applicable (model, tokens,
cost).
"""

from __future__ import annotations

from typing import Any

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import StatusCode as _StatusCode

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False

# Attribute mapping from LOOM names to OTel / GenAI conventions
LOOM_ATTRS: dict[str, str] = {
    "run_id": "loom.run_id",
    "flow_id": "loom.flow_id",
    "step_id": "loom.step_id",
    "step_class": "loom.step_class",
    "attempt": "loom.attempt",
    "model_provider": "gen_ai.system",
    "model": "gen_ai.request.model",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
}


def _map_attributes(attrs: dict[str, Any] | None) -> dict[str, Any]:
    """Translate short LOOM attribute names to OTel conventions."""
    if not attrs:
        return {}
    mapped: dict[str, Any] = {}
    for key, value in attrs.items():
        otel_key = LOOM_ATTRS.get(key, key)
        mapped[otel_key] = value
    return mapped


class OTelSpan:
    """Wraps an OTel ``Span`` to satisfy the ``Span`` protocol."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        otel_key = LOOM_ATTRS.get(key, key)
        self._span.set_attribute(otel_key, value)

    def set_status(self, status: str, description: str = "") -> None:
        if status == "ok":
            self._span.set_status(_StatusCode.OK, description)
        elif status == "error":
            self._span.set_status(_StatusCode.ERROR, description)
        else:
            self._span.set_status(_StatusCode.UNSET, description)

    def record_exception(self, error: BaseException) -> None:
        self._span.set_status(_StatusCode.ERROR, str(error))
        self._span.record_exception(error)

    def add_event(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> None:
        self._span.add_event(name, attributes=attributes)

    def end(self) -> None:
        self._span.end()


class OTelTracer:
    """OpenTelemetry tracer implementing the ``Tracer`` protocol.

    Raises ``RuntimeError`` at construction if ``opentelemetry`` is not
    installed.
    """

    def __init__(self, service_name: str = "loom") -> None:
        if not _HAS_OTEL:
            msg = (
                "opentelemetry SDK is not installed. "
                "Install with: pip install opentelemetry-api opentelemetry-sdk"
            )
            raise RuntimeError(msg)
        self._tracer = _otel_trace.get_tracer(service_name)

    def start_span(
        self, name: str, *, attributes: dict[str, Any] | None = None
    ) -> OTelSpan:
        mapped = _map_attributes(attributes)
        span = self._tracer.start_span(name, attributes=mapped)
        return OTelSpan(span)
