"""Custom fields: resolving one, asking for one, and being told when it is wrong.

The gap this closes: a Jira custom field is per-instance configuration whose
display name ("Story Points") is stable and whose identifier
(``customfield_10016``) is not. The toolset declared no way to join the two, so
a spec naming a custom field left the coding agent nothing to resolve with —
and a guessed id either writes to the wrong column or is rejected with a 400
the client used to discard.
"""

from __future__ import annotations

from typing import Any

import pytest

from loom.toolsets.jira.client import JiraClient


def client() -> JiraClient:
    return JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")


class Response:
    """The parts of an httpx response ``_classify`` reads."""

    def __init__(self, status: int, body: Any = None, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = "" if body is None else str(body)

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class TestIssuesCarryCustomFields:
    def test_a_read_that_asked_for_none_gets_none(self) -> None:
        from loom.toolsets.jira.client import _flatten_issue

        issue = _flatten_issue({"key": "P-1", "fields": {"summary": "s"}})

        assert issue.custom_fields == {}

    def test_requested_custom_fields_survive_flattening(self) -> None:
        """The failure this replaces: JiraIssue's 12 fields, everything else dropped."""
        from loom.toolsets.jira.client import _flatten_issue

        issue = _flatten_issue(
            {
                "key": "P-1",
                "fields": {
                    "summary": "s",
                    "customfield_10016": 5,
                    "customfield_10001": {"value": "Platform", "id": "10001"},
                },
            }
        )

        assert issue.custom_fields["customfield_10016"] == 5
        assert issue.custom_fields["customfield_10001"]["value"] == "Platform"

    def test_values_are_left_in_jiras_own_shape(self) -> None:
        """Unflattened deliberately: which half of {"value", "id"} a caller
        wants depends on the field, and picking one here loses the other."""
        from loom.toolsets.jira.client import _flatten_issue

        issue = _flatten_issue(
            {"key": "P-1", "fields": {"customfield_10002": {"value": "A", "id": "7"}}}
        )

        assert issue.custom_fields["customfield_10002"] == {"value": "A", "id": "7"}

    async def test_search_appends_rather_than_replaces_the_default_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Replacing them would silently empty ``summary`` and ``status``."""
        sent: dict = {}

        async def fake_post(self, path, json):
            sent.update(json)
            return {"issues": [], "isLast": True, "total": 0}

        monkeypatch.setattr(JiraClient, "_post", fake_post)

        await client().search_issues("project = X", custom_fields=["customfield_10016"])

        assert "summary" in sent["fields"] and "status" in sent["fields"]
        assert "customfield_10016" in sent["fields"]

    async def test_search_does_not_duplicate_a_field_asked_for_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: dict = {}

        async def fake_post(self, path, json):
            sent.update(json)
            return {"issues": [], "isLast": True, "total": 0}

        monkeypatch.setattr(JiraClient, "_post", fake_post)

        await client().search_issues("project = X", custom_fields=["summary"])

        assert sent["fields"].count("summary") == 1

    async def test_get_issue_asks_for_nothing_extra_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unchanged call must produce an unchanged request."""
        seen: dict = {}

        async def fake_get(self, path, **params):
            seen["path"] = path
            seen["params"] = params
            return {"key": "P-1", "fields": {}}

        monkeypatch.setattr(JiraClient, "_get", fake_get)

        await client().get_issue("P-1")

        assert seen["params"] == {}

    async def test_get_issue_names_the_fields_when_asked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict = {}

        async def fake_get(self, path, **params):
            seen.update(params)
            return {"key": "P-1", "fields": {"customfield_10016": 8}}

        monkeypatch.setattr(JiraClient, "_get", fake_get)

        issue = await client().get_issue("P-1", ["customfield_10016"])

        assert "customfield_10016" in seen["fields"]
        assert issue.custom_fields["customfield_10016"] == 8


class TestSystemFieldsAreReadableToo:
    """A system field resolves to a bare id, and a read has to be able to use it.

    The failure: ``jira_resolve_field("Due Date")`` returns ``duedate``, the
    client asks Jira for it, and the flattener kept only ``customfield_*`` — so
    a workflow reading ``issue.custom_fields["duedate"]`` got an empty mapping,
    printed a dash for every row, and reported no error at all.
    """

    def test_a_requested_system_field_survives_flattening(self) -> None:
        from loom.toolsets.jira.client import _flatten_issue

        issue = _flatten_issue(
            {"key": "P-1", "fields": {"resolutiondate": "2026-08-01T09:00:00Z"}},
            ["resolutiondate"],
        )

        assert issue.custom_fields["resolutiondate"] == "2026-08-01T09:00:00Z"

    def test_asked_for_and_empty_is_not_the_same_as_never_asked(self) -> None:
        from loom.toolsets.jira.client import _flatten_issue

        asked = _flatten_issue({"key": "P-1", "fields": {"duedate": None}}, ["duedate"])
        unasked = _flatten_issue({"key": "P-1", "fields": {}})

        assert "duedate" in asked.custom_fields and asked.custom_fields["duedate"] is None
        assert "duedate" not in unasked.custom_fields

    def test_an_unrequested_system_field_is_not_swept_in(self) -> None:
        """``fields`` carries the whole default set; only what was asked for
        belongs in the mapping, or ``custom_fields`` stops meaning anything."""
        from loom.toolsets.jira.client import _flatten_issue

        issue = _flatten_issue({"key": "P-1", "fields": {"summary": "s", "duedate": "2026-01-01"}})

        assert issue.custom_fields == {}

    def test_due_date_is_carried_without_being_asked_for(self) -> None:
        from loom.toolsets.jira.client import _flatten_issue

        assert _flatten_issue(
            {"key": "P-1", "fields": {"duedate": "2026-01-31"}}
        ).due_date == "2026-01-31"

    def test_an_issue_with_no_due_date_reads_as_empty_not_none(self) -> None:
        """Jira sends ``"duedate": null`` for an issue that has none."""
        from loom.toolsets.jira.client import _flatten_issue

        assert _flatten_issue({"key": "P-1", "fields": {"duedate": None}}).due_date == ""

    async def test_the_default_read_asks_for_the_due_date(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: dict = {}

        async def fake_post(self, path, json):
            sent.update(json)
            return {"issues": [], "isLast": True, "total": 0}

        monkeypatch.setattr(JiraClient, "_post", fake_post)

        await client().search_issues("project = X")

        assert "duedate" in sent["fields"]

    async def test_a_search_carries_requested_ids_onto_every_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_post(self, path, json):
            return {
                "issues": [
                    {"key": "P-1", "fields": {"duedate": "2026-02-02"}},
                    {"key": "P-2", "fields": {"duedate": None}},
                ],
                "isLast": True,
                "total": 2,
            }

        monkeypatch.setattr(JiraClient, "_post", fake_post)

        found = await client().search_issues("project = X", custom_fields=["duedate"])

        assert [i.custom_fields["duedate"] for i in found] == ["2026-02-02", None]
        assert [i.due_date for i in found] == ["2026-02-02", ""]

    async def test_a_field_the_defaults_already_fetch_is_still_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is asked for once and handed back regardless: a caller that named
        an id reads it from ``custom_fields`` and does not know which list it
        happened to be on."""
        sent: dict = {}

        async def fake_post(self, path, json):
            sent.update(json)
            return {
                "issues": [{"key": "P-1", "fields": {"duedate": "2026-03-03"}}],
                "isLast": True,
                "total": 1,
            }

        monkeypatch.setattr(JiraClient, "_post", fake_post)

        found = await client().search_issues("project = X", custom_fields=["duedate"])

        assert sent["fields"].count("duedate") == 1
        assert found[0].custom_fields["duedate"] == "2026-03-03"


class TestTheSystemFieldNoteIsTrue:
    """The note is read by a model that then writes code against it."""

    def test_a_carried_field_names_the_attribute(self) -> None:
        note = JiraClient._system_field_note("Due Date", "duedate")

        assert "issue.due_date" in note
        assert "no custom_fields entry" in note

    def test_an_uncarried_field_says_to_ask_for_it(self) -> None:
        """What the old blanket note got wrong, one field over."""
        note = JiraClient._system_field_note("Resolution date", "resolutiondate")

        assert "custom_fields=['resolutiondate']" in note
        assert "no custom_fields entry" not in note


class TestListingFields:
    """Where the list comes from, and why it is not ``GET /field``."""

    def _responses(self, monkeypatch, catalogue, values):
        calls: list[dict] = []

        async def fake_get(self, path, **params):
            calls.append({"path": path, **params})
            if path == "field":
                return catalogue
            return {"values": values, "total": len(values), "startAt": 0}

        monkeypatch.setattr(JiraClient, "_get", fake_get)
        return calls

    async def test_it_reads_field_search_not_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GET /field`` omits app-created custom fields on an instance with
        only team-managed projects, and says nothing when it does."""
        calls = self._responses(
            monkeypatch,
            catalogue=[],
            values=[{"id": "customfield_10016", "name": "Story Points"}],
        )

        found = await client().list_fields()

        paths = [c["path"] for c in calls]
        assert "field/search" in paths
        assert [f.id for f in found] == ["customfield_10016"]

    async def test_it_asks_only_for_custom_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._responses(monkeypatch, catalogue=[], values=[])

        await client().list_fields()

        search = next(c for c in calls if c["path"] == "field/search")
        assert search["type"] == "custom"

    async def test_clause_names_come_from_the_catalogue_when_it_knows_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only ``GET /field`` carries clauseNames, which is why it is still called."""
        self._responses(
            monkeypatch,
            catalogue=[
                {
                    "id": "customfield_10016",
                    "clauseNames": ["Story Points", "cf[10016]"],
                }
            ],
            values=[
                {
                    "id": "customfield_10016",
                    "name": "Story Points",
                    "schema": {"type": "number", "customId": 10016},
                }
            ],
        )

        found = await client().list_fields()

        assert found[0].clause_names == ["Story Points", "cf[10016]"]
        assert found[0].field_type == "number"
        assert found[0].custom_id == 10016

    async def test_clause_names_are_synthesised_for_a_field_the_catalogue_omits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The team-managed case: present in field/search, absent from field.

        An empty ``clause_names`` would read as "unusable in JQL", which is
        the opposite of true.
        """
        self._responses(
            monkeypatch,
            catalogue=[],
            values=[
                {
                    "id": "customfield_10024",
                    "name": "Squad",
                    "schema": {"type": "option", "customId": 10024},
                }
            ],
        )

        found = await client().list_fields()

        assert found[0].clause_names == ["Squad", "cf[10024]"]

    async def test_coverage_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """"Not in the first N" is not "does not exist"."""

        async def fake_get(self, path, **params):
            if path == "field":
                return []
            start = int(params.get("startAt", 0))
            return {
                "values": [
                    {"id": f"customfield_1{n:04d}", "name": f"F{n}"}
                    for n in range(start, start + 2)
                ],
                "total": 500,
                "startAt": start,
            }

        monkeypatch.setattr(JiraClient, "_get", fake_get)

        found = await client().list_fields(max_results=2)

        assert not found.complete
        assert found.total == 500


#: What ``GET /field`` returns for the fields every site has.
SYSTEM_FIELDS = [
    {"id": "summary", "name": "Summary", "custom": False, "clauseNames": ["summary"]},
    {"id": "status", "name": "Status", "custom": False, "clauseNames": ["status"]},
]


class TestResolvingAFieldName:
    def _catalogue(self, monkeypatch, values, system=None):
        catalogue = SYSTEM_FIELDS if system is None else system

        async def fake_get(self, path, **params):
            if path == "field":
                return catalogue
            query = (params.get("query") or "").lower()
            rows = [v for v in values if query in v["name"].lower()] if query else values
            return {"values": rows, "total": len(rows), "startAt": 0}

        monkeypatch.setattr(JiraClient, "_get", fake_get)

    async def test_an_exact_name_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._catalogue(
            monkeypatch, [{"id": "customfield_10016", "name": "Story Points"}]
        )

        found = await client().resolve_field("Story Points")

        assert found.exact
        assert found.matches[0].id == "customfield_10016"

    async def test_case_does_not_defeat_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._catalogue(
            monkeypatch, [{"id": "customfield_10016", "name": "Story Points"}]
        )

        assert (await client().resolve_field("story points")).exact

    async def test_a_near_miss_is_returned_labelled_as_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"Story Points" and "Story point estimate" are different fields on
        instances that have both. Picking one silently writes the wrong column."""
        self._catalogue(
            monkeypatch,
            [{"id": "customfield_10020", "name": "Story point estimate"}],
        )

        found = await client().resolve_field("Story Points")

        assert not found.exact
        assert found.matches[0].id == "customfield_10020"
        assert "suggestion" in found.note

    async def test_duplicate_names_are_reported_rather_than_picked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Jira allows two custom fields with one name; the ids differ."""
        self._catalogue(
            monkeypatch,
            [
                {"id": "customfield_10016", "name": "Story Points"},
                {"id": "customfield_10099", "name": "Story Points"},
            ],
        )

        found = await client().resolve_field("Story Points")

        assert found.exact
        assert len(found.matches) == 2
        assert "customfield_10099" in found.note

    async def test_a_system_field_is_named_as_such(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``summary`` is not a custom field, and "no match" alone would send a
        caller looking for an id that does not exist."""
        self._catalogue(monkeypatch, [])

        found = await client().resolve_field("Summary")

        assert found.exact
        assert found.matches[0].id == "summary"
        assert "system field" in found.note

    async def test_a_system_field_is_not_fuzzy_matched_to_a_custom_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trap in the branch above: field/search returns custom fields
        only, so a system name finds nothing there and gets scored against
        every custom field on the site — returning whichever looks nearest,
        confidently and wrongly."""
        self._catalogue(
            monkeypatch,
            [
                {"id": "customfield_10050", "name": "Summary of impact"},
                {"id": "customfield_10051", "name": "Summary notes"},
            ],
        )

        found = await client().resolve_field("Summary")

        assert found.matches[0].id == "summary"
        assert not found.matches[0].custom

    async def test_an_unknown_name_still_says_what_was_tried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._catalogue(monkeypatch, [])

        found = await client().resolve_field("Velocity")

        assert not found.matches
        assert "Velocity" in found.note


class TestCreatingWithCustomFields:
    async def test_custom_fields_reach_the_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: dict = {}

        async def fake_post(self, path, json):
            sent.update(json)
            return {"key": "P-1", "id": "1"}

        monkeypatch.setattr(JiraClient, "_post", fake_post)

        await client().create_issue(
            "P", "Title", custom_fields={"customfield_10016": 5}
        )

        assert sent["fields"]["customfield_10016"] == 5
        assert sent["fields"]["summary"] == "Title"

    async def test_an_assignee_can_be_set_at_creation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: dict = {}

        async def fake_post(self, path, json):
            sent.update(json)
            return {"key": "P-1", "id": "1"}

        monkeypatch.setattr(JiraClient, "_post", fake_post)

        await client().create_issue("P", "Title", assignee_account_id="712020:abc")

        assert sent["fields"]["assignee"] == {"accountId": "712020:abc"}


class TestAWrongFieldIsRepairable:
    """The 400 body is the only place Jira names the offending field."""

    def test_a_field_error_survives_classification(self) -> None:
        from loom.toolsets.jira.client import _classify

        error = _classify(
            Response(
                400,
                {
                    "errorMessages": [],
                    "errors": {
                        "customfield_10042": (
                            "Field 'customfield_10042' cannot be set. It is not "
                            "on the appropriate screen, or unknown."
                        )
                    },
                },
            )
        )

        assert error.fields["customfield_10042"].startswith("Field 'customfield_10042'")
        assert "customfield_10042" in str(error)

    def test_a_field_error_is_not_retried(self) -> None:
        """Three attempts at a field that does not exist is three of the same answer."""
        from loom.core.retry import PERMANENT_ERRORS
        from loom.toolsets.jira.client import _classify

        error = _classify(Response(400, {"errors": {"customfield_1": "nope"}}))

        assert isinstance(error, PERMANENT_ERRORS)

    def test_errormessages_are_kept_too(self) -> None:
        from loom.toolsets.jira.client import _classify

        error = _classify(Response(400, {"errorMessages": ["Issue does not exist"]}))

        assert "Issue does not exist" in str(error)

    def test_a_body_that_is_not_json_still_classifies(self) -> None:
        from loom.toolsets.jira.client import JiraPermanentError, _classify

        assert isinstance(_classify(Response(400)), JiraPermanentError)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, "JiraAuthError"),
            (404, "JiraNotFound"),
            (403, "JiraPermanentError"),
            (429, "JiraRateLimited"),
            (500, "JiraError"),
        ],
    )
    def test_status_maps_to_the_narrowest_class(self, status: int, expected: str) -> None:
        from loom.toolsets.jira.client import _classify

        assert type(_classify(Response(status, {}))).__name__ == expected

    def test_a_rate_limit_is_retryable_and_carries_the_wait(self) -> None:
        from loom.core.retry import PERMANENT_ERRORS
        from loom.toolsets.jira.client import JiraRateLimited, _classify

        error = _classify(Response(429, {}, {"Retry-After": "30"}))

        assert not isinstance(error, PERMANENT_ERRORS)
        assert isinstance(error, JiraRateLimited)
        assert error.retry_after == 30.0

    def test_a_server_error_is_retryable(self) -> None:
        from loom.core.retry import PERMANENT_ERRORS
        from loom.toolsets.jira.client import _classify

        assert not isinstance(_classify(Response(503, {})), PERMANENT_ERRORS)


class TestTheAgentIsToldHowToResolveOne:
    """The point of the manifest declaration: no agent-side change was needed."""

    def test_the_manifest_names_the_field_resolver(self) -> None:
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        assert JIRA_MANIFEST.resolvers()["field"].function == "jira_resolve_field"

    def test_the_prompt_says_to_resolve_before_filtering(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.jira.manifest import JIRA_MANIFEST

        registry = ToolsetRegistry()
        registry.register(JIRA_MANIFEST)

        described = registry.describe(detail="index")

        assert "Resolve a field with jira_resolve_field" in described

    def test_authoring_may_call_the_resolver(self) -> None:
        """``call_read_operation`` refuses anything but a READ, and resolving a
        field at authoring time is the whole point."""
        from loom.toolsets.jira.manifest import JIRA_MANIFEST
        from loom.toolsets.manifest import EffectClass

        op = JIRA_MANIFEST.find_operation("fields.resolve")
        assert op is not None and op.effect is EffectClass.READ


class TestConfluenceFailuresAreClassifiedToo:
    """The same defect, in the toolset that shares Jira's credentials.

    Confluence raised through ``raise_for_status`` as well, so a page the token
    cannot see was requested three times to be refused three times, and the
    body naming the page went nowhere.
    """

    def test_a_v2_error_body_reaches_the_message(self) -> None:
        from loom.toolsets.confluence.client import _classify

        error = _classify(
            Response(
                404,
                {
                    "errors": [
                        {"title": "Not Found", "detail": "No page with id 12345"}
                    ]
                },
            )
        )

        assert "No page with id 12345" in str(error)

    def test_a_v1_error_body_reaches_the_message(self) -> None:
        """CQL search is the v1 half, and answers in a different shape."""
        from loom.toolsets.confluence.client import _classify

        error = _classify(Response(400, {"message": "Invalid CQL: unknown field"}))

        assert "Invalid CQL" in str(error)

    def test_a_4xx_is_not_retried(self) -> None:
        from loom.core.retry import PERMANENT_ERRORS
        from loom.toolsets.confluence.client import _classify

        assert isinstance(_classify(Response(403, {})), PERMANENT_ERRORS)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, "ConfluenceAuthError"),
            (404, "ConfluenceNotFound"),
            (429, "ConfluenceRateLimited"),
            (500, "ConfluenceError"),
        ],
    )
    def test_status_maps_to_the_narrowest_class(self, status: int, expected: str) -> None:
        from loom.toolsets.confluence.client import _classify

        assert type(_classify(Response(status, {}))).__name__ == expected

    def test_a_rate_limit_stays_retryable(self) -> None:
        from loom.core.retry import PERMANENT_ERRORS
        from loom.toolsets.confluence.client import _classify

        assert not isinstance(_classify(Response(429, {})), PERMANENT_ERRORS)
