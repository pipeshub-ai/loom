"""Tests for FilterSpec (event filtering) and EventRouter (pub/sub routing)."""

from __future__ import annotations

import pytest

from loom.triggers.filter import FilterError, FilterSpec
from loom.triggers.routing import EventRouter, RoutingEvent

# ---------------------------------------------------------------------------
# FilterSpec — exact match
# ---------------------------------------------------------------------------


class TestFilterExactMatch:
    def test_exact_match(self) -> None:
        f = FilterSpec(conditions={"priority": "P1"})
        assert f.matches({"priority": "P1"}) is True
        assert f.matches({"priority": "P2"}) is False

    def test_multiple_conditions(self) -> None:
        f = FilterSpec(conditions={"priority": "P1", "status": "open"})
        assert f.matches({"priority": "P1", "status": "open"}) is True
        assert f.matches({"priority": "P1", "status": "closed"}) is False

    def test_missing_field(self) -> None:
        f = FilterSpec(conditions={"priority": "P1"})
        assert f.matches({"other": "value"}) is False

    def test_empty_conditions(self) -> None:
        f = FilterSpec(conditions={})
        assert f.matches({"anything": "goes"}) is True


# ---------------------------------------------------------------------------
# FilterSpec — nested path
# ---------------------------------------------------------------------------


class TestFilterNestedPath:
    def test_dotted_path(self) -> None:
        f = FilterSpec(conditions={"fields.priority.name": "High"})
        payload = {"fields": {"priority": {"name": "High"}}}
        assert f.matches(payload) is True

    def test_missing_nested(self) -> None:
        f = FilterSpec(conditions={"fields.priority.name": "High"})
        payload = {"fields": {"other": "value"}}
        assert f.matches(payload) is False

    def test_non_dict_intermediate(self) -> None:
        f = FilterSpec(conditions={"fields.priority.name": "High"})
        payload = {"fields": "not a dict"}
        assert f.matches(payload) is False

    def test_list_index(self) -> None:
        f = FilterSpec(conditions={"items.0": "first"})
        assert f.matches({"items": ["first", "second"]}) is True
        assert f.matches({"items": ["wrong", "second"]}) is False


# ---------------------------------------------------------------------------
# FilterSpec — operators
# ---------------------------------------------------------------------------


class TestFilterOperators:
    def test_in(self) -> None:
        f = FilterSpec(conditions={"status": {"$in": ["open", "reopened"]}})
        assert f.matches({"status": "open"}) is True
        assert f.matches({"status": "closed"}) is False

    def test_nin(self) -> None:
        f = FilterSpec(conditions={"status": {"$nin": ["closed", "archived"]}})
        assert f.matches({"status": "open"}) is True
        assert f.matches({"status": "closed"}) is False

    def test_gt(self) -> None:
        f = FilterSpec(conditions={"score": {"$gt": 50}})
        assert f.matches({"score": 75}) is True
        assert f.matches({"score": 50}) is False
        assert f.matches({"score": 25}) is False

    def test_gte(self) -> None:
        f = FilterSpec(conditions={"score": {"$gte": 50}})
        assert f.matches({"score": 50}) is True
        assert f.matches({"score": 49}) is False

    def test_lt(self) -> None:
        f = FilterSpec(conditions={"score": {"$lt": 50}})
        assert f.matches({"score": 25}) is True
        assert f.matches({"score": 50}) is False

    def test_lte(self) -> None:
        f = FilterSpec(conditions={"score": {"$lte": 50}})
        assert f.matches({"score": 50}) is True
        assert f.matches({"score": 51}) is False

    def test_ne(self) -> None:
        f = FilterSpec(conditions={"status": {"$ne": "closed"}})
        assert f.matches({"status": "open"}) is True
        assert f.matches({"status": "closed"}) is False

    def test_regex(self) -> None:
        f = FilterSpec(conditions={"email": {"$regex": r"@acme\.com$"}})
        assert f.matches({"email": "bob@acme.com"}) is True
        assert f.matches({"email": "bob@other.com"}) is False

    def test_exists_true(self) -> None:
        f = FilterSpec(conditions={"labels": {"$exists": True}})
        assert f.matches({"labels": ["bug"]}) is True
        assert f.matches({"other": "value"}) is False

    def test_exists_false(self) -> None:
        f = FilterSpec(conditions={"labels": {"$exists": False}})
        assert f.matches({"other": "value"}) is True
        assert f.matches({"labels": ["bug"]}) is False

    def test_combined_operators(self) -> None:
        f = FilterSpec(conditions={"score": {"$gte": 10, "$lte": 100}})
        assert f.matches({"score": 50}) is True
        assert f.matches({"score": 5}) is False
        assert f.matches({"score": 101}) is False

    def test_unknown_operator(self) -> None:
        f = FilterSpec(conditions={"x": {"$unknown": 1}})
        with pytest.raises(ValueError, match="unknown filter operator"):
            f.matches({"x": 1})

    def test_unknown_operator_names_the_field_and_the_alternatives(self) -> None:
        """The message is read by whoever wrote the filter, not by the engine."""
        f = FilterSpec(conditions={"priority": {"$unknown": 1}})

        with pytest.raises(FilterError) as caught:
            f.matches({"priority": 1})

        assert "'priority'" in str(caught.value)
        assert "$in" in str(caught.value)

    def test_a_near_miss_is_suggested(self) -> None:
        f = FilterSpec(conditions={"x": {"$eqq": 1}})

        with pytest.raises(FilterError, match=r"Did you mean '\$eq'\?"):
            f.matches({"x": 1})

    def test_check_finds_a_bad_operator_without_an_event(self) -> None:
        """Declaration time, where the author is still present.

        `matches()` only runs once an event arrives, which in practice is after
        deployment and against a subscriber that then cannot progress.
        """
        with pytest.raises(FilterError, match="unknown filter operator"):
            FilterSpec(conditions={"x": {"$unknown": 1}}).check()

    def test_check_passes_a_well_formed_filter(self) -> None:
        FilterSpec(
            conditions={
                "a": "literal",
                "b": {"$in": [1, 2]},
                "c": {"$gte": 3, "$lte": 4},
                "d": {"$exists": True},
            }
        ).check()

    def test_a_filter_can_be_loaded_even_when_it_is_wrong(self) -> None:
        """Construction stays permissive on purpose.

        Subscriptions are rehydrated from the registry in bulk; refusing to
        build one malformed filter there would fail the load of every other
        subscription alongside it.
        """
        FilterSpec(conditions={"x": {"$unknown": 1}})


class TestUnorderableComparisons:
    """A comparison between types that do not order is a filter bug, not a miss."""

    def test_string_against_number_is_reported(self) -> None:
        f = FilterSpec(conditions={"priority": {"$gt": 50}})

        with pytest.raises(FilterError) as caught:
            f.matches({"priority": "high"})

        assert "do not order" in str(caught.value)
        assert "'priority'" in str(caught.value)

    def test_a_comparable_pair_still_works(self) -> None:
        f = FilterSpec(conditions={"priority": {"$gt": 50}})
        assert f.matches({"priority": 60}) is True
        assert f.matches({"priority": 40}) is False


class TestContainsVersusIn:
    """The two read alike and mean opposite things — the likeliest silent miss."""

    def test_in_asks_whether_the_value_is_one_of_several(self) -> None:
        f = FilterSpec(conditions={"status": {"$in": ["open", "reopened"]}})
        assert f.matches({"status": "open"}) is True
        assert f.matches({"status": "closed"}) is False

    def test_contains_asks_whether_the_payload_list_holds_an_item(self) -> None:
        f = FilterSpec(conditions={"labels": {"$contains": "bug"}})
        assert f.matches({"labels": ["bug", "p1"]}) is True
        assert f.matches({"labels": ["p1"]}) is False

    def test_in_against_a_list_valued_field_does_not_pretend_to_work(self) -> None:
        """`{"labels": {"$in": ["bug"]}}` against `["bug"]` is False, not True.

        Kept as an explicit assertion because it looks like it should match, and
        because the fix is to reach for `$contains` rather than to change `$in`.
        """
        f = FilterSpec(conditions={"labels": {"$in": ["bug"]}})
        assert f.matches({"labels": ["bug"]}) is False

    def test_in_with_a_non_list_operand_is_reported(self) -> None:
        f = FilterSpec(conditions={"labels": {"$in": "bug"}})

        with pytest.raises(FilterError, match=r"needs a list"):
            f.matches({"labels": "bug"})

    def test_eq_exists(self) -> None:
        """Its absence beside `$ne` sent people to an operator that raises."""
        f = FilterSpec(conditions={"status": {"$eq": "open"}})
        assert f.matches({"status": "open"}) is True
        assert f.matches({"status": "closed"}) is False

    def test_null_with_gt(self) -> None:
        f = FilterSpec(conditions={"score": {"$gt": 50}})
        assert f.matches({"other": "value"}) is False

    def test_null_with_regex(self) -> None:
        f = FilterSpec(conditions={"email": {"$regex": "test"}})
        assert f.matches({"other": "value"}) is False


# ---------------------------------------------------------------------------
# EventRouter — subscriptions and routing
# ---------------------------------------------------------------------------


class TestEventRouter:
    @pytest.mark.asyncio
    async def test_basic_routing(self) -> None:
        router = EventRouter()
        router.subscribe("ticket.created", "triage-workflow")
        event = RoutingEvent(name="ticket.created", payload={"id": "T-1"})
        matched = await router.route(event)
        assert matched == ["triage-workflow"]

    @pytest.mark.asyncio
    async def test_fanout_to_multiple(self) -> None:
        router = EventRouter()
        router.subscribe("order.placed", "fulfillment")
        router.subscribe("order.placed", "notifications")
        router.subscribe("order.placed", "analytics")
        event = RoutingEvent(name="order.placed", payload={"order_id": "123"})
        matched = await router.route(event)
        assert len(matched) == 3
        assert set(matched) == {"fulfillment", "notifications", "analytics"}

    @pytest.mark.asyncio
    async def test_no_match(self) -> None:
        router = EventRouter()
        router.subscribe("ticket.created", "triage-workflow")
        event = RoutingEvent(name="unrelated.event")
        matched = await router.route(event)
        assert matched == []

    @pytest.mark.asyncio
    async def test_filter_applied(self) -> None:
        router = EventRouter()
        router.subscribe(
            "ticket.created",
            "urgent-handler",
            filter=FilterSpec(conditions={"priority": "P1"}),
        )
        router.subscribe(
            "ticket.created",
            "normal-handler",
        )

        p1_event = RoutingEvent(name="ticket.created", payload={"priority": "P1"})
        matched = await router.route(p1_event)
        assert set(matched) == {"urgent-handler", "normal-handler"}

        p3_event = RoutingEvent(name="ticket.created", payload={"priority": "P3"})
        matched = await router.route(p3_event)
        assert matched == ["normal-handler"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self) -> None:
        router = EventRouter()
        sub = router.subscribe("event", "workflow")
        router.unsubscribe(sub)
        matched = await router.route(RoutingEvent(name="event"))
        assert matched == []

    @pytest.mark.asyncio
    async def test_routed_history(self) -> None:
        router = EventRouter()
        router.subscribe("event", "workflow")
        await router.route(RoutingEvent(name="event"))
        assert len(router.routed_events) == 1
        router.clear_history()
        assert len(router.routed_events) == 0

    def test_subscriptions_for(self) -> None:
        router = EventRouter()
        router.subscribe("a", "w1")
        router.subscribe("b", "w2")
        router.subscribe("a", "w3")
        subs = router.subscriptions_for("a")
        assert len(subs) == 2
        assert {s.workflow_name for s in subs} == {"w1", "w3"}
