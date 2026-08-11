"""Pluggable serialization for journal payloads.

Every value that crosses the durability boundary — step inputs and outputs, agent
messages, event payloads — passes through a :class:`Serializer`. The default
implementation understands Pydantic models, dataclasses, and the usual scalar zoo, and
can rehydrate a value back into a declared type when a type hint is available.
"""

from __future__ import annotations

from dataclasses import is_dataclass
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import TypeAdapter
from pydantic_core import to_jsonable_python

from workflow_builder.core.exceptions import SerializationError

T = TypeVar("T")


@runtime_checkable
class Serializer(Protocol):
    """Converts values to and from journal-safe representations."""

    def encode(self, value: Any) -> Any:
        """Return a JSON-compatible representation of ``value``."""
        ...

    def decode(self, data: Any, type_: Any | None = None) -> Any:
        """Rebuild a value, coercing into ``type_`` when one is known."""
        ...


class JsonSerializer:
    """Default serializer, backed by Pydantic's JSON-compatible conversion."""

    def encode(self, value: Any) -> Any:
        try:
            return to_jsonable_python(value, serialize_unknown=False, fallback=_fallback)
        except Exception as exc:
            raise SerializationError(
                f"cannot journal a value of type {type(value).__name__}: {exc}. "
                "Return a Pydantic model, dataclass, or plain JSON-compatible value from steps."
            ) from exc

    def decode(self, data: Any, type_: Any | None = None) -> Any:
        if type_ is None or type_ is Any or data is None:
            return data
        try:
            return TypeAdapter(type_).validate_python(data)
        except Exception:
            # A declared type that no longer matches the journal (for example after a
            # refactor) should not destroy an in-flight run; hand back the raw payload.
            return data


def _fallback(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    if isinstance(value, set | frozenset):
        return list(value)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    raise TypeError(f"unserializable type: {type(value).__name__}")


DEFAULT_SERIALIZER: Serializer = JsonSerializer()


def encode(value: Any) -> Any:
    return DEFAULT_SERIALIZER.encode(value)


def decode(data: Any, type_: Any | None = None) -> Any:
    return DEFAULT_SERIALIZER.decode(data, type_)


def validate_as(type_: Any, value: Any) -> Any:
    """Strictly validate ``value`` against ``type_``, raising on mismatch."""
    from pydantic import ValidationError as PydanticValidationError

    from workflow_builder.core.exceptions import ValidationError

    try:
        return TypeAdapter(type_).validate_python(value)
    except PydanticValidationError as exc:
        raise ValidationError(str(exc)) from exc


def json_schema_for(type_: Any) -> dict[str, Any]:
    """Produce a JSON Schema for a type, used for tool and structured-output schemas."""
    return TypeAdapter(type_).json_schema()
