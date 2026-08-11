"""Tests for Jira Pydantic response models."""

from __future__ import annotations


class TestJiraIssue:
    def test_fields(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraIssue

        issue = JiraIssue(
            key="PROJ-1",
            id="123",
            summary="Fix bug",
            status="Open",
            priority="High",
            issue_type="Bug",
            project="PROJ",
        )
        assert issue.key == "PROJ-1"
        assert issue.summary == "Fix bug"
        assert issue.status == "Open"
        assert issue.issue_type == "Bug"

    def test_defaults(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraIssue

        issue = JiraIssue(key="X-1")
        assert issue.id == ""
        assert issue.assignee == "Unassigned"
        assert issue.labels == []
        assert issue.summary == ""
        assert issue.url == ""

    def test_model_dump_shape(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraIssue

        issue = JiraIssue(key="A-1", id="1", summary="Test")
        d = issue.model_dump()
        expected_keys = {
            "key", "id", "summary", "status", "assignee",
            "priority", "issue_type", "project", "labels",
            "created", "updated", "url",
        }
        assert set(d.keys()) == expected_keys

    def test_round_trip(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraIssue

        original = JiraIssue(
            key="P-1", id="1", summary="s", status="Open",
            assignee="Alice", priority="High", issue_type="Bug",
            project="P", labels=["urgent"], created="2024-01-01",
            updated="2024-01-02", url="https://x/browse/P-1",
        )
        d = original.model_dump()
        restored = JiraIssue.model_validate(d)
        assert restored == original

    def test_frozen(self) -> None:
        import pytest
        from pydantic import ValidationError

        from workflow_builder.toolsets.jira.models import JiraIssue

        issue = JiraIssue(key="X-1")
        with pytest.raises(ValidationError):
            issue.key = "Y-2"  # type: ignore[misc]

    def test_json_schema(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraIssue

        schema = JiraIssue.model_json_schema()
        assert "properties" in schema
        assert "key" in schema["properties"]
        assert "issue_type" in schema["properties"]


class TestCreatedIssue:
    def test_fields(self) -> None:
        from workflow_builder.toolsets.jira.models import CreatedIssue

        c = CreatedIssue(key="X-1", id="1", url="https://x/browse/X-1")
        assert c.key == "X-1"
        assert c.url == "https://x/browse/X-1"
        d = c.model_dump()
        assert set(d.keys()) == {"key", "id", "url"}


class TestComment:
    def test_fields(self) -> None:
        from workflow_builder.toolsets.jira.models import Comment

        c = Comment(id="100", author="Alice", created="2024-01-01")
        assert c.author == "Alice"
        d = c.model_dump()
        assert set(d.keys()) == {"id", "author", "created"}

    def test_defaults(self) -> None:
        from workflow_builder.toolsets.jira.models import Comment

        c = Comment(id="1")
        assert c.author == ""
        assert c.created == ""


class TestTransition:
    def test_fields(self) -> None:
        from workflow_builder.toolsets.jira.models import Transition

        t = Transition(id="31", name="In Progress")
        assert t.id == "31"
        assert t.name == "In Progress"


class TestJiraProject:
    def test_fields(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraProject

        p = JiraProject(key="XY", name="XY Project", id="10")
        d = p.model_dump()
        assert set(d.keys()) == {"key", "name", "id"}


class TestJiraProjectDetail:
    def test_fields(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraProjectDetail

        p = JiraProjectDetail(
            key="XY", name="XY", id="10",
            description="Desc", lead="Bob",
        )
        assert p.lead == "Bob"
        d = p.model_dump()
        assert set(d.keys()) == {"key", "name", "id", "description", "lead"}


class TestJiraUser:
    def test_fields(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraUser

        u = JiraUser(
            account_id="abc", display_name="Alice", email="a@b.com"
        )
        assert u.account_id == "abc"
        assert u.display_name == "Alice"

    def test_defaults(self) -> None:
        from workflow_builder.toolsets.jira.models import JiraUser

        u = JiraUser(account_id="x", display_name="X")
        assert u.email == ""
