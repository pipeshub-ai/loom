"""Inbound identity — who is calling a LOOM surface, and what they may do there.

The other half of a deliberate split from :mod:`loom.connectors`
(outbound — what LOOM authenticates *with* to call a third-party API on a
run's behalf). They never meet: a bearer token that authenticates a caller to
the MCP server or the HTTP API is never forwarded to a toolset, and a stored
Jira credential never authorizes a facade call.

- :class:`~loom.identity.principal.Principal` — who, and what
  scopes their token held.
- :data:`~loom.identity.scopes.Scope` /
  :func:`~loom.identity.scopes.scopes_to_grant` — LOOM's own
  scope vocabulary, and how it narrows a workflow's declared
  :class:`~loom.security.grants.GrantSet`.
- :class:`~loom.identity.facade.AuthorizedFacade` — wraps a
  :class:`~loom.facade.RuntimeFacade` with a ``Principal``,
  enforcing scope checks and per-run ownership without changing the port's
  signatures.
- :class:`~loom.identity.config.IdentitySettings` and
  :mod:`~loom.identity.verifier` — how a bearer token gets
  turned into a ``Principal`` in the first place. ``verifier.py`` imports
  neither ``mcp`` nor ``fastapi``, which is what lets both
  ``mcp_server/auth.py`` and ``server/auth.py`` build on it without either
  surface's extra becoming a dependency of the other.
"""

from __future__ import annotations

from loom.identity.config import IdentitySettings
from loom.identity.facade import PRINCIPAL_KEY, AuthorizedFacade
from loom.identity.principal import ANONYMOUS, Principal, ServicePrincipal
from loom.identity.scopes import Scope, scopes_to_grant

__all__ = [
    "ANONYMOUS",
    "PRINCIPAL_KEY",
    "AuthorizedFacade",
    "IdentitySettings",
    "Principal",
    "Scope",
    "ServicePrincipal",
    "scopes_to_grant",
]
