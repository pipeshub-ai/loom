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

from loom.core.exceptions import SerializationError, ValidationError

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
        if isinstance(data, SelfEncoding):
            # It came back from its own envelope, so it already knows things
            # the declared type cannot express. Let it apply the type itself.
            return data.__retype__(type_)
        try:
            return TypeAdapter(type_).validate_python(data)
        except Exception as exc:
            # A declared type that no longer matches the journal (for example after a
            # refactor) should not destroy an in-flight run; hand back the raw payload.
            #
            # But say so. Silently downgrading means the workflow receives a dict
            # where it declared a model and fails several lines later at an
            # attribute access, which reads as a bug in the workflow rather than
            # drift in the journal. The reason is recorded against the value's
            # identity so a caller that wants to react — the engine's replay
            # verification, a diagnostic — can ask, and one that does not is
            # unaffected.
            _record_drift(data, type_, exc)
            return data


#: Marker for binary data encoded into a JSON journal payload.
BYTES_KEY = "__bytes_b64__"

#: Marker for a value that describes its own wire form. See :class:`SelfEncoding`.
WIRE_KEY = "__wire__"


@runtime_checkable
class SelfEncoding(Protocol):
    """A type that knows how it survives a journal.

    Pydantic renders a value by its *structure*, which is right until the
    structure is not the whole value. A list subclass carrying "did I see
    everything?" journals as a list and loses the answer — silently, and only
    on the read side, which is the worst place to find out.

    Implementing this is the escape hatch, and it is a protocol rather than a
    branch in the serializer so that adding the next such type touches no code
    here. The contract is narrow on purpose: a dict of JSON-compatible values
    out, the same dict back in, and nothing about how it is stored.
    """

    def __wire__(self) -> dict[str, Any]:
        """The value as plain data, excluding the marker."""
        ...

    @classmethod
    def __from_wire__(cls, payload: dict[str, Any]) -> Any:
        """Rebuild from :meth:`__wire__`'s output."""
        ...

    def __retype__(self, type_: Any) -> Any:
        """A copy with contents coerced to *type_*, keeping what only I know.

        Decoding otherwise has to choose between the declared type and the
        envelope: validating through pydantic coerces the contents and discards
        the metadata, while skipping validation keeps the metadata and hands
        back raw dicts where models were declared. This is how a value gets
        both.
        """
        ...


#: Types eligible to be revived from a wire envelope, by name.
#:
#: Decoding cannot import arbitrary classes named in a payload — that is how a
#: journal becomes an execution vector — so a type opts in by registering, and
#: an unknown name decodes to its plain data rather than raising.
_WIRE_TYPES: dict[str, type] = {}


def register_wire_type(cls: type) -> type:
    """Allow ``cls`` to be rebuilt from its envelope. Usable as a decorator."""
    _WIRE_TYPES[cls.__name__] = cls
    return cls


def _wrap_self_encoding(value: Any) -> Any:
    """Tag a self-describing value before Pydantic flattens it."""
    return {
        WIRE_KEY: {"type": type(value).__name__, "data": value.__wire__()}
    }


def _wrap_bytes(value: Any) -> Any:
    """Tag loose binary as base64 markers before Pydantic sees it.

    Pydantic serializes ``bytes`` by decoding as UTF-8, which raises on genuinely
    binary content, and even in base64 mode produces a bare string that cannot be
    told from a real one on the way back. Where there is no declared type to guide
    decoding — a raw ``bytes`` return, or bytes inside a plain dict — the marker
    is what makes the round trip lossless. Typed model fields need none of this
    and are left for Pydantic to handle.
    """
    if isinstance(value, SelfEncoding) and not isinstance(value, type):
        return _wrap_self_encoding(value)
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
        if set(data) == {WIRE_KEY}:
            envelope = data[WIRE_KEY]
            target = _WIRE_TYPES.get(envelope.get("type", ""))
            payload = _revive(envelope.get("data") or {})
            # An unregistered type decodes to its data rather than raising: a
            # journal written by a newer deployment must still be readable.
            return target.__from_wire__(payload) if target else payload
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


#: Values handed back undecoded, keyed by identity, with the reason.
#:
#: Keyed by ``id()`` and bounded, because the payload itself is often unhashable
#: and holding a reference would keep a journal payload alive for the life of
#: the process. A miss simply reports no drift, which is the honest answer for
#: a value this has forgotten.
_DRIFT: dict[int, tuple[Any, str]] = {}
_DRIFT_LIMIT = 512


def _record_drift(data: Any, type_: Any, exc: Exception) -> None:
    """Remember that *data* could not be decoded into *type_*."""
    if len(_DRIFT) >= _DRIFT_LIMIT:
        _DRIFT.clear()
    reason = str(exc).split("\n")[1].strip() if "\n" in str(exc) else str(exc)
    _DRIFT[id(data)] = (type_, f"{_name_of(type_)}: {reason}")


def drift_of(value: Any, type_: Any | None = None) -> str | None:
    """Why *value* was handed back undecoded, or ``None``.

    Args:
        value: The value :func:`decode` returned.
        type_: The type it was expected to decode into. When given, a record
            for a different type is ignored — the same object can be decoded
            against more than one contract.
    """
    found = _DRIFT.get(id(value))
    if found is None:
        return None
    recorded_type, reason = found
    if type_ is not None and recorded_type is not type_:
        return None
    return reason


def _name_of(type_: Any) -> str:
    return getattr(type_, "__name__", None) or str(type_)
