"""Per-run environment overrides, layered over the process environment.

``env=`` on :class:`~workflow_builder.runtime.engine.Runtime.run` is for
configuration a step reads — base URLs, log levels, feature flags. Secrets
belong in ``credentials=``. A key that looks like a secret logs a warning
pointing at that other parameter.

This is deliberately **not** a ``Mapping``. ``__iter__`` / ``__len__`` would
have to decide whether ``os.environ`` is included, and ``dict(ctx.env)``
would be a quiet way to dump the whole process environment into a journal
payload. The surface is ``get``, ``__getitem__``, ``__contains__``, and
``overrides()``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from workflow_builder.core.exceptions import ConfigurationError

__all__ = [
    "MAX_ENV_BYTES",
    "MAX_ENV_KEYS",
    "RunEnvironment",
    "validate_run_env",
]

logger = logging.getLogger("workflow.environment")

MAX_ENV_KEYS = 256
MAX_ENV_BYTES = 64 * 1024
_SECRET_KEY = re.compile(r"(SECRET|TOKEN|PASSWORD|APIKEY|_KEY$)", re.IGNORECASE)
_warned_keys: set[str] = set()


class RunEnvironment:
    """A layered, read-only environment for one workflow run.

    Precedence: per-run overrides, then Runtime-level defaults, then
    ``os.environ``. Never mutates ``os.environ`` — concurrent runs on the
    same event loop each see their own overrides.
    """

    def __init__(
        self,
        run_env: dict[str, str] | None = None,
        runtime_env: dict[str, str] | None = None,
    ) -> None:
        self._run = dict(run_env or {})
        self._runtime = dict(runtime_env or {})

    def get(self, key: str, default: str | None = None) -> str | None:
        if key in self._run:
            return self._run[key]
        if key in self._runtime:
            return self._runtime[key]
        return os.environ.get(key, default)

    def __getitem__(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return key in self._run or key in self._runtime or key in os.environ

    def overrides(self) -> dict[str, str]:
        """The per-run overrides only — never Runtime defaults, never ``os.environ``."""
        return dict(self._run)

    def __repr__(self) -> str:
        return f"<RunEnvironment run={len(self._run)} runtime={len(self._runtime)}>"


def validate_run_env(env: dict[str, Any]) -> dict[str, str]:
    """Normalise and bound a caller-supplied env dict.

    Raises :class:`ConfigurationError` when keys/values are not strings, when
    there are more than :data:`MAX_ENV_KEYS` entries, or when the payload
    exceeds :data:`MAX_ENV_BYTES`. Warns once per key that looks like a secret.
    """
    if not isinstance(env, dict):
        raise ConfigurationError("env must be a dict of str to str")
    out: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigurationError("env keys and values must be strings")
        out[key] = value
    if len(out) > MAX_ENV_KEYS:
        raise ConfigurationError(
            f"env has {len(out)} keys; the limit is {MAX_ENV_KEYS}"
        )
    size = sum(len(key) + len(value) for key, value in out.items())
    if size > MAX_ENV_BYTES:
        raise ConfigurationError(
            f"env is {size} bytes; the limit is {MAX_ENV_BYTES}"
        )
    for key in out:
        if _SECRET_KEY.search(key) and key not in _warned_keys:
            _warned_keys.add(key)
            logger.warning(
                "env key %r looks like a secret; pass it via credentials= instead",
                key,
            )
    return out
