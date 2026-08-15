"""``GrantSet`` composition: ``merge()`` widens, ``intersect()`` narrows.

The property that matters for identity work (Phase 3) is that mapping a
token's scopes onto a workflow's declared grant can only take permissions
away, never add to them. These tests pin that down at the ``GrantSet`` level,
before anything about tokens exists.
"""

from __future__ import annotations

from workflow_builder.security.grants import GrantSet, derive_grants


class TestIsEmpty:
    def test_a_bare_grant_set_is_empty(self) -> None:
        assert GrantSet().is_empty

    def test_any_declared_dimension_makes_it_non_empty(self) -> None:
        assert not GrantSet(toolsets=["jira"]).is_empty
        assert not GrantSet(agents=["triage"]).is_empty
        assert not GrantSet(budget={"usd_per_run": 1}).is_empty

    def test_strict_alone_is_never_empty(self) -> None:
        """A strict grant with nothing declared means deny-everything, not

        unrestricted — the exact opposite of what ``is_empty`` normally
        means, so it must not be conflated with "nothing to check".
        """
        assert not GrantSet(strict=True).is_empty


class TestMerge:
    def test_merge_is_a_union(self) -> None:
        merged = GrantSet(toolsets=["jira:read"]).merge(GrantSet(toolsets=["slack:write"]))
        assert set(merged.toolsets) == {"jira:read", "slack:write"}

    def test_merge_deduplicates(self) -> None:
        merged = GrantSet(agents=["triage"]).merge(GrantSet(agents=["triage"]))
        assert merged.agents == ["triage"]

    def test_merge_ors_strict(self) -> None:
        assert GrantSet(strict=True).merge(GrantSet()).strict
        assert GrantSet().merge(GrantSet(strict=True)).strict
        assert not GrantSet().merge(GrantSet()).strict

    def test_merge_can_widen_toolset_access(self) -> None:
        """The documented direction merge is meant for — combining what
        several declared workflows need — is allowed to add up."""
        merged = GrantSet(toolsets=["jira:read"]).merge(GrantSet(toolsets=["jira:write"]))
        assert merged.allows_operation("jira", "issues.create", "write")


class TestIntersect:
    def test_intersect_of_identical_entries_keeps_them(self) -> None:
        a = GrantSet(toolsets=["jira:read"])
        result = a.intersect(GrantSet(toolsets=["jira:read"]))
        assert result.toolsets == ["jira:read"]

    def test_intersect_narrows_to_the_more_specific_scope(self) -> None:
        broad = GrantSet(toolsets=["jira"])
        narrow = GrantSet(toolsets=["jira.issues:read"])
        assert broad.intersect(narrow).toolsets == ["jira.issues:read"]
        assert narrow.intersect(broad).toolsets == ["jira.issues:read"]

    def test_intersect_drops_entries_with_no_counterpart(self) -> None:
        a = GrantSet(toolsets=["jira:read", "slack:write"])
        b = GrantSet(toolsets=["jira:read"])
        assert a.intersect(b).toolsets == ["jira:read"]

    def test_intersect_of_incompatible_effects_yields_nothing(self) -> None:
        read_only = GrantSet(toolsets=["jira:read"])
        write_only = GrantSet(toolsets=["jira:write"])
        assert read_only.intersect(write_only).toolsets == []

    def test_intersect_with_empty_grants_nothing(self) -> None:
        """The conservative choice: absence on either side survives as
        absence, never as "whatever the other side had"."""
        declared = GrantSet(toolsets=["jira:read"])
        assert declared.intersect(GrantSet()).toolsets == []
        assert GrantSet().intersect(declared).toolsets == []

    def test_intersect_never_produces_more_than_either_operand_alone(self) -> None:
        """The property the whole feature rests on, checked directly rather
        than only through the specific-entry examples above."""
        wide = GrantSet(toolsets=["jira", "slack", "github:write"])
        narrow = GrantSet(toolsets=["jira.issues:read", "confluence:read"])
        result = wide.intersect(narrow)

        for entry in result.toolsets:
            scope, _, effect = entry.partition(":")
            toolset, _, group = scope.partition(".")
            op_id = f"{group}.op" if group else "op"
            assert wide.allows_operation(toolset, op_id, effect or "read")
            assert narrow.allows_operation(toolset, op_id, effect or "read")

    def test_intersect_plain_lists_is_exact_set_intersection(self) -> None:
        a = GrantSet(agents=["triage", "auditor"], subflows=["refund"])
        b = GrantSet(agents=["triage"], subflows=["onboarding"])
        result = a.intersect(b)
        assert result.agents == ["triage"]
        assert result.subflows == []

    def test_intersect_budget_takes_the_minimum_of_shared_keys(self) -> None:
        a = GrantSet(budget={"usd_per_run": 5.0})
        b = GrantSet(budget={"usd_per_run": 2.0})
        assert a.intersect(b).budget == {"usd_per_run": 2.0}

    def test_intersect_budget_keeps_a_key_declared_on_only_one_side(self) -> None:
        """Absence means unconstrained, which is the more permissive value —
        so the side that bothered to declare a limit wins."""
        a = GrantSet(budget={"usd_per_run": 5.0})
        b = GrantSet()
        assert a.intersect(b).budget == {"usd_per_run": 5.0}
        assert b.intersect(a).budget == {"usd_per_run": 5.0}

    def test_intersect_ors_strict(self) -> None:
        assert GrantSet(strict=True).intersect(GrantSet()).strict
        assert GrantSet().intersect(GrantSet(strict=True)).strict


def test_derive_grants_is_unaffected_by_the_new_fields() -> None:
    """Regression guard: adding ``strict``/``intersect`` must not change the
    AST-derived defaults an existing caller depends on."""
    grants = derive_grants(
        "async def flow(ctx, _):\n"
        "    await ctx.agent('triage')\n"
        "    await ctx.child('refund')\n"
    )
    assert grants.agents == ["triage"]
    assert grants.subflows == ["refund"]
    assert grants.strict is False
