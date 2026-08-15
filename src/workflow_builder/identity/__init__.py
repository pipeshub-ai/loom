"""Inbound identity — who is calling a LOOM surface, and what they may do there.

The other half of a deliberate split from :mod:`workflow_builder.connectors`
(outbound — what LOOM authenticates *with* to call a third-party API on a
run's behalf). They never meet: a bearer token that authenticates a caller to
the MCP server or the HTTP API is never forwarded to a toolset, and a stored
Jira credential never authorizes a facade call.

- :class:`~workflow_builder.identity.principal.Principal` — who, and what
  scopes their token held.
- :data:`~workflow_builder.identity.scopes.Scope` /
  :func:`~workflow_builder.identity.scopes.scopes_to_grant` — LOOM's own
  scope vocabulary, and how it narrows a workflow's declared
  :class:`~workflow_builder.security.grants.GrantSet`.
- :class:`~workflow_builder.identity.facade.AuthorizedFacade` — wraps a
  :class:`~workflow_builder.facade.RuntimeFacade` with a ``Principal``,
  enforcing scope checks and per-run ownership without changing the port's
  signatures.
- :class:`~workflow_builder.identity.config.IdentitySettings` and
  :mod:`~workflow_builder.identity.verifier` — how a bearer token gets
  turned into a ``Principal`` in the first place. ``verifier.py`` imports
  neither ``mcp`` nor ``fastapi``, which is what lets both
  ``mcp_server/auth.py`` and ``server/auth.py`` build on it without either
  surface's extra becoming a dependency of the other.
"""

from __future__ import annotations

from workflow_builder.identity.config import IdentitySettings
from workflow_builder.identity.facade import PRINCIPAL_KEY, AuthorizedFacade
from workflow_builder.identity.principal import ANONYMOUS, Principal, ServicePrincipal
from workflow_builder.identity.scopes import Scope, scopes_to_grant

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
