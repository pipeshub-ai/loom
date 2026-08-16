"""``Principal`` and the scope vocabulary: who is calling, and what that narrows.

``AuthorizedFacade`` (exercised end-to-end in ``tests/test_surface_parity.py``)
is the consumer of both; this file pins down the two pieces it depends on in
isolation — scope matching on ``Principal``, and the narrowing invariant
``scopes_to_grant`` must never violate.
"""

from __future__ import annotations

from importlib.util import find_spec

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loom.core.exceptions import InsufficientScope
from loom.identity.principal import ANONYMOUS, Principal, ServicePrincipal
from loom.identity.scopes import Scope, scopes_to_grant
from loom.security.grants import GrantSet


@pytest.mark.skipif(
    find_spec("mcp") is None, reason="needs the mcp extra"
)
class TestFromAccessToken:
    """What a `Principal` is derived from, including tokens we did not shape.

    A host may configure FastMCP with an SDK-native verifier rather than one of
    ours, and the MCP SDK's own ``AccessToken`` has **no subject field**: it
    models a token issued to a *client*, not to a person. Reading the attribute
    directly raised ``AttributeError`` from inside a request handler, which is
    the worst place to discover a shape mismatch.
    """

    def test_a_token_with_a_subject_uses_it(self) -> None:
        from loom.identity.verifier import VerifiedToken

        principal = Principal.from_access_token(
            VerifiedToken(token="t", client_id="loom-cli", scopes=["runs:read"],
                          subject="alice")
        )

        assert principal.subject == "alice"
        assert principal.client_id == "loom-cli"
        assert principal.has("runs:read")

    def test_a_token_with_no_subject_falls_back_to_the_client(self) -> None:
        """What the SDK itself does — ``AuthenticatedUser`` passes
        ``client_id`` to ``SimpleUser`` as the username."""
        from mcp.server.auth.provider import AccessToken

        principal = Principal.from_access_token(
            AccessToken(token="t", client_id="loom-cli", scopes=["runs:read"])
        )

        assert principal.subject == "loom-cli"
        assert principal.has("runs:read")

    def test_a_token_naming_neither_a_person_nor_a_client_is_anonymous(
        self,
    ) -> None:
        """Nothing to hold accountable, so nothing is authorized."""
        from mcp.server.auth.provider import AccessToken

        principal = Principal.from_access_token(
            AccessToken(token="t", client_id="", scopes=["runs:read"])
        )

        assert principal is ANONYMOUS
        assert not principal.has("runs:read")


class TestPrincipal:
    def test_a_held_scope_is_held(self) -> None:
        principal = Principal(subject="alice", scopes=frozenset({"runs:write"}))
        assert principal.has("runs:write")

    def test_an_unheld_scope_is_not_held(self) -> None:
        principal = Principal(subject="alice", scopes=frozenset({"runs:read"}))
        assert not principal.has("runs:write")

    def test_a_broader_scope_covers_a_dot_narrower_request(self) -> None:
        """``"jira"`` covers ``"jira.issues:read"`` — the same rule GrantSet
        toolset entries use, so one mental model serves both directions."""
        principal = Principal(subject="alice", scopes=frozenset({"jira"}))
        assert principal.has("jira.issues:read")

    def test_a_broader_scope_with_a_mismatched_effect_does_not_cover(self) -> None:
        principal = Principal(subject="alice", scopes=frozenset({"jira:read"}))
        assert not principal.has("jira.issues:write")

    def test_admin_covers_everything(self) -> None:
        principal = Principal(subject="root", scopes=frozenset({"admin"}))
        assert principal.has("runs:write")
        assert principal.has("anything:at-all")

    def test_requires_raises_with_the_scope_named(self) -> None:
        principal = Principal(subject="alice", scopes=frozenset())
        with pytest.raises(InsufficientScope) as caught:
            principal.requires("runs:write")
        assert caught.value.required == "runs:write"
        assert caught.value.held == []

    def test_requires_is_silent_when_held(self) -> None:
        Principal(subject="alice", scopes=frozenset({"runs:write"})).requires("runs:write")

    def test_anonymous_holds_nothing(self) -> None:
        assert ANONYMOUS.is_anonymous
        assert not ANONYMOUS.has("runs:read")
        with pytest.raises(InsufficientScope):
            ANONYMOUS.requires("runs:read")

    def test_service_principal_defaults_to_service_kind(self) -> None:
        assert ServicePrincipal(subject="scheduler").kind == "service"

    def test_principal_is_immutable(self) -> None:
        principal = Principal(subject="alice")
        with pytest.raises(AttributeError):
            principal.subject = "bob"  # type: ignore[misc]

    def test_repr_carries_no_scopes_or_claims(self) -> None:
        """A principal's scopes and claims are exactly the kind of thing that
        should not end up in a log line via an unguarded ``repr()``."""
        principal = Principal(
            subject="alice",
            scopes=frozenset({"admin"}),
            claims={"email": "alice@example.com"},
        )
        assert "admin" not in repr(principal)
        assert "alice@example.com" not in repr(principal)


class TestScopesToGrant:
    def test_admin_passes_the_declared_grant_through_unchanged(self) -> None:
        declared = GrantSet(toolsets=["jira:read"], agents=["triage"])
        result = scopes_to_grant(frozenset({"admin"}), declared)
        assert result == declared

    def test_a_token_scope_not_in_the_declared_grant_yields_nothing(self) -> None:
        declared = GrantSet(toolsets=["jira:read"])
        result = scopes_to_grant(frozenset({"slack:read"}), declared)
        assert result.toolsets == []

    def test_a_matching_token_scope_survives(self) -> None:
        declared = GrantSet(toolsets=["jira:read", "slack:write"])
        result = scopes_to_grant(frozenset({"jira:read"}), declared)
        assert result.toolsets == ["jira:read"]
        assert "slack:write" not in result.toolsets

    def test_loom_surface_scopes_are_excluded_not_matched_as_toolsets(self) -> None:
        """``runs:write`` is not a toolset id — it must not leak into the
        narrowed grant just because it shares the ``name:effect`` shape."""
        declared = GrantSet(toolsets=["runs:write"])  # a contrived declared grant
        result = scopes_to_grant(frozenset({"runs:write"}), declared)
        assert result.toolsets == []

    def test_the_result_is_always_strict(self) -> None:
        """An identity-derived grant closes every dimension, including ones
        the token said nothing about — the bug GuardedBroker used to have
        before its own ``strict`` flag existed."""
        declared = GrantSet(toolsets=["jira:read"], agents=["triage"])
        result = scopes_to_grant(frozenset({"jira:read"}), declared)
        assert result.strict
        assert result.agents == []

    def test_no_scopes_at_all_yields_an_empty_strict_grant(self) -> None:
        declared = GrantSet(toolsets=["jira:read"])
        result = scopes_to_grant(frozenset(), declared)
        assert result.toolsets == []
        assert result.strict


# ---------------------------------------------------------------------------
# Property: scopes_to_grant never widens what `declared` permits.
# ---------------------------------------------------------------------------

_toolset_names = st.sampled_from(
    ["jira", "jira.issues", "slack", "slack.chat", "google_calendar", "confluence"]
)
_effects = st.sampled_from(["", "read", "write"])


@st.composite
def _toolset_entry(draw: st.DrawFn) -> str:
    name = draw(_toolset_names)
    effect = draw(_effects)
    return f"{name}:{effect}" if effect else name


_grant_sets = st.builds(
    GrantSet,
    toolsets=st.lists(_toolset_entry(), max_size=4, unique=True),
)
_scope_sets = st.frozensets(_toolset_entry(), max_size=4)


class TestScopesToGrantNeverWidens:
    @given(declared=_grant_sets, scopes=_scope_sets)
    def test_every_narrowed_operation_was_already_permitted(
        self, declared: GrantSet, scopes: frozenset[str]
    ) -> None:
        """Anything ``scopes_to_grant``'s result allows, ``declared`` must
        also allow — for every toolset/op/effect combination hypothesis
        can construct from the entries it drew, not just a handful of
        hand-picked examples."""
        narrowed = scopes_to_grant(scopes, declared)

        for toolset_id in ["jira", "slack", "google_calendar", "confluence"]:
            for op_id in ["issues.create", "chat.post", "events.list"]:
                for effect in ["read", "write"]:
                    if narrowed.allows_operation(toolset_id, op_id, effect):
                        assert declared.allows_operation(toolset_id, op_id, effect), (
                            f"narrowed grant allowed {toolset_id}/{op_id}:{effect} "
                            f"that declared={declared.toolsets} did not"
                        )

    @given(scopes=_scope_sets)
    def test_admin_is_the_only_way_to_get_the_declared_grant_back_unchanged(
        self, scopes: frozenset[str]
    ) -> None:
        declared = GrantSet(toolsets=["jira:read", "slack:write"])
        narrowed = scopes_to_grant(scopes, declared)
        if narrowed == declared:
            assert Scope.ADMIN.value in scopes

    @given(scopes=_scope_sets)
    def test_the_result_is_always_strict_unless_admin(
        self, scopes: frozenset[str]
    ) -> None:
        declared = GrantSet(toolsets=["jira:read"])
        narrowed = scopes_to_grant(scopes, declared)
        if Scope.ADMIN.value not in scopes:
            assert narrowed.strict
