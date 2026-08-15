"""A value that must never reach a journal, a log line, or an HTTP response.

Credentials pass through the same code paths as everything else a step
returns — the journal, the CLI's ``--json``, the HTTP API's response body —
and any of those would happily carry a live token if it were a plain string
or a Pydantic model. ``Secret`` exists so that leaking one is not a mistake
a reviewer has to catch by reading the diff; it is a mistake the
serializer refuses to make.

>>> from workflow_builder.core.secret import Secret
>>> token = Secret("sk-abc123")
>>> str(token)
'Secret(***)'
>>> token.reveal()
'sk-abc123'
"""

from __future__ import annotations

from typing import Generic, TypeVar

__all__ = ["Secret"]

T = TypeVar("T")


class Secret(Generic[T]):
    """Wraps a value so it prints, logs, and journals as ``Secret(***)``.

    Deliberately **not** a Pydantic model and **not** a dataclass: both are
    special-cased by :mod:`workflow_builder.core.serde`'s fallback encoder, so
    either would serialize cleanly and the token would round-trip straight
    into the journal. A plain object with no recognized shape falls through
    every case in ``_fallback`` and raises ``TypeError``, which ``encode()``
    turns into ``SerializationError`` — a step that tries to return a
    ``Secret`` fails loudly at write time instead of leaking quietly at read
    time.

    ``reveal()`` is the only way out, and it is one word to grep for across a
    codebase — the point is not that revealing is forbidden, it is that every
    place a secret becomes a plain value is visible in a search.
    """

    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        self._value = value

    def reveal(self) -> T:
        """The wrapped value. Call this only at the point of use."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        """Equal when the wrapped values are, so tests can assert on secrets
        without every call site having to unwrap first. Comparing against a
        bare value is intentionally unsupported — spelling out
        ``secret.reveal() == "x"`` keeps the unwrap visible at the call site
        that needs it, rather than encouraging ``secret == "x"`` everywhere."""
        return isinstance(other, Secret) and self._value == other._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))

    def __bool__(self) -> bool:
        """Truthy exactly when the wrapped value is, so ``if secret:`` reads
        naturally without an unwrap — the boolean itself carries no secret
        material, unlike ``repr``/``str``."""
        return bool(self._value)
