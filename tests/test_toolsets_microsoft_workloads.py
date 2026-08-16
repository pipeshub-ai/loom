"""Teams, OneNote, and Outlook — the workload-specific traps.

The shared Graph layer (auth, paging, error taxonomy, addressing) is covered by
``test_toolsets_microsoft.py``, whose ``Wire`` harness this reuses. What is
tested here is what each *workload* does differently, and in almost every case
the failure being pinned is a plausible wrong answer rather than an error:

- a Teams channel listing that silently ignores a filter;
- an Outlook events listing that misses every recurring meeting;
- an Outlook body that arrives as markup because nobody sent a header;
- a OneNote page created with no title because a fragment was posted.
"""

from __future__ import annotations

import httpx
import pytest

from loom.toolsets.microsoft.errors import GraphPermanentError
from loom.toolsets.microsoft.onenote.client import OneNoteClient, _page_document
from loom.toolsets.microsoft.outlook.calendar.client import OutlookCalendarClient
from loom.toolsets.microsoft.outlook.mail.client import OutlookMailClient
from loom.toolsets.microsoft.outlook.models import CalendarEvent, OutlookMessage
from loom.toolsets.microsoft.teams.client import TeamsClient
from loom.toolsets.microsoft.teams.models import ChatMessage, TeamsChat
from test_toolsets_microsoft import Wire, app_only_auth, token_auth


def teams(wire: Wire, **kw) -> TeamsClient:
    return TeamsClient(kw.pop("auth", None) or token_auth(),
                       transport=wire.transport, **kw)


def onenote(wire: Wire, **kw) -> OneNoteClient:
    return OneNoteClient(kw.pop("auth", None) or token_auth(),
                         transport=wire.transport, **kw)


def mail(wire: Wire, **kw) -> OutlookMailClient:
    return OutlookMailClient(kw.pop("auth", None) or token_auth(),
                             transport=wire.transport, **kw)


def calendar(wire: Wire, **kw) -> OutlookCalendarClient:
    return OutlookCalendarClient(kw.pop("auth", None) or token_auth(),
                                 transport=wire.transport, **kw)


# ---------------------------------------------------------------------------
# The shared app-only refusal, now used by five clients
# ---------------------------------------------------------------------------


class TestAppOnlyRefusalIsShared:
    """One implementation, five callers. Each names its own workload."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("build", "call", "expected_word"),
        [
            (teams, lambda c: c.list_joined_teams(), "teams"),
            (onenote, lambda c: c.list_notebooks(), "notebooks"),
            (mail, lambda c: c.list_messages(), "mailbox"),
            (calendar, lambda c: c.list_calendars(), "calendar"),
        ],
    )
    async def test_each_workload_refuses_before_the_request(
        self, build, call, expected_word: str
    ) -> None:
        wire = Wire({"value": []})
        client = build(wire, auth=app_only_auth())
        with pytest.raises(GraphPermanentError) as caught:
            await call(client)
        assert wire.requests == [], "it should not have spent a round trip"
        message = str(caught.value)
        assert expected_word in message, "the error should name what was wanted"
        assert "MS_REFRESH_TOKEN" in message

    @pytest.mark.asyncio
    async def test_naming_a_user_makes_app_only_work(self) -> None:
        wire = Wire({"value": []})
        await teams(wire, auth=app_only_auth(), user_id="a@b.com").list_joined_teams()
        assert wire.raw() == "/v1.0/users/a%40b.com/joinedTeams"

    @pytest.mark.asyncio
    async def test_a_site_notebook_needs_no_user_at_all(self) -> None:
        """A site or group notebook is addressable app-only, so it must not
        take the user refusal on the way past."""
        wire = Wire({"value": []})
        await onenote(wire, auth=app_only_auth(), site_id="s1").list_notebooks()
        assert wire.path() == "/v1.0/sites/s1/onenote/notebooks"


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


class TestTeamsAddressing:
    @pytest.mark.asyncio
    async def test_a_channel_id_survives_its_colon_and_at_sign(self) -> None:
        """``19:abc@thread.tacv2`` is a path segment containing both."""
        wire = Wire({"value": []})
        await teams(wire).list_channel_messages("T1", "19:abc@thread.tacv2")
        assert wire.raw() == (
            "/v1.0/teams/T1/channels/19%3Aabc%40thread.tacv2/messages"
        )

    @pytest.mark.asyncio
    async def test_a_chat_id_is_escaped_too(self) -> None:
        wire = Wire({"value": []})
        await teams(wire).list_chat_messages("19:x_y@unq.gbl.spaces")
        assert wire.raw() == (
            "/v1.0/chats/19%3Ax_y%40unq.gbl.spaces/messages"
        )


class TestTeamsPagingCeiling:
    @pytest.mark.asyncio
    async def test_messages_ask_for_at_most_fifty(self) -> None:
        """Graph caps a page at 50 and clamps silently above it.

        Asking for more is not an error, so a client that asked for 200 would
        quietly get 50 and page four times as often as it thought.
        """
        wire = Wire({"value": []})
        await teams(wire).list_channel_messages("T1", "C1", limit=500)
        assert wire.query("$top") == "50"

    @pytest.mark.asyncio
    async def test_chats_are_ordered_by_the_only_supported_sort(self) -> None:
        """Graph supports exactly one ordering here and rejects ascending."""
        wire = Wire({"value": []})
        await teams(wire).list_chats()
        assert wire.query("$orderby") == "lastMessagePreview/createdDateTime desc"


class TestTeamsRepliesAreSeparate:
    @pytest.mark.asyncio
    async def test_replies_have_their_own_endpoint(self) -> None:
        """Not a flag on the listing: expanding inline truncates at 200 behind
        a nested next-link, so a long thread comes back short silently."""
        wire = Wire({"value": []})
        await teams(wire).list_message_replies("T1", "C1", "M1")
        assert wire.path().endswith("/messages/M1/replies")

    @pytest.mark.asyncio
    async def test_the_listing_does_not_expand_replies(self) -> None:
        wire = Wire({"value": []})
        await teams(wire).list_channel_messages("T1", "C1")
        assert wire.query("$expand") is None

    @pytest.mark.asyncio
    async def test_members_are_not_expanded_by_default(self) -> None:
        """Graph caps expanded members at 25 whatever page size is asked for,
        and reports no truncation."""
        wire = Wire({"value": []})
        await teams(wire).list_chats()
        assert wire.query("$expand") is None


class TestTeamsSending:
    @pytest.mark.asyncio
    async def test_a_message_body_carries_its_content_type(self) -> None:
        wire = Wire({"id": "1", "body": {"contentType": "html", "content": "hi"}})
        await teams(wire).send_channel_message("T1", "C1", "hi")
        assert wire.body() == {"body": {"contentType": "html", "content": "hi"}}

    @pytest.mark.asyncio
    async def test_a_subject_and_importance_are_sent_when_given(self) -> None:
        wire = Wire({"id": "1"})
        await teams(wire).send_channel_message(
            "T1", "C1", "hi", subject="Deploy", importance="high"
        )
        assert wire.body()["subject"] == "Deploy"
        assert wire.body()["importance"] == "high"

    @pytest.mark.asyncio
    async def test_an_unknown_content_type_is_refused_locally(self) -> None:
        """Graph's own rejection names the enum, not the argument."""
        wire = Wire({"id": "1"})
        with pytest.raises(GraphPermanentError) as caught:
            await teams(wire).send_channel_message("T1", "C1", "hi", content_type="md")
        assert wire.requests == []
        assert "'html' or 'text'" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_reply_posts_under_the_message(self) -> None:
        wire = Wire({"id": "2", "replyToId": "M1"})
        reply = await teams(wire).reply_to_message("T1", "C1", "M1", "ack")
        assert wire.path().endswith("/messages/M1/replies")
        assert reply.reply_to_id == "M1"


class TestTeamsMessageTranslation:
    def test_an_html_body_gets_a_plain_rendering_beside_it(self) -> None:
        """Teams marks nearly every body html even for typed prose, so reading
        ``content`` directly yields markup where a workflow expected words."""
        message = ChatMessage.from_api(
            {"id": "1", "body": {"contentType": "html",
                                 "content": "<div>Deploy&nbsp;done</div>"}}
        )
        assert message.text == "Deploy done"
        assert message.body_html == "<div>Deploy&nbsp;done</div>"

    def test_a_text_body_is_left_alone(self) -> None:
        message = ChatMessage.from_api(
            {"id": "1", "body": {"contentType": "text", "content": "a < b"}}
        )
        assert message.text == "a < b"

    def test_a_system_event_is_marked_as_one(self) -> None:
        """Much of a channel's history is join/leave records, and counting them
        as messages inflates every summary."""
        message = ChatMessage.from_api(
            {"id": "1", "messageType": "systemEventMessage", "from": None}
        )
        assert message.message_type == "systemEventMessage"
        assert message.from_name == ""

    def test_a_mention_is_surfaced(self) -> None:
        message = ChatMessage.from_api(
            {"id": "1", "mentions": [{"id": 0, "mentionText": "Jane Smith"}]}
        )
        assert message.mentions == ["Jane Smith"]

    def test_a_one_on_one_chat_has_no_topic(self) -> None:
        chat = TeamsChat.from_api({"id": "1", "chatType": "oneOnOne", "topic": None})
        assert chat.topic == ""
        assert chat.chat_type == "oneOnOne"


# ---------------------------------------------------------------------------
# OneNote
# ---------------------------------------------------------------------------


class TestOneNotePageDocument:
    """Content is an HTML document, and the title lives inside it."""

    def test_the_title_becomes_the_documents_title_element(self) -> None:
        """There is no title field on a page create — posting a bare fragment
        makes an untitled page and reports success."""
        html = _page_document("Standup", "<p>ok</p>", "")
        assert "<title>Standup</title>" in html
        assert "<p>ok</p>" in html
        assert html.startswith("<!DOCTYPE html>")

    def test_a_title_is_escaped(self) -> None:
        html = _page_document("Q1 <plans> & notes", "", "")
        assert "<title>Q1 &lt;plans&gt; &amp; notes</title>" in html

    def test_the_body_is_not_escaped(self) -> None:
        """It is markup on purpose; escaping it would put visible tags on the
        page."""
        html = _page_document("t", "<ul><li>one</li></ul>", "")
        assert "<ul><li>one</li></ul>" in html

    def test_a_created_timestamp_becomes_a_meta_tag(self) -> None:
        html = _page_document("t", "", "2026-01-05T09:00:00Z")
        assert '<meta name="created" content="2026-01-05T09:00:00Z" />' in html

    @pytest.mark.asyncio
    async def test_create_posts_html_not_json(self) -> None:
        wire = Wire({"id": "p1", "title": "Standup"})
        await onenote(wire).create_page("S1", "Standup", "<p>ok</p>")
        assert wire.last.headers["content-type"] == "text/html"
        assert b"<title>Standup</title>" in wire.last.content

    @pytest.mark.asyncio
    async def test_full_html_bypasses_the_assembly(self) -> None:
        wire = Wire({"id": "p1"})
        await onenote(wire).create_page("S1", "ignored", full_html="<html>mine</html>")
        assert wire.last.content == b"<html>mine</html>"


class TestOneNoteContent:
    @pytest.mark.asyncio
    async def test_page_content_comes_back_as_a_string(self) -> None:
        wire = Wire(httpx.Response(200, content=b"<html><body>hi</body></html>"))
        html = await onenote(wire).get_page_content("P1")
        assert html == "<html><body>hi</body></html>"

    @pytest.mark.asyncio
    async def test_include_ids_is_sent_only_when_asked(self) -> None:
        """Without data-ids the only patch targets are body and title."""
        wire = Wire(httpx.Response(200, content=b"<html/>"))
        await onenote(wire).get_page_content("P1")
        assert wire.query("includeIDs") is None

        wire2 = Wire(httpx.Response(200, content=b"<html/>"))
        await onenote(wire2).get_page_content("P1", include_ids=True)
        assert wire2.query("includeIDs") == "true"

    @pytest.mark.asyncio
    async def test_a_patch_sends_a_command_array(self) -> None:
        """OneNote takes commands, not a replacement document."""
        wire = Wire(httpx.Response(204, content=b""))
        await onenote(wire).patch_page("P1", "<p>more</p>")
        assert wire.last.method == "PATCH"
        assert wire.body() == [
            {"target": "body", "action": "append", "content": "<p>more</p>"}
        ]

    @pytest.mark.asyncio
    async def test_an_unknown_action_is_refused_locally(self) -> None:
        wire = Wire(httpx.Response(204, content=b""))
        with pytest.raises(GraphPermanentError) as caught:
            await onenote(wire).patch_page("P1", "x", action="upsert")
        assert wire.requests == []
        assert "append" in str(caught.value)

    @pytest.mark.asyncio
    async def test_sections_expand_their_parent_notebook(self) -> None:
        """Listed across notebooks, two same-named sections are otherwise
        indistinguishable."""
        wire = Wire({"value": []})
        await onenote(wire).list_sections()
        assert wire.query("$expand") == "parentNotebook"


# ---------------------------------------------------------------------------
# Outlook mail
# ---------------------------------------------------------------------------


class TestOutlookBodyFormat:
    @pytest.mark.asyncio
    async def test_text_bodies_are_requested_by_default(self) -> None:
        """Graph returns HTML unless asked. A model handed markup spends tokens
        on it and reads worse, so the absence of this header is a choice."""
        wire = Wire({"value": []})
        await mail(wire).list_messages()
        assert wire.last.headers["prefer"] == 'outlook.body-content-type="text"'

    @pytest.mark.asyncio
    async def test_html_can_be_asked_for(self) -> None:
        wire = Wire({"value": []})
        await mail(wire).list_messages(body_as_html=True)
        assert "prefer" not in wire.last.headers

    @pytest.mark.asyncio
    async def test_the_preference_is_repeated_on_the_next_page(self) -> None:
        """A next-link carries query parameters but not headers, so a Prefer
        applied to page one only would give one result set in two formats."""
        wire = Wire(
            {
                "value": [{"id": "1"}],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/messages?%24skiptoken=T"
                ),
            },
            {"value": [{"id": "2"}]},
        )
        await mail(wire).list_messages()
        assert len(wire.requests) == 2
        assert wire.requests[1].headers["prefer"] == 'outlook.body-content-type="text"'


class TestOutlookMailQueries:
    @pytest.mark.asyncio
    async def test_a_listing_projects_a_field_set(self) -> None:
        """The reference warns a large page of full messages can hit a gateway
        timeout, and recommends $select."""
        wire = Wire({"value": []})
        await mail(wire).list_messages()
        selected = wire.query("$select") or ""
        assert "subject" in selected
        assert "body," not in selected, "a listing should not carry full bodies"

    @pytest.mark.asyncio
    async def test_reading_one_message_does_include_the_body(self) -> None:
        wire = Wire({"id": "1", "body": {"content": "hello"}})
        message = await mail(wire).get_message("1")
        assert "body" in (wire.query("$select") or "")
        assert message.body == "hello"

    @pytest.mark.asyncio
    async def test_a_folder_scopes_the_listing(self) -> None:
        wire = Wire({"value": []})
        await mail(wire).list_messages(folder_id="inbox")
        assert wire.path() == "/v1.0/me/mailFolders/inbox/messages"

    @pytest.mark.asyncio
    async def test_search_quotes_the_query_and_sends_no_sort(self) -> None:
        """Graph ranks $search by relevance; a sort would be believed and not
        applied."""
        wire = Wire({"value": []})
        await mail(wire).search_messages("invoice")
        assert wire.query("$search") == '"invoice"'
        assert wire.query("$orderby") is None


class TestOutlookSending:
    @pytest.mark.asyncio
    async def test_a_send_wraps_the_message_and_names_recipients(self) -> None:
        wire = Wire(httpx.Response(202, content=b""))
        await mail(wire).send_message(["a@b.com"], "Hi", "Body", cc=["c@d.com"])
        body = wire.body()
        assert wire.path() == "/v1.0/me/sendMail"
        assert body["message"]["subject"] == "Hi"
        assert body["message"]["toRecipients"] == [
            {"emailAddress": {"address": "a@b.com"}}
        ]
        assert body["message"]["ccRecipients"] == [
            {"emailAddress": {"address": "c@d.com"}}
        ]

    @pytest.mark.asyncio
    async def test_a_send_with_no_recipients_is_refused(self) -> None:
        wire = Wire(httpx.Response(202, content=b""))
        with pytest.raises(GraphPermanentError):
            await mail(wire).send_message([], "Hi", "Body")
        assert wire.requests == []

    @pytest.mark.asyncio
    async def test_save_to_sent_is_only_sent_when_disabled(self) -> None:
        """Graph's own note: "Specify it only if the parameter is false"."""
        wire = Wire(httpx.Response(202, content=b""))
        await mail(wire).send_message(["a@b.com"], "s", "b")
        assert "saveToSentItems" not in wire.body()

        wire2 = Wire(httpx.Response(202, content=b""))
        await mail(wire2).send_message(["a@b.com"], "s", "b", save_to_sent=False)
        assert wire2.body()["saveToSentItems"] is False

    @pytest.mark.asyncio
    async def test_a_202_reports_acceptance_not_delivery(self) -> None:
        """Graph: 202 "doesn't indicate that the request processing has
        completed". Returning True for "sent" would be untrue."""
        wire = Wire(httpx.Response(202, content=b""))
        assert await mail(wire).send_message(["a@b.com"], "s", "b") is True

    @pytest.mark.asyncio
    async def test_reply_all_uses_a_different_action(self) -> None:
        wire = Wire(httpx.Response(202, content=b""))
        await mail(wire).reply("M1", "ok", reply_all=True)
        assert wire.path().endswith("/messages/M1/replyAll")

    @pytest.mark.asyncio
    async def test_an_update_with_nothing_to_change_is_refused(self) -> None:
        wire = Wire({"id": "1"})
        with pytest.raises(GraphPermanentError):
            await mail(wire).update_message("M1")
        assert wire.requests == []

    @pytest.mark.asyncio
    async def test_marking_read_patches_only_that_field(self) -> None:
        wire = Wire({"id": "1", "isRead": True})
        await mail(wire).update_message("M1", is_read=True)
        assert wire.body() == {"isRead": True}


class TestOutlookAttachments:
    @pytest.mark.asyncio
    async def test_a_listing_asks_for_metadata_only(self) -> None:
        """Inlining bytes would put tens of megabytes in the journal."""
        wire = Wire({"value": [{"id": "a1", "name": "x.pdf", "size": 10}]})
        rows = await mail(wire).list_attachments("M1")
        assert "contentBytes" not in (wire.query("$select") or "")
        assert rows[0]["name"] == "x.pdf"

    @pytest.mark.asyncio
    async def test_a_file_attachment_decodes_to_bytes(self) -> None:
        wire = Wire(
            {"id": "a1", "name": "note.txt", "contentType": "text/plain",
             "contentBytes": "SGVsbG8gV29ybGQh"}
        )
        attachment = await mail(wire).get_attachment("M1", "a1")
        assert attachment.filename == "note.txt"
        assert attachment.data == b"Hello World!"

    @pytest.mark.asyncio
    async def test_an_item_attachment_is_refused_by_name(self) -> None:
        """An attached mail or a linked file carries no contentBytes, and
        decoding None would be a TypeError several frames from the cause."""
        wire = Wire({"id": "a1", "name": "Fwd: budget"})
        with pytest.raises(GraphPermanentError) as caught:
            await mail(wire).get_attachment("M1", "a1")
        assert "contentBytes" in str(caught.value)


class TestOutlookMessageTranslation:
    def test_the_sender_is_flattened_from_two_levels(self) -> None:
        message = OutlookMessage.from_api(
            {"id": "1", "from": {"emailAddress": {"name": "Ada", "address": "a@b.com"}}}
        )
        assert message.from_.name == "Ada"
        assert message.from_.address == "a@b.com"

    def test_sender_falls_back_when_from_is_absent(self) -> None:
        message = OutlookMessage.from_api(
            {"id": "1", "sender": {"emailAddress": {"address": "s@b.com"}}}
        )
        assert message.from_.address == "s@b.com"

    def test_a_conversation_id_is_carried(self) -> None:
        """Outlook groups by conversation, so acting on one message of a thread
        can look like nothing happened."""
        message = OutlookMessage.from_api({"id": "1", "conversationId": "c1"})
        assert message.conversation_id == "c1"


# ---------------------------------------------------------------------------
# Outlook calendar
# ---------------------------------------------------------------------------


class TestCalendarViewVersusEvents:
    """The distinction the whole calendar toolset is arranged around."""

    @pytest.mark.asyncio
    async def test_the_calendar_view_sends_the_required_window(self) -> None:
        wire = Wire({"value": []})
        await calendar(wire).list_calendar_view(
            "2026-01-05T00:00:00-08:00", "2026-01-06T00:00:00-08:00"
        )
        assert wire.path() == "/v1.0/me/calendarView"
        assert wire.query("startDateTime") == "2026-01-05T00:00:00-08:00"
        assert wire.query("endDateTime") == "2026-01-06T00:00:00-08:00"

    @pytest.mark.asyncio
    async def test_a_view_without_a_window_is_refused(self) -> None:
        """Graph requires both, and without them there is nothing to expand
        recurring series over."""
        wire = Wire({"value": []})
        with pytest.raises(GraphPermanentError) as caught:
            await calendar(wire).list_calendar_view("", "")
        assert wire.requests == []
        assert "start and end" in str(caught.value)

    @pytest.mark.asyncio
    async def test_the_events_listing_is_a_different_endpoint(self) -> None:
        """/events returns series masters; a weekly meeting appears once, with
        a recurrence rule, and not on the days it happens."""
        wire = Wire({"value": []})
        await calendar(wire).list_events()
        assert wire.path() == "/v1.0/me/events"
        assert wire.query("startDateTime") is None

    def test_an_occurrence_names_its_series(self) -> None:
        event = CalendarEvent.from_api(
            {"id": "o1", "type": "occurrence", "seriesMasterId": "m1"}
        )
        assert event.event_type == "occurrence"
        assert event.series_master_id == "m1"


class TestCalendarTimezones:
    @pytest.mark.asyncio
    async def test_no_timezone_sends_no_prefer(self) -> None:
        wire = Wire({"value": []})
        await calendar(wire).list_calendar_view("a", "b")
        assert "prefer" not in wire.last.headers

    @pytest.mark.asyncio
    async def test_a_timezone_is_sent_as_a_prefer_header(self) -> None:
        wire = Wire({"value": []})
        await calendar(wire).list_calendar_view(
            "a", "b", timezone="Pacific Standard Time"
        )
        assert wire.last.headers["prefer"] == 'outlook.timezone="Pacific Standard Time"'

    @pytest.mark.asyncio
    async def test_a_client_default_timezone_applies(self) -> None:
        wire = Wire({"value": []})
        await calendar(wire, timezone="GMT Standard Time").list_calendar_view("a", "b")
        assert wire.last.headers["prefer"] == 'outlook.timezone="GMT Standard Time"'

    def test_a_time_keeps_its_zone_beside_it(self) -> None:
        """Graph returns a local-looking timestamp plus a separate zone name.
        Reading only the first and calling it UTC shifts every meeting."""
        event = CalendarEvent.from_api(
            {"id": "1", "start": {"dateTime": "2026-01-05T09:00:00.0000000",
                                  "timeZone": "Pacific Standard Time"}}
        )
        assert event.start == "2026-01-05T09:00:00.0000000"
        assert event.start_timezone == "Pacific Standard Time"


class TestCalendarWrites:
    @pytest.mark.asyncio
    async def test_creating_an_event_sends_both_ends_with_a_zone(self) -> None:
        wire = Wire({"id": "e1"})
        await calendar(wire).create_event(
            "Review", "2026-01-05T09:00:00", "2026-01-05T10:00:00",
            timezone="Pacific Standard Time",
        )
        body = wire.body()
        assert body["start"] == {
            "dateTime": "2026-01-05T09:00:00",
            "timeZone": "Pacific Standard Time",
        }
        assert body["end"]["timeZone"] == "Pacific Standard Time"

    @pytest.mark.asyncio
    async def test_a_teams_meeting_sets_both_fields(self) -> None:
        """Setting isOnlineMeeting alone yields an event that claims to be
        online and carries no join link — the Outlook version of Google's
        missing conferenceDataVersion."""
        wire = Wire({"id": "e1"})
        await calendar(wire).create_event(
            "Sync", "s", "e", add_teams_meeting=True
        )
        body = wire.body()
        assert body["isOnlineMeeting"] is True
        assert body["onlineMeetingProvider"] == "teamsForBusiness"

    @pytest.mark.asyncio
    async def test_no_teams_meeting_by_default(self) -> None:
        wire = Wire({"id": "e1"})
        await calendar(wire).create_event("Sync", "s", "e")
        assert "isOnlineMeeting" not in wire.body()

    @pytest.mark.asyncio
    async def test_attendees_are_marked_required(self) -> None:
        wire = Wire({"id": "e1"})
        await calendar(wire).create_event("s", "a", "b", attendees=["x@y.com"])
        assert wire.body()["attendees"] == [
            {"emailAddress": {"address": "x@y.com"}, "type": "required"}
        ]

    @pytest.mark.asyncio
    async def test_responding_maps_tentative_to_graphs_own_action(self) -> None:
        wire = Wire(httpx.Response(202, content=b""))
        await calendar(wire).respond_to_event("E1", "tentative")
        assert wire.path().endswith("/events/E1/tentativelyAccept")

    @pytest.mark.asyncio
    async def test_an_unknown_response_is_refused_locally(self) -> None:
        wire = Wire(httpx.Response(202, content=b""))
        with pytest.raises(GraphPermanentError) as caught:
            await calendar(wire).respond_to_event("E1", "maybe")
        assert wire.requests == []
        assert "accept" in str(caught.value)

    @pytest.mark.asyncio
    async def test_cancelling_is_a_different_call_from_deleting(self) -> None:
        """Cancelling notifies attendees; deleting leaves their invitations."""
        wire = Wire(httpx.Response(202, content=b""))
        await calendar(wire).cancel_event("E1", comment="clash")
        assert wire.path().endswith("/events/E1/cancel")
        assert wire.body() == {"comment": "clash"}

    @pytest.mark.asyncio
    async def test_an_update_with_nothing_to_change_is_refused(self) -> None:
        wire = Wire({"id": "e1"})
        with pytest.raises(GraphPermanentError):
            await calendar(wire).update_event("E1")
        assert wire.requests == []


class TestCalendarAvailability:
    @pytest.mark.asyncio
    async def test_a_duration_is_sent_as_an_iso_period(self) -> None:
        """Graph rejects a plain integer here."""
        wire = Wire({"meetingTimeSuggestions": []})
        await calendar(wire).find_meeting_times(["a@b.com"], duration_minutes=45)
        assert wire.body()["meetingDuration"] == "PT45M"

    @pytest.mark.asyncio
    async def test_no_slot_is_an_empty_list_not_an_error(self) -> None:
        wire = Wire({"meetingTimeSuggestions": []})
        assert await calendar(wire).find_meeting_times(["a@b.com"]) == []

    @pytest.mark.asyncio
    async def test_a_suggestion_carries_confidence_and_the_slot(self) -> None:
        wire = Wire(
            {
                "meetingTimeSuggestions": [
                    {
                        "confidence": 100.0,
                        "meetingTimeSlot": {
                            "start": {"dateTime": "2026-01-05T09:00:00",
                                      "timeZone": "UTC"},
                            "end": {"dateTime": "2026-01-05T09:30:00",
                                    "timeZone": "UTC"},
                        },
                        "attendeeAvailability": [{"availability": "free"}],
                    }
                ]
            }
        )
        found = await calendar(wire).find_meeting_times(["a@b.com"])
        assert found[0].confidence == 100.0
        assert found[0].start == "2026-01-05T09:00:00"
        assert found[0].attendee_availability == ["free"]

    @pytest.mark.asyncio
    async def test_an_unreadable_mailbox_reports_an_error_not_freedom(self) -> None:
        """A person silently missing from a free/busy check reads as a person
        who is free, and the meeting gets booked over them."""
        wire = Wire(
            {"value": [{"scheduleId": "x@y.com",
                        "error": {"message": "mailbox not found"}}]}
        )
        slots = await calendar(wire).get_schedule(["x@y.com"], "a", "b")
        assert slots[0].error == "mailbox not found"
        assert slots[0].availability_view == ""

    @pytest.mark.asyncio
    async def test_the_availability_view_is_carried_verbatim(self) -> None:
        wire = Wire({"value": [{"scheduleId": "x@y.com",
                                "availabilityView": "002200"}]})
        slots = await calendar(wire).get_schedule(["x@y.com"], "a", "b")
        assert slots[0].availability_view == "002200"


class TestGapsFoundInAudit:
    """Three operations the first pass left out, each closing a real hole."""

    @pytest.mark.asyncio
    async def test_a_draft_can_be_sent(self) -> None:
        """Without this the documented approval pattern stops one step short.

        ``create_draft`` is described as "an agent writes, wait_for_approval
        parks, a person sends" — but a workflow that *receives* the approval
        had no way to send, so the mail had to be sent by hand.
        """
        wire = Wire(httpx.Response(202, content=b""))
        assert await mail(wire).send_draft("M1") is True
        assert wire.path() == "/v1.0/me/messages/M1/send"
        assert wire.last.method == "POST"

    @pytest.mark.asyncio
    async def test_a_library_file_can_be_deleted(self) -> None:
        """SharePoint could upload a document and never remove one; the only
        way round it was to also hold the OneDrive grant, which is exactly the
        widening the two-toolset split exists to prevent."""
        from test_toolsets_microsoft import sharepoint

        wire = Wire(httpx.Response(204, content=b""))
        assert await sharepoint(wire).delete_item(site="s1", item_id="42") is True
        assert wire.path() == "/v1.0/sites/s1/drive/items/42"
        assert wire.last.method == "DELETE"

    @pytest.mark.asyncio
    async def test_deleting_without_a_target_is_refused(self) -> None:
        """Neither id nor path would address the library root itself."""
        from test_toolsets_microsoft import sharepoint

        wire = Wire(httpx.Response(204, content=b""))
        with pytest.raises(GraphPermanentError):
            await sharepoint(wire).delete_item(site="s1")
        assert wire.requests == []

    @pytest.mark.asyncio
    async def test_a_library_folder_can_be_created(self) -> None:
        from test_toolsets_microsoft import sharepoint

        wire = Wire({"id": "f1", "name": "2026", "folder": {}})
        await sharepoint(wire).create_folder("2026", site="s1", parent_path="Reports")
        assert wire.path() == "/v1.0/sites/s1/drive/root:/Reports:/children"
        assert wire.body()["folder"] == {}

    @pytest.mark.asyncio
    async def test_shared_with_me_reads_the_person_not_the_drive(self) -> None:
        """Sharing is a property of the user; the endpoint does not exist under
        /drives/{id}, so a client configured with a drive_id must still ask
        /me — otherwise it 404s for a reason no message explains."""
        from test_toolsets_microsoft import onedrive

        wire = Wire({"value": []})
        await onedrive(wire, drive_id="b!abc").list_shared_with_me()
        assert wire.path() == "/v1.0/me/drive/sharedWithMe"

    @pytest.mark.asyncio
    async def test_shared_items_carry_their_own_drive(self) -> None:
        """They live in other people's drives, so this client's drive cannot
        address them afterwards."""
        from test_toolsets_microsoft import onedrive

        wire = Wire(
            {"value": [{"id": "1", "name": "budget.xlsx", "file": {},
                        "parentReference": {"driveId": "other-drive"}}]}
        )
        items = await onedrive(wire).list_shared_with_me()
        assert items[0].drive_id == "other-drive"


class TestNationalCloudSupport:
    """A national cloud moves both hosts, so overriding one is a trap.

    US Gov authenticates at ``login.microsoftonline.us`` and serves data from
    ``graph.microsoft.us``. Making only the authority configurable is worse
    than making neither: the tenant authenticates correctly, then calls the
    commercial Graph and gets an authorization error naming the token rather
    than the host.
    """

    def test_the_authority_host_is_overridable(self) -> None:
        from loom.toolsets.microsoft.auth import MicrosoftCredentials

        creds = MicrosoftCredentials.from_env(
            {
                "MS_TENANT_ID": "t",
                "MS_AUTHORITY_HOST": "https://login.microsoftonline.us",
            }
        )
        assert creds.token_url.startswith("https://login.microsoftonline.us/t/")

    def test_the_graph_host_is_overridable_too(self, monkeypatch) -> None:
        from loom.toolsets.microsoft.auth import GRAPH_BASE_URL, graph_base_url

        assert graph_base_url() == GRAPH_BASE_URL
        monkeypatch.setenv("MS_GRAPH_BASE_URL", "https://graph.microsoft.us/v1.0")
        assert graph_base_url() == "https://graph.microsoft.us/v1.0"

    @pytest.mark.parametrize(
        "module",
        [
            "loom.toolsets.microsoft.onedrive.client",
            "loom.toolsets.microsoft.sharepoint.client",
            "loom.toolsets.microsoft.teams.client",
            "loom.toolsets.microsoft.onenote.client",
            "loom.toolsets.microsoft.outlook.mail.client",
            "loom.toolsets.microsoft.outlook.calendar.client",
        ],
    )
    def test_every_default_client_honours_it(self, module: str, monkeypatch) -> None:
        """One variable has to move a whole deployment, or none of it does."""
        import importlib

        monkeypatch.setenv("MS_GRAPH_BASE_URL", "https://graph.microsoft.us/v1.0")
        monkeypatch.setenv("MS_GRAPH_ACCESS_TOKEN", "t")
        client_module = importlib.import_module(module)
        client_module.reset_default_client()
        try:
            client = client_module.get_default_client()
            assert client._session.base_url == "https://graph.microsoft.us/v1.0"
        finally:
            client_module.reset_default_client()
