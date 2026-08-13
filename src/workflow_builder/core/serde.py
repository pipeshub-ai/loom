"""Pluggable serialization for journal payloads.

Every value that crosses the durability boundary — step inputs and outputs, agent
messages, event payloads — passes through a :class:`Serializer`. The default
implementation understands Pydantic models, dataclasses, and the usual scalar zoo, and
can rehydrate a value back into a declared type when a type hint is available.
"""

from __future__ import annotations

import base64
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
            return to_jsonable_python(
                _wrap_bytes(value),
                serialize_unknown=False,
                fallback=_fallback,
                # Backstop for bytes inside a typed model, where the declared
                # field type is enough to decode base64 back on the way in.
                bytes_mode="base64",
            )
        except Exception as exc:
            raise SerializationError(
                f"cannot journal a value of type {type(value).__name__}: {exc}. "
                "Return a Pydantic model, dataclass, or plain JSON-compatible value from steps."
            ) from exc

    def decode(self, data: Any, type_: Any | None = None) -> Any:
        data = _revive(data)
        if type_ is None or type_ is Any or data is None:
            return data
        try:
            return TypeAdapter(type_).validate_python(data)
        except Exception:
            # A declared type that no longer matches the journal (for example after a
            # refactor) should not destroy an in-flight run; hand back the raw payload.
            return data


#: Marker for binary data encoded into a JSON journal payload.
BYTES_KEY = "__bytes_b64__"


def _wrap_bytes(value: Any) -> Any:
    """Tag loose binary as base64 markers before Pydantic sees it.

    Pydantic serializes ``bytes`` by decoding as UTF-8, which raises on genuinely
    binary content, and even in base64 mode produces a bare string that cannot be
    told from a real one on the way back. Where there is no declared type to guide
    decoding — a raw ``bytes`` return, or bytes inside a plain dict — the marker
    is what makes the round trip lossless. Typed model fields need none of this
    and are left for Pydantic to handle.
    """
    if isinstance(value, bytes | bytearray | memoryview):
        return {BYTES_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {k: _wrap_bytes(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_wrap_bytes(item) for item in value]
    return value


def _fallback(value: Any) -> Any:
    # A type that declares its own Pydantic schema knows how to round-trip; honour
    # it rather than falling through to the generic cases below. ``to_jsonable_python``
    # is duck-typed and never builds a schema for the value, so it never asks.
    if hasattr(type(value), "__get_pydantic_core_schema__"):
        return TypeAdapter(type(value)).dump_python(value, mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    if isinstance(value, set | frozenset):
        return list(value)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    raise TypeError(f"unserializable type: {type(value).__name__}")


def _revive(data: Any) -> Any:
    """Turn encoded binary markers back into ``bytes``, recursively."""
    if isinstance(data, dict):
        if set(data) == {BYTES_KEY}:
            return base64.b64decode(data[BYTES_KEY])
        return {k: _revive(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_revive(item) for item in data]
    return data


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


def resolve_annotations(fn: Any) -> dict[str, Any]:
    """Return a function's annotations as real types, not strings.

    ``from __future__ import annotations`` makes every annotation a string. Left
    unresolved, a step declaring ``-> Invoice`` records the *string* ``"Invoice"``
    as its output type, and replay hands the workflow a raw dict instead of an
    ``Invoice`` — silently, and only on the second attempt.

    Falls back to the raw ``__annotations__`` when a name cannot be resolved
    (a ``TYPE_CHECKING``-only import, typically), since a partially-typed
    signature is better than none.
    """
    import typing

    try:
        return typing.get_type_hints(fn)
    except Exception:
        return dict(getattr(fn, "__annotations__", {}))
