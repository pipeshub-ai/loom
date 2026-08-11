"""Role-based access control for workflow operations.

Defines roles, permissions, and an ``authorize`` function that checks
whether a role can perform a given action.
"""

from __future__ import annotations

from enum import StrEnum


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
