"""Gmail and Google Calendar toolsets.

Everything here runs against an ``httpx.MockTransport``, so the request the test
sees is the one the client would put on the wire — URL, query string, headers,
and body — with no network and no patched internals. The MIME and MIME-tree
handling is tested directly, because that is where the real complexity is.
"""

from __future__ import annotations

import base64
import json
from typing import Any, ClassVar

import httpx
import pytest

from loom import Context, Runtime, workflow
from loom.core.retry import PERMANENT_ERRORS, Retry
from loom.stores.memory import MemoryStore
from loom.toolsets.google.auth import GoogleAuth, GoogleCredentials
from loom.toolsets.google.calendar.client import CalendarClient
from loom.toolsets.google.errors import (
    GoogleAPIError,
    GoogleAuthError,
    GooglePermanentError,
    GoogleRateLimited,
    classify,
)
from loom.toolsets.google.gmail.client import (
    GmailClient,
    build_mime,
    flatten_message,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def token_auth() -> GoogleAuth:
    """Auth that needs no token endpoint."""
    return GoogleAuth(GoogleCredentials(access_token="test-token"))


class Recorder:
    """A mock transport that records requests and replays canned responses."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = responses or {}
        self.status = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for fragment, payload in self._responses.items():
            if fragment in str(request.url):
                if isinstance(payload, httpx.Response):
                    return payload
                return httpx.Response(200, json=payload)
        return httpx.Response(self.status, json={})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


MESSAGE = {
    "id": "m1",
    "threadId": "t1",
    "snippet": "Quarterly numbers attached",
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [
            {"name": "From", "value": "Ada Lovelace <ada@example.com>"},
            {"name": "To", "value": "team@example.com, ops@example.com"},
            {"name": "Subject", "value": "Q3 numbers"},
            {"name": "Date", "value": "Mon, 2 Mar 2026 09:00:00 +0000"},
            {"name": "Message-ID", "value": "<abc@mail.example.com>"},
        ],
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": b64("Numbers are up.")},
            },
            {
                "mimeType": "text/html",
                "body": {"data": b64("<p>Numbers are up.</p>")},
            },
            {
                "mimeType": "application/pdf",
                "filename": "q3.pdf",
                "body": {"attachmentId": "att1", "size": 2048},
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_mode_precedence(self) -> None:
        """The refresh token wins: an access token expires within the hour.

        The setup helper prints both, so both end up in .env — and preferring
        the access token means everything works until it silently stops, with
        the durable credential sitting unused right beside it.
        """
        both = GoogleCredentials(
            access_token="t", client_id="c", client_secret="s", refresh_token="r"
        )
        assert both.mode == "refresh_token"

        alone = GoogleCredentials(access_token="t")
        assert alone.mode == "token"
        assert GoogleCredentials(
            client_id="c", client_secret="s", refresh_token="r"
        ).mode == "refresh_token"
        assert GoogleCredentials(service_account_file="/x.json").mode == "service_account"
        assert GoogleCredentials().mode == ""

    def test_partial_refresh_credentials_are_not_a_mode(self) -> None:
        """Two of the three is not a usable credential, and should say so."""
        assert GoogleCredentials(client_id="c", client_secret="s").mode == ""

    def test_missing_credentials_raise_with_instructions(self) -> None:
        with pytest.raises(GoogleAuthError) as exc:
            GoogleAuth(GoogleCredentials())
        assert "GOOGLE_REFRESH_TOKEN" in str(exc.value)

    def test_from_env(self) -> None:
        creds = GoogleCredentials.from_env({"GOOGLE_ACCESS_TOKEN": "abc"})
        assert creds.access_token == "abc"
        assert creds.mode == "token"

    async def test_static_token_is_returned_verbatim(self) -> None:
        auth = token_auth()
        assert await auth.token() == "test-token"
        assert (await auth.headers())["Authorization"] == "Bearer test-token"

    async def test_refresh_token_is_exchanged_once_and_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ten steps sharing an auth should mint one token, not ten."""
        calls: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(
                200, json={"access_token": "minted", "expires_in": 3600}
            )

        auth = GoogleAuth(
            GoogleCredentials(client_id="c", client_secret="s", refresh_token="r")
        )
        _patch_token_transport(monkeypatch, httpx.MockTransport(handler))

        import asyncio

        tokens = await asyncio.gather(*(auth.token() for _ in range(10)))

        assert set(tokens) == {"minted"}
        assert len(calls) == 1
        assert calls[0]["grant_type"] == "refresh_token"
        assert calls[0]["refresh_token"] == "r"

    async def test_expired_token_is_reminted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        minted = iter(["first", "second"])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": next(minted), "expires_in": 3600}
            )

        auth = GoogleAuth(
            GoogleCredentials(client_id="c", client_secret="s", refresh_token="r")
        )
        _patch_token_transport(monkeypatch, httpx.MockTransport(handler))

        assert await auth.token() == "first"
        auth.invalidate()
        assert await auth.token() == "second"

    async def test_a_rejected_grant_is_a_permanent_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A revoked refresh token will never succeed; retrying wastes a run."""
        auth = GoogleAuth(
            GoogleCredentials(client_id="c", client_secret="s", refresh_token="r")
        )
        _patch_token_transport(
            monkeypatch,
            httpx.MockTransport(
                lambda _: httpx.Response(400, json={"error": "invalid_grant"})
            ),
        )

        with pytest.raises(GoogleAuthError) as exc:
            await auth.token()
        assert isinstance(exc.value, PERMANENT_ERRORS)

    def test_service_account_without_the_extra_says_which_extra(self) -> None:
        auth = GoogleAuth(GoogleCredentials(service_account_file="/nope.json"))
        try:
            import google.auth  # noqa: F401
        except ImportError:
            with pytest.raises(GoogleAuthError) as exc:
                auth._signed_assertion()
            assert "loomflow[google]" in str(exc.value)


def _patch_token_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    """Force the token endpoint through a mock transport."""
    original = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs.setdefault("transport", transport)
        original(self, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_client_errors_do_not_retry(self) -> None:
        """The reason Jira's blanket Retry(3) is wrong for these APIs."""
        error = classify(400, {"error": {"message": "Invalid query"}})
        assert isinstance(error, GooglePermanentError)
        assert isinstance(error, PERMANENT_ERRORS)
        assert not Retry(max_attempts=3).should_retry(error, attempt=1)

    def test_rate_limit_does_retry(self) -> None:
        error = classify(429, {"error": {"message": "Too many"}})
        assert isinstance(error, GoogleRateLimited)
        assert Retry(max_attempts=3).should_retry(error, attempt=1)

    def test_403_splits_on_reason(self) -> None:
        """403 is both 'no scope' and 'slow down', and only reason tells them apart."""
        quota = classify(
            403, {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "q"}}
        )
        forbidden = classify(
            403,
            {"error": {"errors": [{"reason": "insufficientPermissions"}], "message": "p"}},
        )
        assert isinstance(quota, GoogleRateLimited)
        assert isinstance(forbidden, GooglePermanentError)

    def test_401_is_an_auth_error(self) -> None:
        assert isinstance(classify(401, {}), GoogleAuthError)

    def test_server_errors_retry(self) -> None:
        error = classify(503, {"error": {"message": "backend"}})
        assert type(error) is GoogleAPIError
        assert Retry(max_attempts=3).should_retry(error, attempt=1)

    def test_message_survives_a_non_json_body(self) -> None:
        error = classify(500, "<html>nope</html>")
        assert "nope" in str(error)


# ---------------------------------------------------------------------------
# Gmail — MIME
# ---------------------------------------------------------------------------


class TestMime:
    def test_flatten_prefers_plain_text_over_html(self) -> None:
        message = flatten_message(MESSAGE)
        assert message.body == "Numbers are up."

    def test_flatten_extracts_headers(self) -> None:
        message = flatten_message(MESSAGE)
        assert message.subject == "Q3 numbers"
        assert message.sender == "Ada Lovelace <ada@example.com>"
        assert message.to == ["team@example.com", "ops@example.com"]
        assert message.date.startswith("Mon, 2 Mar 2026")

    def test_flatten_lists_attachments_without_downloading_them(self) -> None:
        message = flatten_message(MESSAGE)
        assert len(message.attachments) == 1
        ref = message.attachments[0]
        assert (ref.filename, ref.size, ref.attachment_id) == ("q3.pdf", 2048, "att1")

    def test_unread_is_a_label_not_a_field(self) -> None:
        assert flatten_message(MESSAGE).is_unread
        read = {**MESSAGE, "labelIds": ["INBOX"]}
        assert not flatten_message(read).is_unread

    def test_html_only_message_falls_back_to_stripped_text(self) -> None:
        raw = {
            "id": "m2",
            "payload": {
                "mimeType": "text/html",
                "headers": [],
                "body": {
                    "data": b64(
                        "<style>p{color:red}</style><p>Hello&nbsp;&amp; welcome</p>"
                    )
                },
            },
        }
        body = flatten_message(raw).body
        assert "Hello & welcome" in body
        assert "color:red" not in body

    def test_nested_multipart_is_walked(self) -> None:
        raw = {
            "id": "m3",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [],
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": b64("deep")}}
                        ],
                    }
                ],
            },
        }
        assert flatten_message(raw).body == "deep"

    def test_base64url_padding_is_restored(self) -> None:
        """Gmail strips '=' padding; naive b64decode raises on that."""
        unpadded = base64.urlsafe_b64encode(b"12345").decode().rstrip("=")
        raw = {
            "id": "m4",
            "payload": {
                "mimeType": "text/plain",
                "headers": [],
                "body": {"data": unpadded},
            },
        }
        assert flatten_message(raw).body == "12345"

    def test_build_mime_round_trips(self) -> None:
        import email

        encoded = build_mime(
            to=["a@example.com", "b@example.com"],
            subject="Héllo",
            body="Body text",
            cc="c@example.com",
        )
        parsed = email.message_from_bytes(
            base64.urlsafe_b64decode(encoded.encode())
        )
        assert parsed["To"] == "a@example.com, b@example.com"
        assert parsed["Cc"] == "c@example.com"
        assert "Héllo" in str(email.header.make_header(
            email.header.decode_header(parsed["Subject"])
        ))

    def test_build_mime_html_carries_a_plain_alternative(self) -> None:
        import email

        encoded = build_mime(to="a@b.com", subject="s", body="<b>hi</b>", html=True)
        parsed = email.message_from_bytes(base64.urlsafe_b64decode(encoded.encode()))
        types = {part.get_content_type() for part in parsed.walk()}
        assert {"text/plain", "text/html"} <= types


# ---------------------------------------------------------------------------
# Gmail — client
# ---------------------------------------------------------------------------


class TestGmailClient:
    def client(self, recorder: Recorder) -> GmailClient:
        return GmailClient(token_auth(), transport=recorder.transport())

    async def test_search_hydrates_each_hit(self) -> None:
        recorder = Recorder(
            {
                "/messages?": {"messages": [{"id": "m1", "threadId": "t1"}]},
                "/messages/m1": MESSAGE,
            }
        )
        messages = await self.client(recorder).search_messages("is:unread", 5)

        assert [m.subject for m in messages] == ["Q3 numbers"]
        assert "q=is%3Aunread" in str(recorder.requests[0].url)

    async def test_search_sends_the_bearer_token(self) -> None:
        recorder = Recorder({"/messages?": {"messages": []}})
        await self.client(recorder).search_messages("x")
        assert recorder.last.headers["authorization"] == "Bearer test-token"

    async def test_empty_search_makes_no_hydration_calls(self) -> None:
        recorder = Recorder({"/messages?": {"messages": []}})
        assert await self.client(recorder).search_messages("nothing") == []
        assert len(recorder.requests) == 1

    async def test_pagination_follows_next_page_token(self) -> None:
        pages = iter(
            [
                {"messages": [{"id": "a"}], "nextPageToken": "p2"},
                {"messages": [{"id": "b"}]},
            ]
        )
        recorder = Recorder()
        recorder._responses = {}
        transport = httpx.MockTransport(
            lambda request: (
                recorder.requests.append(request),
                httpx.Response(200, json=next(pages)),
            )[1]
        )
        client = GmailClient(token_auth(), transport=transport)

        refs = await client.list_message_ids(max_results=2)

        assert [r.id for r in refs] == ["a", "b"]
        assert "pageToken=p2" in str(recorder.requests[1].url)

    async def test_send_posts_a_base64url_raw_message(self) -> None:
        recorder = Recorder({"/send": {"id": "s1", "threadId": "t9"}})
        sent = await self.client(recorder).send_message(
            "a@b.com", "Subject", "Body"
        )

        assert sent.id == "s1"
        assert recorder.last.method == "POST"
        decoded = base64.urlsafe_b64decode(recorder.body()["raw"].encode()).decode()
        assert "To: a@b.com" in decoded
        assert "Subject: Subject" in decoded

    async def test_reply_sets_the_threading_headers(self) -> None:
        """threadId alone threads in Gmail and nowhere else."""
        recorder = Recorder({"/messages/m1": MESSAGE, "/send": {"id": "s2"}})
        await self.client(recorder).reply_to_message("m1", "Thanks")

        raw = base64.urlsafe_b64decode(recorder.body()["raw"].encode()).decode()
        assert "In-Reply-To: <abc@mail.example.com>" in raw
        assert "References: <abc@mail.example.com>" in raw
        assert "Subject: Re: Q3 numbers" in raw
        assert recorder.body()["threadId"] == "t1"

    async def test_reply_does_not_double_prefix_re(self) -> None:
        already = {
            **MESSAGE,
            "payload": {
                **MESSAGE["payload"],  # type: ignore[dict-item]
                "headers": [
                    {"name": "Subject", "value": "Re: Q3 numbers"},
                    {"name": "From", "value": "ada@example.com"},
                ],
            },
        }
        recorder = Recorder({"/messages/m1": already, "/send": {"id": "s3"}})
        await self.client(recorder).reply_to_message("m1", "ok")

        raw = base64.urlsafe_b64decode(recorder.body()["raw"].encode()).decode()
        assert "Subject: Re: Q3 numbers" in raw
        assert "Re: Re:" not in raw

    async def test_reply_all_includes_the_original_recipients(self) -> None:
        recorder = Recorder({"/messages/m1": MESSAGE, "/send": {"id": "s4"}})
        await self.client(recorder).reply_to_message("m1", "ok", reply_all=True)

        raw = base64.urlsafe_b64decode(recorder.body()["raw"].encode()).decode()
        assert "team@example.com" in raw

    async def test_modify_labels_posts_both_lists(self) -> None:
        recorder = Recorder({"/modify": MESSAGE})
        await self.client(recorder).modify_labels("m1", ["STARRED"], ["UNREAD"])

        assert recorder.body() == {
            "addLabelIds": ["STARRED"],
            "removeLabelIds": ["UNREAD"],
        }

    async def test_get_attachment_returns_a_loom_attachment(self) -> None:
        payload = b"%PDF-1.4 fake"
        recorder = Recorder(
            {
                "/attachments/att1": {
                    "data": base64.urlsafe_b64encode(payload).decode(),
                    "size": len(payload),
                }
            }
        )
        attachment = await self.client(recorder).get_attachment("m1", "att1", "q3.pdf")

        assert attachment.data == payload
        assert attachment.filename == "q3.pdf"
        assert attachment.mime == "application/pdf"
        assert attachment.sha256

    async def test_labels_and_profile(self) -> None:
        recorder = Recorder(
            {
                "/labels": {
                    "labels": [
                        {
                            "id": "INBOX",
                            "name": "INBOX",
                            "type": "system",
                            "messagesTotal": 12,
                            "messagesUnread": 3,
                        }
                    ]
                },
                "/profile": {"emailAddress": "me@example.com", "messagesTotal": 42},
            }
        )
        client = self.client(recorder)

        labels = await client.list_labels()
        profile = await client.get_profile()

        assert labels[0].messages_unread == 3
        assert profile.email_address == "me@example.com"

    async def test_a_401_reauthenticates_once_then_succeeds(self) -> None:
        """A token that expired early should cost a retry, not a failed run."""
        seen: list[httpx.Request] = []
        replies = iter(
            [httpx.Response(401, json={"error": {"message": "expired"}}),
             httpx.Response(200, json=MESSAGE)]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return next(replies)

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))
        message = await client.get_message("m1")

        assert message.id == "m1"
        assert len(seen) == 2

    async def test_a_persistent_401_surfaces_as_an_auth_error(self) -> None:
        client = GmailClient(
            token_auth(),
            transport=httpx.MockTransport(lambda _: httpx.Response(401, json={})),
        )
        with pytest.raises(GoogleAuthError):
            await client.get_message("m1")

    async def test_a_400_is_permanent(self) -> None:
        client = GmailClient(
            token_auth(),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(400, json={"error": {"message": "bad query"}})
            ),
        )
        with pytest.raises(GooglePermanentError):
            await client.search_messages("subject:(")


# ---------------------------------------------------------------------------
# Calendar — client
# ---------------------------------------------------------------------------


class TestCalendarClient:
    def client(self, recorder: Recorder) -> CalendarClient:
        return CalendarClient(token_auth(), transport=recorder.transport())

    EVENT: ClassVar[dict[str, Any]] = {
        "id": "e1",
        "summary": "Standup",
        "location": "Room 4",
        "status": "confirmed",
        "start": {"dateTime": "2026-03-02T09:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-03-02T09:15:00Z"},
        "organizer": {"email": "lead@example.com"},
        "attendees": [
            {"email": "a@example.com", "responseStatus": "accepted"},
            {"email": "b@example.com", "optional": True},
        ],
        "htmlLink": "https://calendar.google.com/event?eid=e1",
    }

    async def test_list_events_expands_recurrences_and_orders_them(self) -> None:
        recorder = Recorder({"/events": {"items": [self.EVENT]}})
        events = await self.client(recorder).list_events(
            time_min="2026-03-02T00:00:00Z", time_max="2026-03-03T00:00:00Z"
        )

        url = str(recorder.last.url)
        assert "singleEvents=true" in url
        assert "orderBy=startTime" in url
        assert "timeMin=2026-03-02" in url
        assert events[0].summary == "Standup"
        assert events[0].attendees[0].response_status == "accepted"

    async def test_order_by_is_omitted_when_recurrences_are_not_expanded(self) -> None:
        """The API rejects orderBy=startTime unless singleEvents is set."""
        recorder = Recorder({"/events": {"items": []}})
        await self.client(recorder).list_events(single_events=False)
        assert "orderBy" not in str(recorder.last.url)

    async def test_calendar_ids_are_percent_encoded(self) -> None:
        """Calendar ids are email addresses; an unescaped @ breaks the path."""
        recorder = Recorder({"/events": {"items": []}})
        await self.client(recorder).list_events(calendar_id="team@example.com")
        assert "team%40example.com/events" in str(recorder.last.url)

    async def test_all_day_events_are_flagged(self) -> None:
        raw = {
            "id": "e2",
            "summary": "Holiday",
            "start": {"date": "2026-12-25"},
            "end": {"date": "2026-12-26"},
        }
        recorder = Recorder({"/events": {"items": [raw]}})
        event = (await self.client(recorder).list_events())[0]

        assert event.all_day
        assert event.start == "2026-12-25"

    async def test_timed_events_are_not_flagged_all_day(self) -> None:
        recorder = Recorder({"/events": {"items": [self.EVENT]}})
        event = (await self.client(recorder).list_events())[0]
        assert not event.all_day
        assert event.time_zone == "UTC"

    async def test_create_event_builds_the_nested_time_shape(self) -> None:
        recorder = Recorder({"/events": self.EVENT})
        await self.client(recorder).create_event(
            "Standup",
            "2026-03-02T09:00:00Z",
            "2026-03-02T09:15:00Z",
            attendees=["a@example.com"],
            time_zone="Europe/London",
        )

        body = recorder.body()
        assert body["start"] == {
            "dateTime": "2026-03-02T09:00:00Z",
            "timeZone": "Europe/London",
        }
        assert body["attendees"] == [{"email": "a@example.com"}]

    async def test_create_all_day_uses_date_not_datetime(self) -> None:
        recorder = Recorder({"/events": self.EVENT})
        await self.client(recorder).create_event(
            "Holiday", "2026-12-25", "2026-12-26", all_day=True, time_zone="UTC"
        )

        body = recorder.body()
        assert body["start"] == {"date": "2026-12-25"}
        assert "timeZone" not in body["start"]

    async def test_attendees_are_not_emailed_by_default(self) -> None:
        """A bulk workflow should not mail everyone because of a default."""
        recorder = Recorder({"/events": self.EVENT})
        await self.client(recorder).create_event(
            "Standup", "2026-03-02T09:00:00Z", "2026-03-02T09:15:00Z"
        )
        assert "sendUpdates=none" in str(recorder.last.url)

    async def test_send_updates_all_is_passed_through(self) -> None:
        recorder = Recorder({"/events": self.EVENT})
        await self.client(recorder).create_event(
            "Standup", "a", "b", send_updates="all"
        )
        assert "sendUpdates=all" in str(recorder.last.url)

    async def test_update_patches_only_the_given_fields(self) -> None:
        recorder = Recorder({"/events/e1": self.EVENT})
        await self.client(recorder).update_event("e1", {"location": "Room 9"})

        assert recorder.last.method == "PATCH"
        assert recorder.body() == {"location": "Room 9"}

    async def test_delete_tolerates_an_empty_204(self) -> None:
        recorder = Recorder({"/events/e1": httpx.Response(204)})
        assert await self.client(recorder).delete_event("e1") is None

    async def test_free_busy_flattens_per_calendar_intervals(self) -> None:
        recorder = Recorder(
            {
                "freeBusy": {
                    "calendars": {
                        "primary": {
                            "busy": [
                                {
                                    "start": "2026-03-02T09:00:00Z",
                                    "end": "2026-03-02T10:00:00Z",
                                }
                            ]
                        },
                        "team@example.com": {"busy": []},
                    }
                }
            }
        )
        busy = await self.client(recorder).free_busy(
            "2026-03-02T00:00:00Z", "2026-03-03T00:00:00Z", ["primary", "team@example.com"]
        )

        assert len(busy) == 1
        assert busy[0].calendar_id == "primary"
        assert recorder.body()["items"] == [
            {"id": "primary"},
            {"id": "team@example.com"},
        ]

    async def test_list_calendars(self) -> None:
        recorder = Recorder(
            {
                "calendarList": {
                    "items": [
                        {
                            "id": "primary",
                            "summary": "Me",
                            "primary": True,
                            "accessRole": "owner",
                        }
                    ]
                }
            }
        )
        calendars = await self.client(recorder).list_calendars()
        assert calendars[0].primary
        assert calendars[0].access_role == "owner"

    async def test_quick_add_passes_the_phrase_as_a_query_param(self) -> None:
        recorder = Recorder({"quickAdd": self.EVENT})
        await self.client(recorder).quick_add_event("Lunch with Bob tomorrow 1pm")
        assert "text=Lunch+with+Bob" in str(recorder.last.url)


# ---------------------------------------------------------------------------
# OAuth setup helper
# ---------------------------------------------------------------------------


class TestSetupHelper:
    def test_missing_client_explains_where_to_get_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from loom.toolsets.google import setup

        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)

        assert setup.main([]) == 2
        assert "console.cloud.google.com" in capsys.readouterr().err

    def test_the_full_loopback_flow(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Fake browser, fake token endpoint, real HTTP server and redirect."""
        import threading
        import time
        import urllib.parse
        import urllib.request

        from loom.toolsets.google import setup

        seen: dict[str, list[str]] = {}

        def fake_browser(url: str) -> bool:
            seen.update(urllib.parse.parse_qs(urllib.parse.urlparse(url).query))

            def redirect_back() -> None:
                time.sleep(0.2)
                urllib.request.urlopen(
                    f"{seen['redirect_uri'][0]}?code=c1&state={seen['state'][0]}"
                ).read()

            threading.Thread(target=redirect_back, daemon=True).start()
            return True

        def fake_post(url: str, data: Any = None, timeout: Any = None) -> httpx.Response:
            assert data["code"] == "c1"
            assert data["grant_type"] == "authorization_code"
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.x",
                    "refresh_token": "1//r",
                    "expires_in": 3599,
                },
            )

        monkeypatch.setattr(setup.webbrowser, "open", fake_browser)
        monkeypatch.setattr(httpx, "post", fake_post)

        code = setup.main(["--client-id", "cid", "--client-secret", "sec"])
        out = capsys.readouterr().out

        assert code == 0
        assert "GOOGLE_REFRESH_TOKEN=1//r" in out
        assert "GOOGLE_ACCESS_TOKEN=ya29.x" in out

        # Without these, Google returns an access token and no refresh token —
        # the whole point of running this.
        assert seen["access_type"] == ["offline"]
        assert seen["prompt"] == ["consent"]

    def test_a_mismatched_state_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The redirect must be the one this run started."""
        import contextlib
        import threading
        import time
        import urllib.error
        import urllib.parse
        import urllib.request

        from loom.toolsets.google import setup

        def fake_browser(url: str) -> bool:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

            def redirect_back() -> None:
                time.sleep(0.2)
                # A 400 is the point, so the HTTPError is the pass condition.
                with contextlib.suppress(urllib.error.HTTPError):
                    urllib.request.urlopen(
                        f"{params['redirect_uri'][0]}?code=evil&state=wrong"
                    ).read()

            threading.Thread(target=redirect_back, daemon=True).start()
            return True

        monkeypatch.setattr(setup.webbrowser, "open", fake_browser)
        assert setup.main(["--client-id", "c", "--client-secret", "s"]) == 1

    def test_the_default_port_is_stable_across_runs(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A port that changes each run means re-registering the redirect URI
        each run, for anyone on a Web application client."""
        from loom.toolsets.google import setup

        ports = [self._run(monkeypatch, capsys)[0] for _ in range(2)]
        assert ports == [setup.DEFAULT_PORT, setup.DEFAULT_PORT]

    def test_a_busy_default_falls_back_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Silently moving would produce a mismatch the user cannot explain."""
        import http.server

        from loom.toolsets.google import setup

        squatter = http.server.HTTPServer(
            ("127.0.0.1", setup.DEFAULT_PORT), http.server.BaseHTTPRequestHandler
        )
        try:
            port, out = self._run(monkeypatch, capsys)
        finally:
            squatter.server_close()

        assert port != setup.DEFAULT_PORT
        assert "was busy" in out

    def test_an_explicit_busy_port_is_an_error_not_a_silent_move(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--port names the one Google was told about; moving off it is useless."""
        import http.server

        from loom.toolsets.google import setup

        monkeypatch.setattr(setup.webbrowser, "open", lambda _: True)
        squatter = http.server.HTTPServer(
            ("127.0.0.1", setup.DEFAULT_PORT), http.server.BaseHTTPRequestHandler
        )
        try:
            code = setup.main(
                ["--client-id", "i", "--client-secret", "s",
                 "--port", str(setup.DEFAULT_PORT)]
            )
        finally:
            squatter.server_close()

        assert code == 2
        assert "in use" in capsys.readouterr().err

    def test_the_redirect_uri_uses_the_loopback_ip(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Google's guidance, and 127.0.0.1 != localhost for URI matching."""
        _, out = self._run(monkeypatch, capsys)
        assert "http://127.0.0.1:" in out

    @pytest.fixture(autouse=True)
    def _own_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point DEFAULT_PORT at a port this test just found free.

        Asserting against the real 8931 makes the suite fail for anyone who
        happens to be running the helper, or anything else, on it.
        """
        import socket

        from loom.toolsets.google import setup

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = int(probe.getsockname()[1])
        monkeypatch.setattr(setup, "DEFAULT_PORT", free)

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> tuple[int, str]:
        """Run the flow to completion; return the bound port and the output.

        Both, because reading the capture buffer empties it — a helper that
        consumed it would leave nothing for the caller to assert on.
        """
        import re
        import threading
        import time
        import urllib.parse
        import urllib.request

        from loom.toolsets.google import setup

        def fake_browser(url: str) -> bool:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect, state = query["redirect_uri"][0], query["state"][0]

            def redirect_back() -> None:
                time.sleep(0.2)
                urllib.request.urlopen(f"{redirect}?code=c&state={state}").read()

            threading.Thread(target=redirect_back, daemon=True).start()
            return True

        monkeypatch.setattr(setup.webbrowser, "open", fake_browser)
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **k: httpx.Response(
                200, json={"access_token": "a", "refresh_token": "r"}
            ),
        )

        assert setup.main(["--client-id", "i", "--client-secret", "s"]) == 0
        out = capsys.readouterr().out
        listening = re.search(r"Listening on http://127\.0\.0\.1:(\d+)/", out)
        assert listening is not None
        return int(listening.group(1)), out

    def test_scope_sets_cover_what_the_tools_need(self) -> None:
        from loom.toolsets.google import setup

        assert set(setup.SCOPE_SETS) == {"read", "write", "gmail", "calendar"}
        assert all(
            scope.startswith("https://www.googleapis.com/auth/")
            for scopes in setup.SCOPE_SETS.values()
            for scope in scopes
        )
        # Read-only must not be able to send.
        assert not any("send" in s for s in setup.SCOPE_SETS["read"])


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


class TestManifests:
    def test_both_register_and_stay_distinguishable(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.google import (
            GMAIL_MANIFEST,
            GOOGLE_CALENDAR_MANIFEST,
        )

        registry = ToolsetRegistry()
        registry.register(GMAIL_MANIFEST)
        registry.register(GOOGLE_CALENDAR_MANIFEST)

        # What the coding agent actually does: search by what it wants to do.
        assert "gmail" in {c.toolset_id for c in registry.search("send email")}
        assert "google_calendar" in {
            c.toolset_id for c in registry.search("calendar event")
        }
        assert GMAIL_MANIFEST.qualified_id == "app:loom:gmail"
        assert GOOGLE_CALENDAR_MANIFEST.qualified_id == "app:loom:google_calendar"

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("send email", "gmail"),
            ("read inbox", "gmail"),
            ("search mail", "gmail"),
            ("attachment", "gmail"),
            ("schedule meeting", "google_calendar"),
            ("calendar event", "google_calendar"),
            ("check availability", "google_calendar"),
            ("invite attendees", "google_calendar"),
        ],
    )
    def test_search_finds_them_by_the_words_an_agent_would_use(
        self, query: str, expected: str
    ) -> None:
        """Tier-1 search scores manifest prose. Vocabulary the caller does not
        use is vocabulary that makes the toolset undiscoverable."""
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.google import (
            GMAIL_MANIFEST,
            GOOGLE_CALENDAR_MANIFEST,
        )

        registry = ToolsetRegistry()
        registry.register(GMAIL_MANIFEST)
        registry.register(GOOGLE_CALENDAR_MANIFEST)

        assert expected in {card.toolset_id for card in registry.search(query)}

    def test_destructive_operations_are_marked_destructive(self) -> None:
        """Effect class is what a read-only grant filters on."""
        from loom.toolsets.google import (
            GMAIL_MANIFEST,
            GOOGLE_CALENDAR_MANIFEST,
        )
        from loom.toolsets.manifest import EffectClass

        trash = GMAIL_MANIFEST.find_operation("messages.trash")
        delete = GOOGLE_CALENDAR_MANIFEST.find_operation("events.delete")
        assert trash is not None and trash.effect is EffectClass.DESTRUCTIVE
        assert delete is not None and delete.effect is EffectClass.DESTRUCTIVE

    def test_send_is_a_write_and_search_is_a_read(self) -> None:
        from loom.toolsets.google import GMAIL_MANIFEST
        from loom.toolsets.manifest import EffectClass

        send = GMAIL_MANIFEST.find_operation("messages.send")
        search = GMAIL_MANIFEST.find_operation("messages.search")
        assert send is not None and send.effect is EffectClass.WRITE
        assert search is not None and search.effect is EffectClass.READ

    def test_every_operation_declares_a_scope_and_a_summary(self) -> None:
        from loom.toolsets.google import (
            GMAIL_MANIFEST,
            GOOGLE_CALENDAR_MANIFEST,
        )

        for manifest in (GMAIL_MANIFEST, GOOGLE_CALENDAR_MANIFEST):
            for op in manifest.all_operations():
                assert op.summary, f"{op.id} has no summary"
                assert op.scopes, f"{op.id} declares no OAuth scope"

    def test_tool_docs_name_every_step(self) -> None:
        """The docs are what the coding agent reads; a missing tool is invisible."""
        from loom.toolsets.google.calendar import tools as cal_tools
        from loom.toolsets.google.gmail import tools as gmail_tools

        for module, docs in (
            (gmail_tools, gmail_tools.GMAIL_TOOL_DOCS),
            (cal_tools, cal_tools.CALENDAR_TOOL_DOCS),
        ):
            steps = [name for name in module.__all__ if not name.isupper()]
            missing = [name for name in steps if name not in docs]
            assert not missing, f"undocumented: {missing}"


# ---------------------------------------------------------------------------
# End to end, through a real Runtime
# ---------------------------------------------------------------------------


class TestThroughAWorkflow:
    async def test_steps_journal_and_replay_without_recalling_the_api(self) -> None:
        """The durability claim: a replay reads the journal, not Gmail."""
        recorder = Recorder(
            {
                "/messages?": {"messages": [{"id": "m1", "threadId": "t1"}]},
                "/messages/m1": MESSAGE,
                "/modify": {**MESSAGE, "labelIds": ["INBOX"]},
            }
        )
        client = GmailClient(token_auth(), transport=recorder.transport())

        from loom import step

        @step
        async def triage() -> list[str]:
            """Search and mark read, against the mock transport."""
            found = await client.search_messages("is:unread", 5)
            for message in found:
                await client.modify_labels(message.id, None, ["UNREAD"])
            return [m.subject for m in found]

        @workflow(name="triage_inbox")
        async def triage_inbox(ctx: Context, _input: str) -> list[str]:
            return await ctx.step(triage)

        runtime = Runtime(store=MemoryStore())
        runtime.register(triage_inbox)

        first = await runtime.run("triage_inbox", "go")
        calls_after_first = len(recorder.requests)

        replayed = await runtime.replay(first.run_id)

        assert first.output == ["Q3 numbers"]
        assert replayed.output == ["Q3 numbers"]
        assert len(recorder.requests) == calls_after_first, (
            "replay re-called the Gmail API; the step was not served from the journal"
        )

    async def test_a_permanent_error_fails_fast_instead_of_retrying(self) -> None:
        """Three attempts at a malformed query is three times the wait, same answer."""
        attempts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            return httpx.Response(400, json={"error": {"message": "bad query"}})

        client = GmailClient(token_auth(), transport=httpx.MockTransport(handler))

        from loom import step

        @step(retry=Retry(max_attempts=3, initial_delay=0.01))
        async def search() -> list[Any]:
            """Search with a query the API rejects."""
            return await client.search_messages("subject:(")

        @workflow(name="bad_search")
        async def bad_search(ctx: Context, _input: str) -> Any:
            return await ctx.step(search)

        runtime = Runtime(store=MemoryStore())
        runtime.register(bad_search)
        result = await runtime.run("bad_search", "go")

        assert result.status.value == "failed"
        assert len(attempts) == 1, f"retried a permanent error {len(attempts)} times"

    async def test_typed_models_survive_the_journal(self) -> None:
        """A step returning EmailMessage must replay as EmailMessage, not a dict."""
        recorder = Recorder({"/messages/m1": MESSAGE})
        client = GmailClient(token_auth(), transport=recorder.transport())

        from loom import step
        from loom.toolsets.google.gmail.models import EmailMessage

        @step
        async def fetch() -> EmailMessage:
            """Fetch one message."""
            return await client.get_message("m1")

        @workflow(name="fetch_one")
        async def fetch_one(ctx: Context, _input: str) -> str:
            message = await ctx.step(fetch)
            # Attribute access is the test: a dict would raise here on replay.
            return message.subject

        runtime = Runtime(store=MemoryStore())
        runtime.register(fetch_one)

        first = await runtime.run("fetch_one", "go")
        replayed = await runtime.replay(first.run_id)

        assert first.output == "Q3 numbers"
        assert replayed.output == "Q3 numbers"


class TestCalendarIdValidation:
    """A bad calendar id should say so, not fail inside urllib."""

    async def test_a_missing_calendar_id_names_the_argument(self) -> None:
        recorder = Recorder({"/events": {"items": []}})
        client = CalendarClient(token_auth(), transport=recorder.transport())

        with pytest.raises(ValueError) as exc:
            await client.list_events(calendar_id=None)  # type: ignore[arg-type]

        assert "calendar_id" in str(exc.value)
        assert "primary" in str(exc.value)
        assert not recorder.requests, "a bad id should not reach the network"

    async def test_an_empty_calendar_id_is_refused(self) -> None:
        client = CalendarClient(token_auth(), transport=Recorder().transport())
        with pytest.raises(ValueError):
            await client.list_events(calendar_id="")
