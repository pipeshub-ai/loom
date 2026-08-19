"""Keeping credentials out of what a journal shows.

A step's *inputs* are recorded so a person reading a trace can see what it was
called with. That is a debugging aid and it is never replayed — but until now it
was also the shortest path from a credential to durable storage:

    await ctx.step(call_api, "hello", api_key)

    call_api -> {'args': ['hello', 'sk-SUPER-SECRET-123'], 'kwargs': {}}

``loom show`` prints that back. Nothing in the journal path redacted anything.

**Three layers, strongest first.** The weakest is the only one that works on
code nobody wrote with secrets in mind, which is why all three exist.

:class:`loom.core.secret.Secret`
    Refuses to serialize at all, so a step that tries to *return* one fails
    loudly at write time rather than leaking quietly at read time. Strongest,
    because it travels with the value and does not care what anyone named it.
    This module's job for a ``Secret`` is narrow: replace it before the encoder
    reaches it, so one secret argument no longer takes the whole recorded input
    down with it — ``{'__unserializable__': 'dict'}`` was safe and told a reader
    nothing about the other arguments.

``pydantic.SecretStr``
    Encodes as ``'**********'``. Pydantic has always done this, so a model field
    typed that way was never at risk — a fact worth pinning with a test rather
    than reimplementing.

A name denylist
    For everything else, because most code is written before anyone thinks
    about the journal. Any mapping key or parameter name that reads like a
    credential is replaced. This runs over the *encoded* value, after models
    have become dicts, so a nested model's ``api_key`` field is caught by the
    same rule as a plain dict key.

Binding positionals is what makes the third layer useful. A denylist over keys
cannot see ``ctx.step(parse, text, api_key)`` — a positional argument has no
key — so a step call matches its arguments to the function's signature first.

What remains, and is not fixable here: a secret interpolated into a string
(``f"Bearer {token}"``), or one passed positionally to a parameter nobody named
suggestively. Detecting either means guessing at the *contents* of a value, and
a heuristic that redacts anything resembling a key eventually eats a legitimate
identifier. That residual is why the reference-workflow rules forbid passing a
credential as a step argument at all, rather than relying on this.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "DEFAULT_REDACT_KEYS",
    "REDACTED",
    "is_secret_name",
    "redact",
    "redact_call_input",
    "strip_secret_values",
]

#: What a redacted value is replaced with. Distinct from pydantic's
#: ``'**********'`` on purpose, so a trace shows which layer acted.
REDACTED = "***"

#: Name fragments that mean "this is a credential", matched case-insensitively
#: against a mapping key or a parameter name.
#:
#: Matched on whole words, never as a bare substring — ``tokenizer_config`` is
#: not a token. A **multi-word** entry matches a consecutive run anywhere, since
#: ``api_key`` is unambiguous wherever it appears (``openai_api_key``,
#: ``x_api_key``). A **single-word** entry must be the *last* word, because
#: those read as credentials in the suffix position (``client_secret``,
#: ``db_password``, ``access_token``) and as ordinary nouns elsewhere:
#: ``secret_santa_list`` and ``token_endpoint`` are not secrets, and a denylist
#: that redacts ordinary data is one people switch off.
DEFAULT_REDACT_KEYS: frozenset[str] = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "client_secret",
        "credential",
        "credentials",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "signing_secret",
        "token",
    }
)

_WORD = re.compile(r"[^a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def is_secret_name(name: str, keys: Iterable[str] = DEFAULT_REDACT_KEYS) -> bool:
    """Whether *name* reads like a credential.

    Matches on word parts, so ``openai_api_key``, ``qb_api_key``, ``apiKey``,
    and ``X-Api-Key`` all hit ``api_key`` while ``keyboard``,
    ``tokenizer_config``, and ``secret_santa_list`` do not. See
    :data:`DEFAULT_REDACT_KEYS` for why one-word and many-word entries match
    differently.
    """
    lowered = _WORD.sub("_", _CAMEL.sub("_", name).lower()).strip("_")
    if not lowered:
        return False
    parts = lowered.split("_")
    for key in keys:
        needle = key.split("_")
        if len(needle) == 1:
            if parts[-1] == needle[0]:
                return True
        elif _contains_run(parts, needle):
            return True
    return False


def _contains_run(parts: Sequence[str], needle: Sequence[str]) -> bool:
    """Whether *needle* appears as a consecutive run of whole words in *parts*."""
    if not needle or len(needle) > len(parts):
        return False
    span = len(needle)
    return any(
        list(parts[i : i + span]) == list(needle) for i in range(len(parts) - span + 1)
    )


def _is_secret_value(value: Any) -> bool:
    """Whether *value* is a type that exists to hold a credential.

    Duck-typed rather than imported, so this module stays free of both a
    pydantic import and a cycle back into :mod:`loom.core.secret`.
    """
    return hasattr(value, "reveal") or hasattr(value, "get_secret_value")


def strip_secret_values(value: Any) -> Any:
    """Replace secret-typed values with :data:`REDACTED`, before encoding.

    Runs on raw Python rather than on an encoded structure, because the point
    is to get a :class:`~loom.core.secret.Secret` out of the way *before* the
    encoder refuses it. Pydantic models are left intact — their own serialiser
    already redacts a ``SecretStr`` field, and better than a walk could.
    """
    if _is_secret_value(value):
        return REDACTED
    if isinstance(value, Mapping):
        return {k: strip_secret_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_secret_values(v) for v in value]
    if isinstance(value, tuple):
        return tuple(strip_secret_values(v) for v in value)
    return value


def redact(value: Any, keys: Iterable[str] = DEFAULT_REDACT_KEYS) -> Any:
    """Replace credential-named entries anywhere in an already-encoded value.

    Runs over the JSON-ish shape ``encode`` produces, so a Pydantic model's
    fields have already become mapping keys and need no separate handling.
    Non-container values pass through — this decides on *names*, never on
    contents.
    """
    keys = frozenset(keys)
    if not keys:
        return value
    return _walk(value, keys)


def _walk(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            k: REDACTED
            if isinstance(k, str) and is_secret_name(k, keys)
            else _walk(v, keys)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_walk(v, keys) for v in value]
    if isinstance(value, tuple):
        return tuple(_walk(v, keys) for v in value)
    return value


def redact_call_input(
    fn: Callable[..., Any] | None,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    keys: Iterable[str] = DEFAULT_REDACT_KEYS,
) -> dict[str, Any]:
    """Build a step's recorded ``input``, with credential parameters removed.

    Keeps the recorded shape — ``{"args": [...], "kwargs": {...}}`` — because
    that is what ``loom show`` and every existing journal already carry. What
    changes is that a positional argument is first matched to the parameter it
    binds, so ``ctx.step(parse, text, api_key)`` redacts the second position
    even though nothing at the call site named it.

    A signature that cannot be read — a builtin, a C function, a position past
    a ``*args`` — yields no name for those positions, and they are left alone
    rather than guessed at.
    """
    keys = frozenset(keys)
    if not keys:
        return {"args": list(args), "kwargs": dict(kwargs)}

    names = _positional_names(fn, len(args))
    return {
        "args": [
            REDACTED if name and is_secret_name(name, keys) else _walk(value, keys)
            for name, value in zip(names, args, strict=True)
        ],
        "kwargs": {
            name: REDACTED if is_secret_name(name, keys) else _walk(value, keys)
            for name, value in kwargs.items()
        },
    }


def _positional_names(fn: Callable[..., Any] | None, count: int) -> list[str | None]:
    """The parameter name each of *count* positional arguments binds to.

    ``None`` where there is no name to report — past a ``*args``, or when the
    signature cannot be introspected at all.
    """
    blank: list[str | None] = [None] * count
    if fn is None:
        return blank
    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return blank

    names: list[str | None] = []
    positional = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    for index in range(count):
        parameter = parameters[index] if index < len(parameters) else None
        names.append(
            parameter.name if parameter is not None and parameter.kind in positional else None
        )
    return names
