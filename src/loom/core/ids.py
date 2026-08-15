"""Identifier generation and content-addressed fingerprinting.

Fingerprints give steps a stable identity that is independent of their position in the
graph. That identity is what makes "replay this execution against the edited code"
tractable: the engine can tell "same step, new implementation" apart from "a different
step entirely" and re-execute only from the point of divergence.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import secrets
import time
from typing import Any

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid() -> str:
    """Return a lexicographically sortable, time-prefixed 26-character identifier."""
    timestamp_ms = int(time.time() * 1000)
    randomness = secrets.randbits(80)
    return _encode_base32(timestamp_ms, 10) + _encode_base32(randomness, 16)


def new_id(prefix: str) -> str:
    """Return a prefixed sortable id, e.g. ``run_01J2X...``."""
    return f"{prefix}_{new_ulid()}"


def new_run_id() -> str:
    return new_id("run")


def canonical_json(value: Any) -> str:
    """Serialize ``value`` deterministically: sorted keys, no incidental whitespace.

    Values that are not natively JSON-encodable fall back to a type-qualified ``repr`` so
    that fingerprinting never raises. Fingerprints are advisory, so an imprecise
    representation is preferable to blowing up a run.
    """

    def default(obj: Any) -> Any:
        dump = getattr(obj, "model_dump", None)
        if callable(dump):
            try:
                return dump(mode="json")
            except Exception:
                pass
        if isinstance(obj, set | frozenset):
            return sorted(str(item) for item in obj)
        if isinstance(obj, bytes):
            return hashlib.sha256(obj).hexdigest()
        return f"{type(obj).__module__}.{type(obj).__qualname__}:{obj!r}"

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=default)


def stable_hash(value: Any, *, length: int = 16) -> str:
    """Return a short, stable hex digest of an arbitrary value."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return digest[:length]


def fingerprint(name: str, args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None) -> str:
    """Content address for one invocation: the callable's name plus its arguments."""
    return stable_hash({"name": name, "args": list(args), "kwargs": kwargs or {}})


def code_fingerprint(fn: Any) -> str:
    """Hash a callable's source so the engine can detect implementation changes.

    Falls back to the qualified name when source is unavailable (C extensions, REPL
    definitions, stripped bytecode).
    """
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return stable_hash(f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', fn)}")
    normalized = "\n".join(line.rstrip() for line in source.splitlines() if line.strip())
    return stable_hash(normalized)


def idempotency_key(*parts: Any) -> str:
    """Build a deterministic idempotency key from arbitrary parts."""
    return stable_hash(list(parts), length=32)


def worker_id() -> str:
    """Identify this process within a worker fleet (used for lease ownership)."""
    return f"{os.uname().nodename}:{os.getpid()}"
