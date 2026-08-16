"""The Zoom toolset.

Everything runs against an ``httpx.MockTransport``, so what the test sees is the
request the client would put on the wire.

Two behaviours carry most of the weight, and both produce a *not found* rather
than an error when they are wrong — the hardest kind of bug to chase. **UUID
encoding**: a meeting uuid beginning with ``/`` or containing ``//`` must be
double-encoded, or Zoom answers "meeting does not exist" for a meeting that
does. And **the two identifiers**: the numeric id is the series, the uuid is one
occurrence, and past-meeting endpoints take the second.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from loom import Context, Runtime, workflow
from loom.core.retry import PERMANENT_ERRORS
from loom.stores.memory import MemoryStore
from loom.toolsets.zoom.auth import ZoomAuth, ZoomCredentials
from loom.toolsets.zoom.client import ZoomClient, encode_uuid, flatten_meeting
from loom.toolsets.zoom.errors import (
    ZoomAuthError,
    ZoomDailyLimitReached,
    ZoomPermanentError,
    ZoomRateLimited,
    classify,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def token_auth() -> ZoomAuth:
    """Auth that needs no token endpoint."""
    return ZoomAuth(ZoomCredentials(access_token="test-token"))


class Recorder:
    """Records requests and replays canned responses; a list is a page run."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = {
            k: list(v) if isinstance(v, list) else v
            for k, v in (responses or {}).items()
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for fragment, payload in self._responses.items():
            if fragment not in str(request.url):
                continue
            chosen = (
                (payload.pop(0) if len(payload) > 1 else payload[0])
                if isinstance(payload, list)
                else payload
            )
            if isinstance(chosen, httpx.Response):
                return chosen
            return httpx.Response(200, json=chosen)
        return httpx.Response(200, json={})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def query(self, index: int = -1) -> dict[str, str]:
        return dict(httpx.QueryParams(self.requests[index].url.query.decode()))

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)


def client(recorder: Recorder) -> ZoomClient:
    return ZoomClient(token_auth(), transport=recorder.transport())


MEETING = {
    "id": 81234567890,
    "uuid": "aDYlohsHRtCd4ii1uC2+hA==",
    "topic": "Design review",
    "type": 2,
    "start_time": "2026-03-02T09:00:00Z",
    "duration": 60,
    "timezone": "UTC",
    "join_url": "https://zoom.us/j/81234567890",
    "start_url": "https://zoom.us/s/81234567890?zak=SECRET-HOST-TOKEN",
    "host_email": "ada@example.com",
}

USER = {
    "id": "abc123",
    "email": "ada@example.com",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "display_name": "Ada Lovelace",
    "type": 2,
    "status": "active",
    "dept": "Engineering",
}


# ---------------------------------------------------------------------------
# The UUID rule
# ---------------------------------------------------------------------------


class TestUuidEncoding:
    """Zoom's documented rule, and the reason it is not optional.

    A meeting uuid is base64, so it can contain ``/``. Encoded once, a uuid
    that begins with ``/`` or contains ``//`` answers ``3001 Meeting does not
    exist`` — a *not found* for a meeting that plainly does, which sends the
    reader looking for the wrong bug entirely.
    """

    def test_an_ordinary_uuid_is_encoded_once(self) -> None:
        assert encode_uuid("aDYlohsHRtCd4ii1uC2+hA==") == (
            "aDYlohsHRtCd4ii1uC2%2BhA%3D%3D"
        )

    def test_a_leading_slash_is_encoded_twice(self) -> None:
        encoded = encode_uuid("/abc==")
        assert encoded.startswith("%252F")

    def test_a_double_slash_is_encoded_twice(self) -> None:
        assert encode_uuid("ab//cd").count("%252F") == 2

    def test_a_uuid_without_slashes_is_not_double_encoded(self) -> None:
        """Double-encoding one that does not need it produces the same 3001
        from the other direction."""
        assert "%25" not in encode_uuid("aDYlohsHRtCd4ii1uC2+hA==")

    @pytest.mark.parametrize("bad", ["", None, 123])
    def test_a_bad_uuid_names_the_argument_and_the_confusion(
        self, bad: Any
    ) -> None:
        with pytest.raises(ValueError) as exc:
            encode_uuid(bad)
        assert "meeting_uuid" in str(exc.value)
        assert "series" in str(exc.value)

    async def test_participants_use_the_encoded_uuid(self) -> None:
        recorder = Recorder({"/participants": {"participants": []}})
        await client(recorder).participants("/abc==")

        assert "%252F" in str(recorder.last.url)

    async def test_a_numeric_id_is_not_treated_as_a_uuid(self) -> None:
        """get_recording accepts both; only one needs the uuid dance."""
        recorder = Recorder({"/recordings": {"recording_files": []}})
        await client(recorder).get_recording(81234567890)

        assert "meetings/81234567890/recordings" in str(recorder.last.url)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_server_to_server_wins_over_a_ready_made_token(self) -> None:
        """An access token lives an hour; the client secret mints them
        indefinitely, and a workflow that sleeps outlives the first."""
        both = ZoomCredentials(
            account_id="a", client_id="c", client_secret="s", access_token="t"
        )
        assert both.mode == "server_to_server"
        assert ZoomCredentials(access_token="t").mode == "token"
        assert ZoomCredentials().mode == ""

    def test_partial_server_credentials_are_not_a_mode(self) -> None:
        assert ZoomCredentials(client_id="c", client_secret="s").mode == ""

    def test_missing_credentials_raise_with_instructions(self) -> None:
        with pytest.raises(ZoomAuthError) as exc:
            ZoomAuth(ZoomCredentials())
        assert "ZOOM_ACCOUNT_ID" in str(exc.value)
        assert "loom connect zoom" in str(exc.value)

    def test_from_env(self) -> None:
        creds = ZoomCredentials.from_env(
            {"ZOOM_ACCOUNT_ID": "a", "ZOOM_CLIENT_ID": "c", "ZOOM_CLIENT_SECRET": "s"}
        )
        assert creds.mode == "server_to_server"

    async def test_a_static_token_is_returned_verbatim(self) -> None:
        assert await token_auth().token() == "test-token"

    async def test_the_account_credentials_grant_is_sent_correctly(self) -> None:
        """Client id and secret go in a Basic header and the account id in the
        query string; putting the secret in the body is a 400 that reads as a
        bad account id."""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200, json={"access_token": "minted", "expires_in": 3600}
            )

        auth = ZoomAuth(
            ZoomCredentials(account_id="A1", client_id="cid", client_secret="sec"),
            transport=httpx.MockTransport(handler),
        )
        assert await auth.token() == "minted"

        request = calls[0]
        query = dict(httpx.QueryParams(request.url.query.decode()))
        assert query["grant_type"] == "account_credentials"
        assert query["account_id"] == "A1"
        expected = base64.b64encode(b"cid:sec").decode()
        assert request.headers["authorization"] == f"Basic {expected}"

    async def test_the_token_is_minted_once_and_cached(self) -> None:
        """Ten steps sharing an auth should mint one token, not ten."""
        import asyncio

        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(
                200, json={"access_token": "minted", "expires_in": 3600}
            )

        auth = ZoomAuth(
            ZoomCredentials(account_id="A1", client_id="c", client_secret="s"),
            transport=httpx.MockTransport(handler),
        )
        tokens = await asyncio.gather(*(auth.token() for _ in range(10)))

        assert set(tokens) == {"minted"}
        assert len(calls) == 1

    async def test_invalidate_forces_a_remint(self) -> None:
        minted = iter(["first", "second"])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"access_token": next(minted), "expires_in": 3600}
            )

        auth = ZoomAuth(
            ZoomCredentials(account_id="A1", client_id="c", client_secret="s"),
            transport=httpx.MockTransport(handler),
        )
        assert await auth.token() == "first"
        auth.invalidate()
        assert await auth.token() == "second"

    async def test_a_401_reauthenticates_once_then_succeeds(self) -> None:
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(1)
            if len(seen) == 1:
                return httpx.Response(401, json={"code": 124, "message": "expired"})
            return httpx.Response(200, json=MEETING)

        zoom = ZoomClient(token_auth(), transport=httpx.MockTransport(handler))
        assert (await zoom.get_meeting(1)).topic == "Design review"
        assert len(seen) == 2

    async def test_a_connected_credential_is_preferred(self) -> None:
        from loom.connectors.credentials import (
            MemoryCredentialStore,
            StoredCredential,
            credential_store_scope,
        )
        from loom.core.secret import Secret

        store = MemoryCredentialStore()
        await store.put("zoom", StoredCredential(token=Secret("connected")))

        auth = ZoomAuth(ZoomCredentials(access_token="from-env"))
        with credential_store_scope(store):
            assert await auth.token() == "connected"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    def test_a_daily_limit_is_not_retryable(self) -> None:
        """The distinction that matters: a per-second limit clears while a step
        backs off, a daily one does not clear until midnight UTC — so retrying
        against it burns the run's time to reach the same answer."""
        daily = classify(
            429, {"message": "You have reached the maximum daily rate limit."}
        )
        assert isinstance(daily, ZoomDailyLimitReached)
        assert isinstance(daily, PERMANENT_ERRORS)

    def test_a_per_second_limit_is_retryable(self) -> None:
        per_second = classify(
            429, {"message": "You have reached the maximum per-second rate limit."}
        )
        assert isinstance(per_second, ZoomRateLimited)
        assert not isinstance(per_second, PERMANENT_ERRORS)

    def test_the_body_code_beats_the_status_for_auth(self) -> None:
        """Zoom answers 200-shaped bodies with code 124 in some places."""
        assert isinstance(classify(200, {"code": 124}), ZoomAuthError)
        assert isinstance(classify(401, {}), ZoomAuthError)

    def test_a_missing_meeting_is_permanent(self) -> None:
        error = classify(404, {"code": 3001, "message": "Meeting does not exist"})
        assert isinstance(error, ZoomPermanentError)
        assert error.code == 3001

    def test_a_server_error_is_retryable(self) -> None:
        assert not isinstance(classify(503, {}), PERMANENT_ERRORS)

    def test_the_message_survives_a_non_json_body(self) -> None:
        assert "bad gateway" in str(classify(502, "bad gateway"))


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    async def test_the_next_page_token_is_followed(self) -> None:
        recorder = Recorder(
            {
                "/meetings": [
                    {"meetings": [MEETING], "next_page_token": "tok2"},
                    {"meetings": [{**MEETING, "id": 2}], "next_page_token": ""},
                ]
            }
        )
        found = await client(recorder).list_meetings(max_results=10)

        assert [m.id for m in found] == [81234567890, 2]
        assert found.complete is True
        assert recorder.query(1)["next_page_token"] == "tok2"

    async def test_an_empty_token_means_exhausted(self) -> None:
        """Zoom signals the end with "" rather than by omitting the field."""
        recorder = Recorder({"/meetings": {"meetings": [MEETING], "next_page_token": ""}})
        found = await client(recorder).list_meetings(max_results=50)

        assert found.complete is True
        assert len(recorder.requests) == 1

    async def test_a_truncated_read_says_so(self) -> None:
        recorder = Recorder(
            {
                "/meetings": {
                    "meetings": [MEETING, {**MEETING, "id": 2}],
                    "next_page_token": "more",
                    "total_records": 97,
                }
            }
        )
        found = await client(recorder).list_meetings(max_results=2)

        assert found.complete is False
        assert found.total == 97
        assert found.summary() == "2 of 97"

    async def test_page_size_is_zooms_spelling(self) -> None:
        recorder = Recorder({"/meetings": {"meetings": []}})
        await client(recorder).list_meetings(max_results=50)

        assert recorder.query()["page_size"] == "50"

    async def test_a_light_endpoint_is_capped_at_three_hundred(self) -> None:
        """Exceeding Zoom's ceiling is a 400, not a clamp."""
        recorder = Recorder({"/meetings": {"meetings": []}})
        await client(recorder).list_meetings(max_results=5000)

        assert int(recorder.query()["page_size"]) == 300

    async def test_a_heavy_endpoint_is_capped_lower(self) -> None:
        recorder = Recorder({"/recordings": {"meetings": []}})
        await client(recorder).list_recordings(max_results=5000)

        assert int(recorder.query()["page_size"]) == 100

    async def test_participants_page(self) -> None:
        recorder = Recorder(
            {
                "/participants": [
                    {"participants": [{"name": "Ada"}], "next_page_token": "p2"},
                    {"participants": [{"name": "Bob"}], "next_page_token": ""},
                ]
            }
        )
        found = await client(recorder).participants("uuid1", max_results=10)

        assert [p.name for p in found] == ["Ada", "Bob"]


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


class TestMeetings:
    async def test_creating_sends_the_scheduling_shape(self) -> None:
        recorder = Recorder({"/meetings": MEETING})
        await client(recorder).create_meeting(
            "Design review", start_time="2026-03-02T09:00:00Z", duration=60
        )

        body = recorder.body()
        assert body == {
            "topic": "Design review",
            "type": 2,
            "duration": 60,
            "timezone": "UTC",
            "start_time": "2026-03-02T09:00:00Z",
        }
        assert "users/me/meetings" in str(recorder.last.url)

    async def test_optional_fields_are_omitted_not_blanked(self) -> None:
        recorder = Recorder({"/meetings": MEETING})
        await client(recorder).create_meeting("Quick sync")

        assert "agenda" not in recorder.body()
        assert "password" not in recorder.body()
        assert "start_time" not in recorder.body()

    async def test_settings_pass_through(self) -> None:
        recorder = Recorder({"/meetings": MEETING})
        await client(recorder).create_meeting(
            "Recorded", settings={"auto_recording": "cloud"}
        )

        assert recorder.body()["settings"] == {"auto_recording": "cloud"}

    async def test_updating_returns_the_id_because_zoom_returns_nothing(
        self,
    ) -> None:
        recorder = Recorder({"/meetings/81234567890": httpx.Response(204)})
        assert await client(recorder).update_meeting(81234567890, {"topic": "New"}) == (
            81234567890
        )
        assert recorder.last.method == "PATCH"

    async def test_nobody_is_emailed_on_delete_by_default(self) -> None:
        """Cancelling a hundred stale meetings should not mail a hundred sets
        of attendees as a side effect of a default."""
        recorder = Recorder({"/meetings/1": httpx.Response(204)})
        await client(recorder).delete_meeting(1)

        assert recorder.query()["schedule_for_reminder"] == "false"

    async def test_notify_is_passed_through(self) -> None:
        recorder = Recorder({"/meetings/1": httpx.Response(204)})
        await client(recorder).delete_meeting(1, notify=True)

        assert recorder.query()["schedule_for_reminder"] == "true"

    def test_the_start_url_is_flagged_as_a_host_credential(self) -> None:
        """It embeds a host token — anyone who opens it runs the meeting."""
        from loom.toolsets.zoom.models import ZoomMeeting

        described = ZoomMeeting.model_fields["start_url"].description or ""
        source = ZoomMeeting.__doc__ or ""
        combined = (described + source + (ZoomMeeting.model_json_schema().get(
            "properties", {}).get("start_url", {}).get("description", "")))
        assert "host" in combined.lower() or "secret" in combined.lower()

    def test_both_identifiers_survive_flattening(self) -> None:
        meeting = flatten_meeting(MEETING)

        assert meeting.id == 81234567890
        assert meeting.uuid == "aDYlohsHRtCd4ii1uC2+hA=="


class TestParticipants:
    async def test_a_rejoin_is_two_rows_not_one(self) -> None:
        """One row per session. Summing duration by name double-counts."""
        recorder = Recorder(
            {
                "/participants": {
                    "participants": [
                        {"name": "Ada", "user_email": "ada@example.com",
                         "duration": 600},
                        {"name": "Ada", "user_email": "ada@example.com",
                         "duration": 300},
                    ],
                    "next_page_token": "",
                }
            }
        )
        found = await client(recorder).participants("uuid1")

        assert len(found) == 2
        assert {p.email for p in found} == {"ada@example.com"}

    async def test_an_anonymous_guest_has_no_id(self) -> None:
        """Common, and why attendance cannot always be matched to a directory
        user."""
        recorder = Recorder(
            {"/participants": {"participants": [{"name": "Guest"}],
                               "next_page_token": ""}}
        )
        found = await client(recorder).participants("uuid1")

        assert found[0].id == ""
        assert found[0].name == "Guest"


class TestRecordings:
    async def test_files_are_flattened_and_readiness_is_explicit(self) -> None:
        recorder = Recorder(
            {
                "/recordings": {
                    "id": 1,
                    "uuid": "u1",
                    "topic": "Review",
                    "recording_files": [
                        {"id": "f1", "file_type": "MP4", "status": "completed",
                         "download_url": "https://zoom.us/rec/download/x",
                         "file_size": 1024},
                        {"id": "f2", "file_type": "TRANSCRIPT",
                         "status": "processing"},
                    ],
                }
            }
        )
        recording = await client(recorder).get_recording(1)

        assert [f.file_type for f in recording.files] == ["MP4", "TRANSCRIPT"]
        assert recording.files[0].is_ready is True
        assert recording.files[1].is_ready is False

    async def test_a_completed_file_with_no_url_is_not_ready(self) -> None:
        recorder = Recorder(
            {
                "/recordings": {
                    "recording_files": [{"id": "f1", "status": "completed"}]
                }
            }
        )
        recording = await client(recorder).get_recording(1)

        assert recording.files[0].is_ready is False

    async def test_the_window_is_passed_through(self) -> None:
        recorder = Recorder({"/recordings": {"meetings": []}})
        await client(recorder).list_recordings(start="2026-03-01", end="2026-03-31")

        assert recorder.query()["from"] == "2026-03-01"
        assert recorder.query()["to"] == "2026-03-31"

    async def test_downloading_sends_the_bearer_token(self) -> None:
        """The URL is not public; a plain fetch of it does not work."""
        recorder = Recorder(
            {"rec/download": httpx.Response(200, content=b"video-bytes")}
        )
        from loom.toolsets.zoom import auth as zoom_auth

        zoom_auth._default = token_auth()
        try:
            attachment = await client(recorder).download_recording(
                "https://zoom.us/rec/download/x", "meeting.mp4"
            )
            assert attachment.data == b"video-bytes"
            assert recorder.last.headers["authorization"] == "Bearer test-token"
        finally:
            zoom_auth._default = None


class TestUsers:
    async def test_an_unknown_email_is_none_not_a_raise(self) -> None:
        """"No Zoom account for that address" is an ordinary answer."""
        recorder = Recorder(
            {"/users/": httpx.Response(404, json={"code": 1001, "message": "not found"})}
        )
        assert await client(recorder).find_user_by_email("nope@example.com") is None

    async def test_a_real_failure_still_raises(self) -> None:
        recorder = Recorder(
            {"/users/": httpx.Response(403, json={"code": 3000, "message": "no scope"})}
        )
        with pytest.raises(ZoomAuthError):
            await client(recorder).find_user_by_email("ada@example.com")

    async def test_a_found_email_returns_the_user(self) -> None:
        recorder = Recorder({"/users/": USER})
        found = await client(recorder).find_user_by_email("ada@example.com")

        assert found is not None
        assert found.id == "abc123"
        assert found.department == "Engineering"

    async def test_an_email_is_percent_encoded_into_the_path(self) -> None:
        recorder = Recorder({"/users/": USER})
        await client(recorder).get_user("ada+test@example.com")

        assert "ada%2Btest%40example.com" in str(recorder.last.url)


# ---------------------------------------------------------------------------
# Through a real Runtime
# ---------------------------------------------------------------------------


class TestThroughAWorkflow:
    async def test_a_paged_read_keeps_its_coverage_across_the_journal(self) -> None:
        from loom.toolsets.zoom import client as zoom_client
        from loom.toolsets.zoom.tools import zoom_list_meetings

        recorder = Recorder(
            {
                "/meetings": {
                    "meetings": [MEETING, {**MEETING, "id": 2}],
                    "next_page_token": "more",
                    "total_records": 40,
                }
            }
        )
        zoom_client._default_client = ZoomClient(
            token_auth(), transport=recorder.transport()
        )

        @workflow(name="zoom-coverage")
        async def flow(ctx: Context, _input: Any) -> dict[str, Any]:
            found = await ctx.step(zoom_list_meetings, "me", "scheduled", 2)
            return {"count": len(found), "complete": found.complete,
                    "summary": found.summary(), "first": found[0].topic}

        try:
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.output == {
                "count": 2, "complete": False, "summary": "2 of 40",
                "first": "Design review",
            }

            calls = len(recorder.requests)
            replayed = await runtime.replay(result.run_id)
            assert replayed.output == result.output
            assert len(recorder.requests) == calls, "replay re-called the API"
        finally:
            zoom_client._default_client = None

    async def test_creating_a_meeting_is_not_retried(self) -> None:
        """A retry schedules a second meeting with a different join link, and
        nothing ties the two together."""
        from loom.toolsets.zoom import client as zoom_client
        from loom.toolsets.zoom.tools import zoom_create_meeting

        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503, json={"message": "unavailable"})

        zoom_client._default_client = ZoomClient(
            token_auth(), transport=httpx.MockTransport(handler)
        )

        @workflow(name="zoom-create")
        async def flow(ctx: Context, _input: Any) -> str:
            meeting = await ctx.step(zoom_create_meeting, "Review")
            return meeting.join_url

        try:
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.status.value == "failed"
            assert len(attempts) == 1
        finally:
            zoom_client._default_client = None

    async def test_a_daily_limit_stops_rather_than_backing_off(self) -> None:
        """It does not clear until midnight UTC, so retrying inside the run
        cannot help — the run should fail and be rescheduled."""
        from loom.toolsets.zoom import client as zoom_client
        from loom.toolsets.zoom.tools import zoom_list_meetings

        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(
                429, json={"message": "You have reached the maximum daily rate limit."}
            )

        zoom_client._default_client = ZoomClient(
            token_auth(), transport=httpx.MockTransport(handler)
        )

        @workflow(name="zoom-daily")
        async def flow(ctx: Context, _input: Any) -> int:
            return len(await ctx.step(zoom_list_meetings))

        try:
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.status.value == "failed"
            assert len(attempts) == 1, "a daily limit must not be retried"
        finally:
            zoom_client._default_client = None

    async def test_typed_models_survive_the_journal(self) -> None:
        from loom.toolsets.zoom import client as zoom_client
        from loom.toolsets.zoom.tools import zoom_get_meeting

        recorder = Recorder({"/meetings/1": MEETING})
        zoom_client._default_client = ZoomClient(
            token_auth(), transport=recorder.transport()
        )

        @workflow(name="zoom-models")
        async def flow(ctx: Context, _input: Any) -> dict[str, Any]:
            first = await ctx.step(zoom_get_meeting, 1)
            again = await ctx.step(zoom_get_meeting, 1)
            return {"uuid": again.uuid, "duration": again.duration,
                    "same": first.id == again.id}

        try:
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.output == {
                "uuid": "aDYlohsHRtCd4ii1uC2+hA==", "duration": 60, "same": True,
            }
        finally:
            zoom_client._default_client = None


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_it_registers_and_is_findable(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.zoom import ZOOM_MANIFEST

        registry = ToolsetRegistry()
        registry.register(ZOOM_MANIFEST)

        assert ZOOM_MANIFEST.qualified_id == "app:loom:zoom"
        assert "zoom" in {c.toolset_id for c in registry.search("schedule a meeting")}

    def test_the_resolver_is_declared(self) -> None:
        from loom.toolsets.zoom import ZOOM_MANIFEST

        assert ZOOM_MANIFEST.resolvers()["user"].function == "zoom_find_user_by_email"

    def test_destructive_operations_are_marked(self) -> None:
        from loom.toolsets.manifest import EffectClass
        from loom.toolsets.zoom import ZOOM_MANIFEST

        assert (
            ZOOM_MANIFEST.find_operation("meetings.delete").effect
            is EffectClass.DESTRUCTIVE
        )
        assert (
            ZOOM_MANIFEST.find_operation("recordings.delete").effect
            is EffectClass.DESTRUCTIVE
        )
        assert (
            ZOOM_MANIFEST.find_operation("meetings.create").effect is EffectClass.WRITE
        )

    def test_creating_is_not_marked_idempotent(self) -> None:
        from loom.toolsets.zoom import ZOOM_MANIFEST

        assert not ZOOM_MANIFEST.find_operation("meetings.create").idempotent
        assert ZOOM_MANIFEST.find_operation("meetings.update").idempotent

    def test_past_operations_ask_for_the_uuid_in_their_schema(self) -> None:
        """The manifest is what the coding agent reads, so the id/uuid trap
        has to be stated there and not only in the docstring."""
        from loom.toolsets.zoom import ZOOM_MANIFEST

        for op_id in ("past.get", "past.participants"):
            spec = ZOOM_MANIFEST.find_operation(op_id)
            described = spec.input_schema["properties"]["meeting_uuid"]["description"]
            assert "occurrence" in described.lower(), op_id

    def test_every_operation_declares_a_scope_and_a_summary(self) -> None:
        from loom.toolsets.zoom import ZOOM_MANIFEST

        for op in ZOOM_MANIFEST.all_operations():
            assert op.summary, f"{op.id} has no summary"
            assert op.scopes, f"{op.id} declares no scope"

    def test_the_paged_reads_are_exactly_the_ones_that_page(self) -> None:
        from loom.toolsets.zoom import ZOOM_MANIFEST

        assert {op.function for op in ZOOM_MANIFEST.paginated()} == {
            "zoom_list_meetings",
            "zoom_list_participants",
            "zoom_list_recordings",
            "zoom_list_users",
        }

    def test_tool_docs_name_every_step(self) -> None:
        from loom.toolsets.zoom import tools

        steps = [n for n in tools.__all__ if not n.isupper()]
        missing = [n for n in steps if n not in tools.ZOOM_TOOL_DOCS]
        assert not missing, f"undocumented: {missing}"

    def test_the_docs_warn_about_both_traps(self) -> None:
        from loom.toolsets.zoom import tools

        docs = tools.ZOOM_TOOL_DOCS
        assert "start_url" in docs, "nothing warns that start_url is a credential"
        assert "OCCURRENCE" in docs, "nothing distinguishes the id from the uuid"


# ---------------------------------------------------------------------------
# The operations the earlier classes did not reach
# ---------------------------------------------------------------------------


class TestRemainingOperations:
    async def test_past_meeting_details_use_the_encoded_uuid(self) -> None:
        recorder = Recorder(
            {"past_meetings": {"uuid": "u1", "topic": "Review",
                               "participants_count": 4}}
        )
        details = await client(recorder).past_meeting("/u1//x")

        assert details["participants_count"] == 4
        assert "%252F" in str(recorder.last.url)

    async def test_deleting_a_recording_returns_the_id(self) -> None:
        recorder = Recorder({"/recordings": httpx.Response(204)})
        assert await client(recorder).delete_recording(81234567890) == 81234567890
        assert recorder.last.method == "DELETE"

    async def test_listing_users_filters_by_status(self) -> None:
        recorder = Recorder({"/users": {"users": [USER], "next_page_token": ""}})
        found = await client(recorder).list_users(status="inactive")

        assert recorder.query()["status"] == "inactive"
        assert found[0].email == "ada@example.com"

    async def test_get_user_defaults_to_me(self) -> None:
        recorder = Recorder({"/users/": USER})
        await client(recorder).get_user()

        assert str(recorder.last.url).endswith("/users/me")

    async def test_a_204_is_not_decoded_as_json(self) -> None:
        """Zoom answers 204 for updates and deletes; decoding an empty body
        would raise several frames from the cause."""
        recorder = Recorder({"/meetings/1": httpx.Response(204)})
        assert await client(recorder).update_meeting(1, {"topic": "x"}) == 1


class TestConfigurableTimeouts:
    """A cloud recording is hundreds of megabytes and takes minutes."""

    def test_the_api_timeout_reaches_the_session(self) -> None:
        zoom = ZoomClient(token_auth(), timeout=5.0)
        assert zoom._session._timeout == 5.0

    def test_transfers_have_their_own_longer_default(self) -> None:
        from loom.toolsets.zoom.client import (
            DEFAULT_TIMEOUT,
            DEFAULT_TRANSFER_TIMEOUT,
        )

        assert DEFAULT_TRANSFER_TIMEOUT > DEFAULT_TIMEOUT
        assert ZoomClient(token_auth())._transfer_timeout == DEFAULT_TRANSFER_TIMEOUT

    def test_the_transfer_timeout_is_settable(self) -> None:
        zoom = ZoomClient(token_auth(), transfer_timeout=900.0)
        assert zoom._transfer_timeout == 900.0
