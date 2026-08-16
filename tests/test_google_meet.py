"""The Google Meet toolset.

Everything here runs against an ``httpx.MockTransport``, so the request the test
sees is the one the client would put on the wire.

Two themes carry most of the file. **Resource names**: Meet identifies every
object by a path rather than an id, callers hold whichever half was printed to
them, and passing the wrong half produces a 404 naming a URL nobody built. And
**the participant union**: the API returns exactly one of three differently
shaped user objects, so reaching for the wrong one yields ``None`` rather than
an error — an attendance report that silently omits everyone who dialled in.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from loom import Context, Runtime, workflow
from loom.stores.memory import MemoryStore
from loom.toolsets.google.auth import GoogleAuth, GoogleCredentials
from loom.toolsets.google.meet.client import MeetClient, flatten_space

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def token_auth() -> GoogleAuth:
    return GoogleAuth(GoogleCredentials(access_token="test-token"))


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


def client(recorder: Recorder) -> MeetClient:
    return MeetClient(token_auth(), transport=recorder.transport())


SPACE = {
    "name": "spaces/jQCFfuBOdN5z",
    "meetingUri": "https://meet.google.com/abc-mnop-xyz",
    "meetingCode": "abc-mnop-xyz",
    "config": {"accessType": "TRUSTED", "entryPointAccess": "ALL"},
}

RECORD = {
    "name": "conferenceRecords/cr1",
    "space": "spaces/jQCFfuBOdN5z",
    "startTime": "2026-03-02T09:00:00Z",
    "endTime": "2026-03-02T09:45:00Z",
    "expireTime": "2026-04-01T00:00:00Z",
}

TRANSCRIPT_NAME = "conferenceRecords/cr1/transcripts/t1"


# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------


class TestSpaces:
    async def test_create_returns_the_link_a_human_needs(self) -> None:
        recorder = Recorder({"/spaces": SPACE})
        space = await client(recorder).create_space()

        assert space.meeting_uri == "https://meet.google.com/abc-mnop-xyz"
        assert space.meeting_code == "abc-mnop-xyz"
        assert space.name == "spaces/jQCFfuBOdN5z"

    async def test_create_with_no_access_type_sends_no_config(self) -> None:
        """Sending an empty config block would override the account default
        with whatever the API treats as unset."""
        recorder = Recorder({"/spaces": SPACE})
        await client(recorder).create_space()

        assert recorder.body() == {}

    async def test_an_access_type_is_nested_under_config(self) -> None:
        recorder = Recorder({"/spaces": SPACE})
        await client(recorder).create_space("RESTRICTED")

        assert recorder.body() == {"config": {"accessType": "RESTRICTED"}}

    async def test_a_meeting_code_is_accepted_where_a_path_is_expected(self) -> None:
        """The code is the only form anyone has to hand — it is what is printed
        in an invitation. Requiring the path means every caller rebuilds it."""
        recorder = Recorder({"/spaces": SPACE})
        await client(recorder).get_space("abc-mnop-xyz")

        assert str(recorder.last.url).endswith("/v2/spaces/abc-mnop-xyz")

    async def test_a_full_resource_path_is_not_doubled(self) -> None:
        recorder = Recorder({"/spaces": SPACE})
        await client(recorder).get_space("spaces/jQCFfuBOdN5z")

        assert str(recorder.last.url).endswith("/v2/spaces/jQCFfuBOdN5z")
        assert "spaces/spaces" not in str(recorder.last.url)

    async def test_a_wrong_collection_is_refused_rather_than_404ing(self) -> None:
        recorder = Recorder()
        with pytest.raises(ValueError, match="spaces"):
            await client(recorder).get_space("conferenceRecords/cr1")

    async def test_an_empty_name_names_the_argument(self) -> None:
        recorder = Recorder()
        with pytest.raises(ValueError, match="spaces"):
            await client(recorder).get_space("")

    async def test_active_conference_is_how_live_is_known(self) -> None:
        """The only field that answers "is this meeting happening now"."""
        live = {**SPACE, "activeConference": {"conferenceRecord": "conferenceRecords/x"}}
        assert flatten_space(live).active_conference == "conferenceRecords/x"
        assert flatten_space(SPACE).active_conference == ""

    async def test_update_sends_only_the_mask_for_what_changed(self) -> None:
        """A patch carrying the whole config resets entryPointAccess every time
        someone only meant to change the access type."""
        recorder = Recorder({"/spaces": SPACE})
        await client(recorder).update_space("spaces/s1", access_type="OPEN")

        assert recorder.query()["updateMask"] == "config.accessType"
        assert recorder.body() == {"config": {"accessType": "OPEN"}}

    async def test_updating_both_masks_both(self) -> None:
        recorder = Recorder({"/spaces": SPACE})
        await client(recorder).update_space(
            "spaces/s1", access_type="OPEN", entry_point_access="ALL"
        )

        assert recorder.query()["updateMask"] == (
            "config.accessType,config.entryPointAccess"
        )

    async def test_an_empty_update_is_refused(self) -> None:
        """An empty mask is a request that changes nothing and reports success."""
        recorder = Recorder()
        with pytest.raises(ValueError, match="update mask"):
            await client(recorder).update_space("spaces/s1")

        assert not recorder.requests

    async def test_ending_a_conference_hits_the_custom_verb(self) -> None:
        recorder = Recorder()
        await client(recorder).end_active_conference("abc-mnop-xyz")

        assert str(recorder.last.url).endswith(
            "/spaces/abc-mnop-xyz:endActiveConference"
        )
        assert recorder.last.method == "POST"


# ---------------------------------------------------------------------------
# Conference records
# ---------------------------------------------------------------------------


class TestConferenceRecords:
    async def test_page_size_is_meets_spelling(self) -> None:
        """Meet reads pageSize; maxResults is ignored rather than rejected."""
        recorder = Recorder({"/conferenceRecords": {"conferenceRecords": [RECORD]}})
        await client(recorder).list_conference_records(max_results=25)

        query = recorder.query()
        assert query["pageSize"] == "25"
        assert "maxResults" not in query

    async def test_a_page_beyond_the_ceiling_is_capped(self) -> None:
        recorder = Recorder({"/conferenceRecords": {"conferenceRecords": [RECORD]}})
        await client(recorder).list_conference_records(max_results=999)

        assert int(recorder.query()["pageSize"]) == 50

    async def test_the_filter_is_passed_through(self) -> None:
        recorder = Recorder({"/conferenceRecords": {"conferenceRecords": []}})
        await client(recorder).list_conference_records(
            'space.meeting_code = "abc-mnop-xyz"'
        )

        assert recorder.query()["filter"] == 'space.meeting_code = "abc-mnop-xyz"'

    async def test_no_filter_sends_no_empty_parameter(self) -> None:
        recorder = Recorder({"/conferenceRecords": {"conferenceRecords": []}})
        await client(recorder).list_conference_records()

        assert "filter" not in recorder.query()

    async def test_pages_are_followed(self) -> None:
        recorder = Recorder(
            {
                "/conferenceRecords": [
                    {"conferenceRecords": [RECORD], "nextPageToken": "p2"},
                    {"conferenceRecords": [{**RECORD, "name": "conferenceRecords/cr2"}]},
                ]
            }
        )
        found = await client(recorder).list_conference_records(max_results=10)

        assert [r.name for r in found] == [
            "conferenceRecords/cr1",
            "conferenceRecords/cr2",
        ]
        assert found.complete is True
        assert recorder.query(1)["pageToken"] == "p2"

    async def test_a_truncated_listing_says_so(self) -> None:
        recorder = Recorder(
            {"/conferenceRecords": {"conferenceRecords": [RECORD], "nextPageToken": "p"}}
        )
        found = await client(recorder).list_conference_records(max_results=1)

        assert found.complete is False

    async def test_a_call_still_running_has_no_end_time(self) -> None:
        """Recordings and transcripts do not exist yet at that point."""
        recorder = Recorder(
            {"/conferenceRecords": {"conferenceRecords": [{**RECORD, "endTime": ""}]}}
        )
        found = await client(recorder).list_conference_records()

        assert found[0].in_progress is True

    async def test_a_finished_call_is_not_in_progress(self) -> None:
        recorder = Recorder({"/conferenceRecords": {"conferenceRecords": [RECORD]}})
        found = await client(recorder).list_conference_records()

        assert found[0].in_progress is False

    async def test_get_accepts_a_bare_id(self) -> None:
        recorder = Recorder({"/conferenceRecords": RECORD})
        await client(recorder).get_conference_record("cr1")

        assert str(recorder.last.url).endswith("/v2/conferenceRecords/cr1")


# ---------------------------------------------------------------------------
# Participants — the three-way union
# ---------------------------------------------------------------------------


class TestParticipants:
    def _listing(self, *participants: dict[str, Any]) -> Recorder:
        return Recorder({"/participants": {"participants": list(participants)}})

    async def test_a_signed_in_user_keeps_their_directory_identity(self) -> None:
        recorder = self._listing(
            {
                "name": "conferenceRecords/cr1/participants/p1",
                "signedinUser": {"user": "users/123", "displayName": "Ada Lovelace"},
                "earliestStartTime": "2026-03-02T09:00:00Z",
                "latestEndTime": "2026-03-02T09:45:00Z",
            }
        )
        found = await client(recorder).list_participants("conferenceRecords/cr1")

        assert found[0].kind == "signed_in"
        assert found[0].display_name == "Ada Lovelace"
        assert found[0].identifier == "users/123"

    async def test_an_anonymous_guest_is_still_an_attendee(self) -> None:
        """Reaching only for signedinUser drops them from the report entirely,
        with no error to say the count is short."""
        recorder = self._listing(
            {
                "name": "conferenceRecords/cr1/participants/p2",
                "anonymousUser": {"displayName": "Guest"},
            }
        )
        found = await client(recorder).list_participants("conferenceRecords/cr1")

        assert found[0].kind == "anonymous"
        assert found[0].display_name == "Guest"
        assert found[0].identifier == "", "an anonymous user has no stable identity"

    async def test_a_dial_in_participant_is_flagged_as_phone(self) -> None:
        recorder = self._listing(
            {
                "name": "conferenceRecords/cr1/participants/p3",
                "phoneUser": {"displayName": "+1 555 0100"},
            }
        )
        found = await client(recorder).list_participants("conferenceRecords/cr1")

        assert found[0].kind == "phone"
        assert found[0].identifier == "+1 555 0100"

    async def test_all_three_kinds_arrive_in_one_list(self) -> None:
        recorder = self._listing(
            {"name": "a", "signedinUser": {"user": "users/1", "displayName": "Ada"}},
            {"name": "b", "anonymousUser": {"displayName": "Guest"}},
            {"name": "c", "phoneUser": {"displayName": "+1 555 0100"}},
        )
        found = await client(recorder).list_participants("conferenceRecords/cr1")

        assert [p.kind for p in found] == ["signed_in", "anonymous", "phone"]
        assert len(found) == 3, "an attendance count must include everyone"

    async def test_a_participant_with_no_user_object_does_not_crash(self) -> None:
        """A shape this code has not seen must degrade, not raise — the caller
        wanted a headcount, not an exception."""
        recorder = self._listing({"name": "conferenceRecords/cr1/participants/p4"})
        found = await client(recorder).list_participants("conferenceRecords/cr1")

        assert found[0].kind == "anonymous"
        assert found[0].display_name == ""

    async def test_participants_page_at_their_own_ceiling(self) -> None:
        recorder = self._listing()
        await client(recorder).list_participants("conferenceRecords/cr1", 900)

        assert int(recorder.query()["pageSize"]) == 100

    async def test_the_record_path_is_normalised(self) -> None:
        recorder = self._listing()
        await client(recorder).list_participants("cr1")

        assert "/conferenceRecords/cr1/participants" in str(recorder.last.url)


# ---------------------------------------------------------------------------
# Recordings and transcripts
# ---------------------------------------------------------------------------


class TestArtifacts:
    async def test_a_recording_exposes_its_drive_file(self) -> None:
        """The seam to the Drive toolset: this id goes to drive_download_file."""
        recorder = Recorder(
            {
                "/recordings": {
                    "recordings": [
                        {
                            "name": "conferenceRecords/cr1/recordings/r1",
                            "state": "FILE_GENERATED",
                            "driveDestination": {
                                "file": "driveFile123",
                                "exportUri": "https://drive.google.com/file/d/x",
                            },
                        }
                    ]
                }
            }
        )
        found = await client(recorder).list_recordings("conferenceRecords/cr1")

        assert found[0].drive_file_id == "driveFile123"
        assert found[0].is_ready is True

    async def test_a_recording_that_has_only_ended_is_not_ready(self) -> None:
        """Meet reports the recording as soon as it stops; the Drive file
        appears later. Acting on it in between finds nothing there."""
        recorder = Recorder(
            {"/recordings": {"recordings": [{"name": "r", "state": "ENDED"}]}}
        )
        found = await client(recorder).list_recordings("conferenceRecords/cr1")

        assert found[0].is_ready is False
        assert found[0].drive_file_id == ""

    async def test_a_generated_state_without_a_file_is_still_not_ready(self) -> None:
        recorder = Recorder(
            {
                "/recordings": {
                    "recordings": [{"name": "r", "state": "FILE_GENERATED"}]
                }
            }
        )
        found = await client(recorder).list_recordings("conferenceRecords/cr1")

        assert found[0].is_ready is False

    async def test_a_transcript_exposes_its_docs_id(self) -> None:
        recorder = Recorder(
            {
                "/transcripts": {
                    "transcripts": [
                        {
                            "name": TRANSCRIPT_NAME,
                            "state": "FILE_GENERATED",
                            "docsDestination": {"document": "doc456"},
                        }
                    ]
                }
            }
        )
        found = await client(recorder).list_transcripts("conferenceRecords/cr1")

        assert found[0].document_id == "doc456"
        assert found[0].is_ready is True

    async def test_transcript_entries_take_the_full_path(self) -> None:
        recorder = Recorder({"/entries": {"transcriptEntries": []}})
        await client(recorder).list_transcript_entries(TRANSCRIPT_NAME)

        assert str(recorder.last.url).startswith(
            f"https://meet.googleapis.com/v2/{TRANSCRIPT_NAME}/entries"
        )

    async def test_a_bare_transcript_id_is_refused_with_the_shape_it_wants(
        self,
    ) -> None:
        """A transcript is named *under* its conference record, so a bare id
        identifies nothing — and the API's own answer is an unhelpful 404."""
        recorder = Recorder()
        with pytest.raises(ValueError, match="conferenceRecords"):
            await client(recorder).list_transcript_entries("t1")

        assert not recorder.requests

    async def test_entries_carry_the_speaker_and_the_text(self) -> None:
        recorder = Recorder(
            {
                "/entries": {
                    "transcriptEntries": [
                        {
                            "name": f"{TRANSCRIPT_NAME}/entries/e1",
                            "participant": "conferenceRecords/cr1/participants/p1",
                            "text": "Let's start with the numbers.",
                            "languageCode": "en-US",
                            "startTime": "2026-03-02T09:00:05Z",
                        }
                    ]
                }
            }
        )
        found = await client(recorder).list_transcript_entries(TRANSCRIPT_NAME)

        assert found[0].text == "Let's start with the numbers."
        assert found[0].participant.endswith("participants/p1")

    async def test_a_long_transcript_reports_that_it_was_cut_off(self) -> None:
        """Where a truncated answer is most likely and least visible: a summary
        of the first page reads exactly like a summary of the meeting."""
        entry = {"name": "e", "text": "..."}
        recorder = Recorder(
            {
                "/entries": {
                    "transcriptEntries": [entry] * 3,
                    "nextPageToken": "more",
                }
            }
        )
        found = await client(recorder).list_transcript_entries(
            TRANSCRIPT_NAME, max_results=3
        )

        assert found.complete is False
        assert found.summary() == "first 3 (more available)"

    async def test_a_whole_transcript_pages_until_exhausted(self) -> None:
        entry = {"name": "e", "text": "spoken"}
        recorder = Recorder(
            {
                "/entries": [
                    {"transcriptEntries": [entry] * 2, "nextPageToken": "p2"},
                    {"transcriptEntries": [entry]},
                ]
            }
        )
        found = await client(recorder).list_transcript_entries(
            TRANSCRIPT_NAME, max_results=500
        )

        assert len(found) == 3
        assert found.complete is True


# ---------------------------------------------------------------------------
# Through a real Runtime
# ---------------------------------------------------------------------------


class TestThroughAWorkflow:
    async def test_a_transcript_read_journals_and_replays(self) -> None:
        from loom.toolsets.google.meet import client as meet_client
        from loom.toolsets.google.meet.tools import meet_list_transcript_entries

        recorder = Recorder(
            {
                "/entries": {
                    "transcriptEntries": [
                        {"name": "e1", "text": "one"},
                        {"name": "e2", "text": "two"},
                    ],
                    "nextPageToken": "more",
                }
            }
        )
        meet_client._default_client = MeetClient(
            token_auth(), transport=recorder.transport()
        )

        @workflow(name="meet-transcript")
        async def flow(ctx: Context, _input: Any) -> dict[str, Any]:
            said = await ctx.step(
                meet_list_transcript_entries, TRANSCRIPT_NAME, 2
            )
            return {
                "text": " ".join(entry.text for entry in said),
                "complete": said.complete,
            }

        try:
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.output == {"text": "one two", "complete": False}

            calls = len(recorder.requests)
            replayed = await runtime.replay(result.run_id)
            assert replayed.output == result.output
            assert len(recorder.requests) == calls, "replay re-called the API"
        finally:
            meet_client._default_client = None

    async def test_creating_a_space_is_not_retried(self) -> None:
        """A retry after a post-creation timeout leaves a second room with a
        second link, and nothing ties the two together."""
        from loom.toolsets.google.meet import client as meet_client
        from loom.toolsets.google.meet.tools import meet_create_space

        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503, json={"error": {"message": "unavailable"}})

        meet_client._default_client = MeetClient(
            token_auth(), transport=httpx.MockTransport(handler)
        )

        @workflow(name="meet-create")
        async def flow(ctx: Context, _input: Any) -> str:
            space = await ctx.step(meet_create_space)
            return space.meeting_uri

        try:
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.status.value == "failed"
            # A 503 is retryable in general; this step opts out because the
            # duplicate it would create is not detectable afterwards.
            assert len(attempts) == 1
        finally:
            meet_client._default_client = None


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_it_registers_and_stays_distinguishable(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.google import GOOGLE_MEET_MANIFEST

        registry = ToolsetRegistry()
        registry.register(GOOGLE_MEET_MANIFEST)

        assert GOOGLE_MEET_MANIFEST.qualified_id == "app:loom:google_meet"
        assert "google_meet" in {
            card.toolset_id for card in registry.search("meeting transcript")
        }

    def test_ending_a_live_call_is_marked_destructive(self) -> None:
        from loom.toolsets.google import GOOGLE_MEET_MANIFEST
        from loom.toolsets.manifest import EffectClass

        end = GOOGLE_MEET_MANIFEST.find_operation("spaces.end_active_conference")
        create = GOOGLE_MEET_MANIFEST.find_operation("spaces.create")

        assert end.effect is EffectClass.DESTRUCTIVE
        assert create.effect is EffectClass.WRITE

    def test_every_operation_declares_a_scope_and_a_summary(self) -> None:
        from loom.toolsets.google import GOOGLE_MEET_MANIFEST

        for op in GOOGLE_MEET_MANIFEST.all_operations():
            assert op.summary, f"{op.id} has no summary"
            assert op.scopes, f"{op.id} declares no OAuth scope"

    def test_every_listing_is_declared_paged(self) -> None:
        """All five of them return more than one page in practice."""
        from loom.toolsets.google import GOOGLE_MEET_MANIFEST

        paged = {op.function for op in GOOGLE_MEET_MANIFEST.paginated()}
        assert paged == {
            "meet_list_conference_records",
            "meet_list_participants",
            "meet_list_recordings",
            "meet_list_transcripts",
            "meet_list_transcript_entries",
        }
        assert "meet_get_space" not in paged

    def test_tool_docs_name_every_step(self) -> None:
        from loom.toolsets.google.meet import tools

        steps = [name for name in tools.__all__ if not name.isupper()]
        missing = [name for name in steps if name not in tools.MEET_TOOL_DOCS]
        assert not missing, f"undocumented: {missing}"


class TestSchedulingPointsAtCalendar:
    """The single most likely wrong turn, so it is asserted rather than hoped.

    Asked to "schedule a Meet for Tuesday", the obvious move is
    ``meet_create_space`` — which produces a link with no time, no invitees and
    no calendar entry. That looks like success until nobody joins. Both the
    manifest and the docs have to say where scheduling actually lives.
    """

    def test_the_manifest_description_redirects_to_calendar(self) -> None:
        from loom.toolsets.google import GOOGLE_MEET_MANIFEST

        text = GOOGLE_MEET_MANIFEST.description.lower()
        assert "cannot" in text and "schedule" in text
        assert "add_meet" in text

    def test_the_create_operation_says_what_it_does_not_do(self) -> None:
        from loom.toolsets.google import GOOGLE_MEET_MANIFEST

        create = GOOGLE_MEET_MANIFEST.find_operation("spaces.create")
        assert "calendar" in create.description.lower()

    def test_the_docs_show_the_calendar_call(self) -> None:
        from loom.toolsets.google.meet import tools

        assert "calendar_create_event" in tools.MEET_TOOL_DOCS
        assert "add_meet=True" in tools.MEET_TOOL_DOCS

    def test_calendar_actually_provides_it(self) -> None:
        """The redirect must point at something that exists."""
        import inspect

        from loom.toolsets.google.calendar.tools import calendar_create_event

        signature = inspect.signature(
            getattr(calendar_create_event, "fn", calendar_create_event)
        )
        assert "add_meet" in signature.parameters
