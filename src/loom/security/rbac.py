"""Role-based access control for workflow operations.

Defines roles, permissions, an ``authorize`` function that checks whether a
role can perform a given action, and the :func:`requires` decorator that binds
a permission to the method it guards.

The decorator exists because the alternative did not work. A bare
``self._authorize(Permission.X)`` on the first line of a method is a line
somebody can forget, and four of them were forgotten — ``retry``, ``approve``,
``send_event`` and ``publish`` all mutated a run with no check at all, while
``tests/test_phase5.py`` asserted the role table and never once asserted a call
site. A permission the method *carries* can be enumerated, so
``TestEveryMutatingRuntimeMethodIsGuarded`` fails the build when the next
method is added without one.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, ParamSpec, TypeVar, cast


class Permission(StrEnum):
    """Fine-grained permission tokens for workflow operations."""

    FLOW_AUTHOR = "flow:author"
    FLOW_DEPLOY = "flow:deploy"
    FLOW_RUN = "flow:run"
    FLOW_CANCEL = "flow:cancel"
    RUN_VIEW = "run:view"
    RUN_REPLAY = "run:replay"
    GRANT_APPROVE = "grant:approve"
    ADMIN_ALL = "admin:all"


class Role(StrEnum):
    """Predefined roles with escalating privilege levels."""

    ADMIN = "admin"
    DEVELOPER = "developer"
    OPERATOR = "operator"
    VIEWER = "viewer"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset({Permission.ADMIN_ALL}),
    Role.DEVELOPER: frozenset({
        Permission.FLOW_AUTHOR,
        Permission.FLOW_DEPLOY,
        Permission.FLOW_RUN,
        Permission.FLOW_CANCEL,
        Permission.RUN_VIEW,
        Permission.RUN_REPLAY,
    }),
    Role.OPERATOR: frozenset({
        Permission.FLOW_RUN,
        Permission.FLOW_CANCEL,
        Permission.RUN_VIEW,
    }),
    Role.VIEWER: frozenset({Permission.RUN_VIEW}),
}


class AuthorizationError(Exception):
    """Raised when a role lacks the required permission."""


def authorize(role: Role, permission: Permission) -> bool:
    """Return ``True`` if *role* is allowed to perform *permission*.

    The ``ADMIN_ALL`` permission acts as a wildcard — any role that holds it
    is implicitly granted every other permission.
    """
    granted = ROLE_PERMISSIONS.get(role, frozenset())
    if Permission.ADMIN_ALL in granted:
        return True
    return permission in granted


def require(role: Role, permission: Permission) -> None:
    """Assert that *role* holds *permission*, raising on denial.

    Raises:
        AuthorizationError: If the role does not hold the permission.
    """
    if not authorize(role, permission):
        raise AuthorizationError(
            f"role '{role}' lacks permission '{permission}'"
        )


# ---------------------------------------------------------------------------
# Binding a permission to the method it guards
# ---------------------------------------------------------------------------

_P = ParamSpec("_P")
_R = TypeVar("_R")

PERMISSION_ATTR = "__loom_permission__"
"""Where :func:`requires` records what it enforces, so a test can enumerate it."""


def requires(
    permission: Permission,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Enforce *permission* before the decorated coroutine runs, and say so.

    The guard is a no-op when the object has no role configured — the
    ``role=None`` default — so an embedded Runtime behaves exactly as it did
    before any of this existed. The marker is set unconditionally, because the
    test that reads it is about what the method *declares*, not about how a
    particular instance is configured.
    """

    def decorate(fn: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        @functools.wraps(fn)
        async def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            subject: Any = args[0]
            subject._authorize(permission)
            return await fn(*args, **kwargs)

        setattr(guarded, PERMISSION_ATTR, permission)
        return cast("Callable[_P, Awaitable[_R]]", guarded)

    return decorate


def permission_of(fn: Any) -> Permission | None:
    """The permission :func:`requires` bound to *fn*, or ``None``."""
    return cast("Permission | None", getattr(fn, PERMISSION_ATTR, None))
