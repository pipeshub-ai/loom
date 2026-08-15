"""``Principal`` — who reached a LOOM surface, and what their token attested to.

Says nothing about what a *running workflow* may call outbound; that is
``security.grants.GrantSet`` and ``connectors/``'s job, kept deliberately
separate (see the package docstring). A ``Principal`` only answers "who is
this" and "what scopes did their token hold" — the input to two decisions
made elsewhere: :func:`~loom.identity.scopes.scopes_to_grant`
narrows a workflow's grant by it, and :class:`~loom.identity.facade.AuthorizedFacade`
checks it before letting a facade operation (start a run, cancel one,
publish a workflow) through at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from loom.core.exceptions import InsufficientScope

__all__ = ["ANONYMOUS", "Principal", "ServicePrincipal"]


@runtime_checkable
class _VerifiedToken(Protocol):
    """What :meth:`Principal.from_access_token` needs, shaped like
    ``mcp.server.auth.provider.AccessToken`` without importing it — nothing
    under :mod:`loom.identity` has a reason to depend on the
    ``mcp`` package; only ``mcp_server/server.py`` does.
    """

    subject: str | None
    scopes: list[str]
    client_id: str
    claims: dict[str, Any] | None


@dataclass(frozen=True)
class Principal:
    """One identity, and the scopes its token held.

    Immutable — a caller does not get to expand its own scopes mid-request
    by mutating the object every check reads from.
    """

    subject: str
    """Whatever the token's ``sub`` claim (or equivalent) said. Unique per
    caller, not necessarily human-readable."""

    scopes: frozenset[str] = frozenset()
    client_id: str = ""
    kind: Literal["user", "service", "anonymous"] = "user"
    claims: Mapping[str, Any] = field(default_factory=dict)
    """The verified token's raw claims, for a caller that needs something
    this type does not name — an email for an audit log entry, say. Never
    consulted by anything in :mod:`loom.identity` itself, so a
    verifier that returns claims this type does not otherwise use loses
    nothing."""

    @property
    def is_anonymous(self) -> bool:
        return self.kind == "anonymous"

    def has(self, scope: str) -> bool:
        """Whether *scope* is held, directly or via a broader held scope.

        ``"jira"`` covers a request for ``"jira.issues:read"`` — the same
        "broader scope, dot-narrower name, compatible effect" rule
        :func:`~loom.security.grants._covers` uses for
        ``GrantSet`` toolset entries, so one scope vocabulary and one
        matching rule serves both the inbound (this) and outbound
        (``GrantSet``) sides.

        ``"admin"`` (:data:`~loom.identity.scopes.Scope.ADMIN`)
        satisfies every check, the same wildcard
        :func:`~loom.identity.scopes.scopes_to_grant` gives it
        on the outbound side — checked as a literal here rather than by
        importing that module, which otherwise would import this one back.
        """
        if scope in self.scopes or "admin" in self.scopes:
            return True
        name, _, effect = scope.partition(":")
        for held in self.scopes:
            held_name, _, held_effect = held.partition(":")
            if held_effect and held_effect != effect:
                continue
            if name == held_name or name.startswith(f"{held_name}."):
                return True
        return False

    def requires(self, scope: str) -> None:
        """Raise :class:`InsufficientScope` (→ HTTP 403) if *scope* is not held."""
        if not self.has(scope):
            raise InsufficientScope(
                f"'{self.subject}' does not hold the '{scope}' scope",
                required=scope,
                held=sorted(self.scopes),
            )

    def __repr__(self) -> str:
        return f"Principal(subject={self.subject!r}, kind={self.kind!r})"

    @classmethod
    def from_access_token(cls, token: _VerifiedToken) -> Principal:
        """A verified caller, from whatever a ``TokenVerifier`` returned.

        A service-issued token (``kind="service"``) is distinguished by scope,
        not by this constructor — a scope named ``service`` is exactly as
        ordinary as any other, and this type does not invent a rule to
        detect one that a token's own claims did not state. Build a
        :class:`ServicePrincipal` directly when the caller side already
        knows a token is a service token.
        """
        subject = token.subject or ""
        if not subject:
            # Every reference `TokenVerifier` in `identity/verifier.py`
            # already refuses a token with no subject (returns `None`, a
            # 401), so this is a defensive fallback for a custom verifier
            # that did not — never construct a Principal nothing can be
            # held accountable to.
            return ANONYMOUS
        return cls(
            subject=subject,
            scopes=frozenset(token.scopes),
            client_id=token.client_id,
            claims=dict(token.claims or {}),
        )


ANONYMOUS = Principal(subject="anonymous", kind="anonymous")
"""The identity of a caller nobody authenticated.

Not the same as "no identity is configured" — wrapping a facade in
:class:`~loom.identity.facade.AuthorizedFacade` with this
principal still runs every ``requires()`` check, and every one of them
fails, since ``ANONYMOUS.scopes`` is empty. A surface that means to allow
unauthenticated access (stdio, a loopback-only bind) opts in by not
wrapping the facade at all — handing it this principal and expecting the
checks to be lenient would be the anonymous-but-privileged bug waiting to
happen.
"""


@dataclass(frozen=True)
class ServicePrincipal(Principal):
    """A principal with no human behind it — a scheduled run, a service token.

    Differs from :class:`Principal` only in its default ``kind``. Kept as a
    distinct type rather than a bare ``kind="service"`` call so
    ``Runtime(service_principal=...)`` and the trigger dispatcher's identity
    resolution (a scheduled run has no interactive caller to ask) have a
    type to construct against and to check with ``isinstance``, not a
    string literal to remember to match.
    """

    kind: Literal["user", "service", "anonymous"] = "service"
