"""LOOM's own scope vocabulary, and the one function that maps scopes onto a grant.

Two vocabularies meet here, and keeping them straight is the point of this
module:

- **LOOM-surface scopes** (:class:`Scope` below — ``runs:write``,
  ``workflows:publish``, ...) govern the facade operations themselves:
  starting a run, cancelling one, publishing a workflow. Checked directly
  via :meth:`~loom.identity.principal.Principal.requires` in
  :class:`~loom.identity.facade.AuthorizedFacade`.
- **Toolset-shaped scopes** (``jira.issues:read``, ``slack.chat:write``, the
  same vocabulary a workflow's own :class:`~loom.security.grants.GrantSet`
  uses) govern what a *run*, once started, may call outbound.
  :func:`scopes_to_grant` narrows a workflow's declared grant by whichever
  of these a token holds.

A token is expected to carry both kinds together, e.g.
``"runs:write jira.issues:read"`` — the first lets the caller start a run at
all, the second bounds what that run may do once it is running.
"""

from __future__ import annotations

from enum import StrEnum

from loom.security.grants import GrantSet

__all__ = ["Scope", "scopes_to_grant"]


class Scope(StrEnum):
    """LOOM's reserved vocabulary for the facade operations themselves.

    Distinct from a toolset's own scopes: these say nothing about what a
    *workflow* may call, only about who may ask a LOOM surface to start,
    cancel, or inspect a run.
    """

    RUNS_READ = "runs:read"
    RUNS_WRITE = "runs:write"
    RUNS_CANCEL = "runs:cancel"
    WORKFLOWS_READ = "workflows:read"
    WORKFLOWS_PUBLISH = "workflows:publish"
    WORKFLOWS_AUTHOR = "workflows:author"
    """Asking a model to write a workflow. Separate from publishing because it
    spends tokens and, when observation is on, reaches systems named in the
    spec — neither of which is implied by being allowed to publish code someone
    has already read."""
    SCHEDULES_WRITE = "schedules:write"
    CREDENTIALS_CONNECT = "credentials:connect"
    """Minting a credential this deployment will then use.

    Its own scope rather than ``workflows:author``: authoring spends tokens and
    reaches out, and neither implies being trusted to obtain a credential every
    later run will act under. Reading connection *status* is not this — that is
    ``workflows:read``, because it carries names and states and no secret."""
    ADMIN = "admin"
    """Every other scope this vocabulary defines, and every toolset a
    workflow could declare. Reserved for a fully trusted caller (a service
    principal, an operator's own token) — a human's day-to-day token should
    never need it, since it defeats the purpose of narrowing at all."""


_RESERVED = frozenset(member.value for member in Scope)


def scopes_to_grant(scopes: frozenset[str], declared: GrantSet) -> GrantSet:
    """Narrow *declared* by whichever of *scopes* are toolset-shaped. Never widens.

    LOOM-surface scopes (:class:`Scope`'s own members) are filtered out
    first — they are not a toolset id, and passing them through would only
    ever fail to match anything in :meth:`GrantSet.intersect`, silently.
    Excluding them here says so instead of relying on that silence.

    The remaining scopes become a ``GrantSet(toolsets=..., strict=True)``:
    ``strict`` because an identity-derived grant that named some toolsets
    but said nothing about agents or sub-workflows must not leave those
    dimensions unchecked (the bug ``GuardedBroker`` used to have before its
    own ``strict`` flag) — see :meth:`GrantSet.intersect`'s docstring for
    the narrowing proof this composes with.

    ``Scope.ADMIN`` is the one exception: it has no toolset-shaped
    counterpart to narrow against, so it maps to *declared* unchanged —
    every dimension the workflow itself asked for, nothing more, which is
    still a subset of ``declared`` and therefore still satisfies the
    "never widens" contract this function is tested against.
    """
    if Scope.ADMIN.value in scopes:
        return declared
    toolset_scopes = sorted(scope for scope in scopes if scope not in _RESERVED)
    token_grant = GrantSet(toolsets=toolset_scopes, strict=True)
    return declared.intersect(token_grant)
