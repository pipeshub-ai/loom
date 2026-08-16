"""The Google Drive toolset.

Everything here runs against an ``httpx.MockTransport``, so the request the test
sees is the one the client would put on the wire — URL, query string, headers,
and body — with no network and no patched internals.

The emphasis is deliberate. Most of Drive's failure modes are **silent**: a
missing ``fields`` mask returns a file with no timestamps and a 200, a missing
shared-drive flag returns an empty list and a 200, a ``maxResults`` where Drive
wanted ``pageSize`` returns the first 100 rows and a 200. None of those raise,
none of them look wrong, and all of them make a workflow confidently report
something untrue. So the tests assert on the *query string*, not just on the
parsed result.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from loom import Context, Runtime, workflow
from loom.stores.memory import MemoryStore
from loom.toolsets.google.auth import GoogleAuth, GoogleCredentials
from loom.toolsets.google.drive.client import (
    FILE_FIELDS,
    DriveClient,
    flatten_file,
)
from loom.toolsets.google.drive.models import FOLDER_MIME, DriveFile
from loom.toolsets.google.errors import GooglePermanentError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def token_auth() -> GoogleAuth:
    """Auth that needs no token endpoint."""
    return GoogleAuth(GoogleCredentials(access_token="test-token"))


class Recorder:
    """A mock transport that records requests and replays canned responses.

    A response may be a list, in which case each matching request takes the
    next one — which is how a paging sequence is expressed.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = {k: list(v) if isinstance(v, list) else v
                           for k, v in (responses or {}).items()}
        self.status = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for fragment, payload in self._responses.items():
            if fragment not in str(request.url):
                continue
            if isinstance(payload, list):
                # The last one repeats, so a test only lists the pages it cares
                # about rather than padding out a sequence.
                chosen = payload.pop(0) if len(payload) > 1 else payload[0]
            else:
                chosen = payload
            if isinstance(chosen, httpx.Response):
                return chosen
            return httpx.Response(200, json=chosen)
        return httpx.Response(self.status, json={})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def query(self, index: int = -1) -> dict[str, str]:
        return dict(httpx.QueryParams(self.requests[index].url.query.decode()))

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)


def client(recorder: Recorder) -> DriveClient:
    return DriveClient(token_auth(), transport=recorder.transport())


FILE = {
    "id": "f1",
    "name": "Q3 report.pdf",
    "mimeType": "application/pdf",
    # Drive sends size as a *string* of int64, not a number.
    "size": "2048",
    "parents": ["folder1"],
    "owners": [{"emailAddress": "ada@example.com"}],
    "webViewLink": "https://drive.google.com/file/d/f1/view",
    "webContentLink": "https://drive.google.com/uc?id=f1",
    "createdTime": "2026-03-01T09:00:00.000Z",
    "modifiedTime": "2026-03-02T09:00:00.000Z",
    "md5Checksum": "abc123",
}

FOLDER = {"id": "folder1", "name": "Reports", "mimeType": FOLDER_MIME}

GOOGLE_DOC = {
    "id": "doc1",
    "name": "Strategy",
    "mimeType": "application/vnd.google-apps.document",
}


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


class TestFlatten:
    def test_size_arrives_as_a_string_and_becomes_an_int(self) -> None:
        """Comparing "2048" > 1000 is False, and no error says so."""
        assert flatten_file(FILE).size == 2048

    def test_a_folder_has_no_size_key_at_all(self) -> None:
        """Not a zero — absent. Indexing it would raise on every folder."""
        assert "size" not in FOLDER
        assert flatten_file(FOLDER).size == 0

    def test_is_folder_beats_comparing_the_mime_string(self) -> None:
        assert flatten_file(FOLDER).is_folder
        assert not flatten_file(FILE).is_folder

    def test_a_google_doc_is_flagged_and_a_folder_is_not(self) -> None:
        """Both share the vnd.google-apps prefix; only one is exportable."""
        assert flatten_file(GOOGLE_DOC).is_google_doc
        assert not flatten_file(FOLDER).is_google_doc
        assert not flatten_file(FILE).is_google_doc

    def test_owners_are_flattened_to_addresses(self) -> None:
        assert flatten_file(FILE).owners == ["ada@example.com"]

    def test_a_shared_drive_file_has_no_owners_and_that_is_not_an_error(self) -> None:
        shared = {"id": "x", "name": "y", "driveId": "d1"}
        flat = flatten_file(shared)
        assert flat.owners == []
        assert flat.drive_id == "d1"


# ---------------------------------------------------------------------------
# Listing: the query string is the contract
# ---------------------------------------------------------------------------


class TestListFiles:
    async def test_page_size_is_drives_spelling_not_gmails(self) -> None:
        """The silent one. Drive reads pageSize and *ignores* maxResults.

        Sending the wrong name is not an error — it is every request asking for
        the server default, so a caller wanting 600 files gets 100 and a 200.
        """
        recorder = Recorder({"/files": {"files": [FILE]}})
        await client(recorder).list_files(max_results=600)

        query = recorder.query()
        assert query["pageSize"] == "600"
        assert "maxResults" not in query

    async def test_a_page_larger_than_drives_ceiling_is_capped(self) -> None:
        """Drive answers 400 above 1000 rather than clamping, so we clamp."""
        recorder = Recorder({"/files": {"files": [FILE]}})
        await client(recorder).list_files(max_results=5000)

        assert int(recorder.query()["pageSize"]) == 1000

    async def test_the_fields_mask_is_sent(self) -> None:
        """Without it Drive returns id, name, mimeType and nothing else — so a
        filter on modified_time matches nothing, with no error."""
        recorder = Recorder({"/files": {"files": [FILE]}})
        await client(recorder).list_files()

        fields = recorder.query()["fields"]
        assert fields.startswith("nextPageToken,files(")
        assert "modifiedTime" in fields
        assert "webViewLink" in fields

    async def test_the_mask_covers_every_field_the_model_reads(self) -> None:
        """The mask and the flattener must not drift: a field added to the
        model and not to the mask is silently always empty."""
        asked = {
            part.split("(")[0]
            for part in FILE_FIELDS.replace("(emailAddress)", "").split(",")
        }
        # Every model field maps to a camelCase key in the mask, bar the two
        # derived properties and the flattener's own renames.
        renamed = {"mime_type": "mimeType", "md5_checksum": "md5Checksum"}
        for name in DriveFile.model_fields:
            key = renamed.get(name) or "".join(
                word if i == 0 else word.capitalize()
                for i, word in enumerate(name.split("_"))
            )
            assert key in asked, f"{name} is read but never requested"

    async def test_shared_drives_are_included(self) -> None:
        """Off by default, Drive searches My Drive only — so a team whose files
        all live on a shared drive gets an empty result and a 200."""
        recorder = Recorder({"/files": {"files": [FILE]}})
        await client(recorder).list_files()

        query = recorder.query()
        assert query["includeItemsFromAllDrives"] == "true"
        assert query["supportsAllDrives"] == "true"
        assert query["corpora"] == "allDrives"

    async def test_naming_a_drive_switches_the_corpora(self) -> None:
        """A driveId with corpora=allDrives is ignored, not honoured."""
        recorder = Recorder({"/files": {"files": [FILE]}})
        await client(recorder).list_files(drive_id="d1")

        query = recorder.query()
        assert query["driveId"] == "d1"
        assert query["corpora"] == "drive"

    async def test_trashed_files_are_excluded_by_default(self) -> None:
        """Drive includes them; a workflow that processes a folder would
        otherwise re-process the bin."""
        recorder = Recorder({"/files": {"files": [FILE]}})
        await client(recorder).list_files()

        assert "trashed = false" in recorder.query()["q"]

    async def test_include_trashed_drops_that_clause(self) -> None:
        recorder = Recorder({"/files": {"files": [FILE]}})
        await client(recorder).list_files(include_trashed=True)

        assert "trashed" not in recorder.query().get("q", "")

    async def test_a_folder_id_becomes_a_parents_clause(self) -> None:
        recorder = Recorder({"/files": {"files": [FILE]}})
        await client(recorder).list_files(folder_id="folder1")

        assert "'folder1' in parents" in recorder.query()["q"]

    async def test_clauses_are_combined_without_precedence_surprises(self) -> None:
        """``a or b and trashed = false`` binds the wrong way; parens fix it."""
        recorder = Recorder({"/files": {"files": [FILE]}})
        await client(recorder).list_files("name = 'x' or name = 'y'", folder_id="f")

        query = recorder.query()["q"]
        assert "(name = 'x' or name = 'y')" in query
        assert query.count(" and ") == 2

    async def test_no_query_at_all_still_excludes_the_bin(self) -> None:
        recorder = Recorder({"/files": {"files": []}})
        await client(recorder).list_files()

        assert recorder.query()["q"] == "(trashed = false)"


class TestPagination:
    async def test_next_page_token_is_followed(self) -> None:
        recorder = Recorder(
            {
                "/files": [
                    {"files": [FILE, FILE], "nextPageToken": "p2"},
                    {"files": [FILE]},
                ]
            }
        )
        found = await client(recorder).list_files(max_results=10)

        assert len(found) == 3
        assert found.complete is True
        assert len(recorder.requests) == 2
        assert recorder.query(1)["pageToken"] == "p2"

    async def test_a_truncated_answer_says_so(self) -> None:
        """The whole point of Results: 2 of many must not read as 2."""
        recorder = Recorder(
            {"/files": {"files": [FILE, FILE], "nextPageToken": "p2"}}
        )
        found = await client(recorder).list_files(max_results=2)

        assert len(found) == 2
        assert found.complete is False
        assert found.summary() == "first 2 (more available)"

    async def test_an_exhausted_source_is_complete(self) -> None:
        recorder = Recorder({"/files": {"files": [FILE]}})
        found = await client(recorder).list_files(max_results=50)

        assert found.complete is True
        assert found.summary() == "1 result"

    async def test_the_last_page_asks_only_for_what_is_left(self) -> None:
        """Otherwise the final request over-fetches by most of a page."""
        recorder = Recorder(
            {
                "/files": [
                    {"files": [FILE] * 4, "nextPageToken": "p2"},
                    {"files": [FILE]},
                ]
            }
        )
        await client(recorder).list_files(max_results=5)

        assert recorder.query(1)["pageSize"] == "1"

    async def test_rows_come_back_as_models_with_coverage_intact(self) -> None:
        """``.mapped`` rather than a comprehension — a comprehension yields a
        plain list and throws the coverage away one line after computing it."""
        recorder = Recorder(
            {"/files": {"files": [FILE], "nextPageToken": "more"}}
        )
        found = await client(recorder).list_files(max_results=1)

        assert isinstance(found[0], DriveFile)
        assert found.complete is False

    async def test_permissions_page_at_their_own_lower_ceiling(self) -> None:
        """files.list allows 1000; permissions.list 400s above 100."""
        recorder = Recorder({"/permissions": {"permissions": []}})
        await client(recorder).list_permissions("f1", max_results=500)

        assert int(recorder.query()["pageSize"]) == 100

    async def test_shared_drives_page_too(self) -> None:
        recorder = Recorder(
            {
                "/drives": [
                    {"drives": [{"id": "d1", "name": "Team"}], "nextPageToken": "p"},
                    {"drives": [{"id": "d2", "name": "Ops"}]},
                ]
            }
        )
        found = await client(recorder).list_shared_drives(max_results=10)

        assert [d.id for d in found] == ["d1", "d2"]
        assert found.complete is True

    async def test_a_cursor_with_no_rows_behind_it_terminates(self) -> None:
        """A server that keeps saying "more" and sending nothing would
        otherwise loop to the page ceiling."""
        recorder = Recorder({"/files": {"files": [], "nextPageToken": "forever"}})
        found = await client(recorder).list_files(max_results=100)

        assert len(found) == 0
        assert len(recorder.requests) == 1


class TestQueryEscaping:
    async def test_an_apostrophe_in_a_name_is_escaped(self) -> None:
        """"Ada's reports" otherwise closes the quote and Drive answers 400
        naming a character position in a string the caller never wrote."""
        recorder = Recorder({"/files": {"files": []}})
        await client(recorder).find_folder("Ada's reports")

        assert "\\'" in recorder.query()["q"]

    async def test_find_folder_matches_exactly_not_loosely(self) -> None:
        """``contains`` would return "Reports Archive" for "Reports", and
        writing to the wrong folder is worse than finding nothing."""
        recorder = Recorder({"/files": {"files": [FOLDER]}})
        await client(recorder).find_folder("Reports")

        query = recorder.query()["q"]
        assert "name = 'Reports'" in query
        assert "contains" not in query
        assert FOLDER_MIME in query

    async def test_find_folder_returns_none_when_nothing_matches(self) -> None:
        """None, not a raise — the caller may legitimately create it."""
        recorder = Recorder({"/files": {"files": []}})
        assert await client(recorder).find_folder("Nope") is None

    async def test_find_folder_can_be_scoped_to_a_parent(self) -> None:
        recorder = Recorder({"/files": {"files": [FOLDER]}})
        await client(recorder).find_folder("Reports", parent_id="root1")

        assert "'root1' in parents" in recorder.query()["q"]


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


class TestDownloadAndExport:
    async def test_download_asks_for_media_and_returns_the_bytes(self) -> None:
        recorder = Recorder(
            {
                "alt=media": httpx.Response(200, content=b"%PDF-1.4 binary"),
                "/files/f1": FILE,
            }
        )
        attachment = await client(recorder).download_file("f1")

        assert attachment.data == b"%PDF-1.4 binary"
        assert attachment.filename == "Q3 report.pdf"
        assert attachment.mime == "application/pdf"
        assert attachment.size == len(b"%PDF-1.4 binary")

    async def test_binary_content_is_not_decoded_as_json(self) -> None:
        """A PDF through the JSON path raises ValueError several frames from
        the cause."""
        raw = bytes(range(256))
        recorder = Recorder(
            {"alt=media": httpx.Response(200, content=raw), "/files/f1": FILE}
        )
        attachment = await client(recorder).download_file("f1")

        assert attachment.data == raw

    async def test_downloading_a_google_doc_names_the_export_call(self) -> None:
        """Drive's own answer is a 403 'fileNotDownloadable', which reads like
        a permissions problem and sends the reader somewhere useless."""
        recorder = Recorder({"/files/doc1": GOOGLE_DOC})

        with pytest.raises(GooglePermanentError) as exc:
            await client(recorder).download_file("doc1")

        assert "drive_export_file" in str(exc.value)
        # It refused before spending a request on a download that cannot work.
        assert not any("alt=media" in str(r.url) for r in recorder.requests)

    async def test_downloading_a_folder_says_it_is_a_folder(self) -> None:
        recorder = Recorder({"/files/folder1": FOLDER})

        with pytest.raises(GooglePermanentError) as exc:
            await client(recorder).download_file("folder1")

        assert "folder" in str(exc.value)
        assert "drive_list_files" in str(exc.value)

    async def test_export_defaults_by_source_type(self) -> None:
        """A Doc wants PDF and a Sheet wants XLSX; making the caller supply one
        turns every export into a lookup."""
        recorder = Recorder(
            {"/export": httpx.Response(200, content=b"pdf"), "/files/doc1": GOOGLE_DOC}
        )
        attachment = await client(recorder).export_file("doc1")

        export_at = next(
            i for i, r in enumerate(recorder.requests) if "/export" in str(r.url)
        )
        assert recorder.query(export_at)["mimeType"] == "application/pdf"
        assert attachment.filename == "Strategy.pdf"

    async def test_a_sheet_exports_as_xlsx(self) -> None:
        sheet = {
            "id": "s1",
            "name": "Numbers",
            "mimeType": "application/vnd.google-apps.spreadsheet",
        }
        recorder = Recorder(
            {"/export": httpx.Response(200, content=b"xl"), "/files/s1": sheet}
        )
        attachment = await client(recorder).export_file("s1")

        assert attachment.filename == "Numbers.xlsx"

    async def test_an_explicit_export_format_wins(self) -> None:
        recorder = Recorder(
            {"/export": httpx.Response(200, content=b"c,s,v"), "/files/doc1": GOOGLE_DOC}
        )
        attachment = await client(recorder).export_file("doc1", "text/csv")

        assert attachment.filename == "Strategy.csv"
        assert attachment.mime == "text/csv"

    async def test_exporting_an_ordinary_file_says_to_download_it(self) -> None:
        recorder = Recorder({"/files/f1": FILE})

        with pytest.raises(GooglePermanentError) as exc:
            await client(recorder).export_file("f1")

        assert "drive_download_file" in str(exc.value)


class TestUpload:
    async def test_upload_goes_to_the_upload_host(self) -> None:
        """Posting content to the ordinary endpoint creates a metadata-only
        file — an empty document with the right name."""
        recorder = Recorder({"/upload/": FILE})
        await client(recorder).upload_file("notes.txt", b"hello")

        assert "/upload/drive/v3/files" in str(recorder.last.url)
        assert recorder.query()["uploadType"] == "multipart"

    async def test_the_body_is_metadata_then_bytes(self) -> None:
        recorder = Recorder({"/upload/": FILE})
        await client(recorder).upload_file(
            "notes.txt", b"hello", folder_id="folder1", description="d"
        )

        body = recorder.last.content
        assert b'"name": "notes.txt"' in body
        assert b'"parents": ["folder1"]' in body
        assert b'"description": "d"' in body
        assert body.rstrip().endswith(b"--")
        # The content is verbatim, not re-encoded — base64 or rewritten line
        # endings would corrupt every binary file.
        assert b"\r\n\r\nhello\r\n--" in body

    async def test_the_content_type_declares_the_boundary(self) -> None:
        recorder = Recorder({"/upload/": FILE})
        await client(recorder).upload_file("notes.txt", b"hello")

        header = recorder.last.headers["content-type"]
        assert header.startswith("multipart/related; boundary=")
        boundary = header.split("boundary=")[1]
        assert f"--{boundary}".encode() in recorder.last.content

    async def test_content_containing_the_boundary_does_not_truncate(self) -> None:
        """Vanishingly rare, catastrophic, and cheap to rule out: the file
        would end its own part early and be silently cut in half."""
        recorder = Recorder({"/upload/": FILE})
        payload = b"before--loom-drive-boundary--after"
        await client(recorder).upload_file("x.bin", payload)

        boundary = recorder.last.headers["content-type"].split("boundary=")[1]
        assert boundary != "loom-drive-boundary"
        assert payload in recorder.last.content

    async def test_the_boundary_is_deterministic(self) -> None:
        """A workflow step must produce the same request when re-driven."""
        first, second = Recorder({"/upload/": FILE}), Recorder({"/upload/": FILE})
        await client(first).upload_file("a.txt", b"same")
        await client(second).upload_file("a.txt", b"same")

        assert first.last.content == second.last.content

    async def test_text_is_encoded_as_utf8(self) -> None:
        recorder = Recorder({"/upload/": FILE})
        await client(recorder).upload_file("a.txt", "héllo")

        assert "héllo".encode() in recorder.last.content

    async def test_the_mime_type_is_guessed_from_the_filename(self) -> None:
        recorder = Recorder({"/upload/": FILE})
        await client(recorder).upload_file("report.pdf", b"x")

        assert b"Content-Type: application/pdf" in recorder.last.content

    async def test_updating_content_patches_and_keeps_the_id(self) -> None:
        """A new revision of the same file — re-uploading instead would break
        every existing link and drop every permission."""
        recorder = Recorder({"/upload/": FILE})
        await client(recorder).update_file_content("f1", b"v2", mime_type="text/plain")

        assert recorder.last.method == "PATCH"
        assert "/upload/drive/v3/files/f1" in str(recorder.last.url)
        assert recorder.query()["uploadType"] == "media"
        assert recorder.last.content == b"v2"


# ---------------------------------------------------------------------------
# Organising
# ---------------------------------------------------------------------------


class TestOrganising:
    async def test_create_folder_sends_the_folder_mime(self) -> None:
        recorder = Recorder({"/files": FOLDER})
        result = await client(recorder).create_folder("Reports", "root1")

        assert recorder.body()["mimeType"] == FOLDER_MIME
        assert recorder.body()["parents"] == ["root1"]
        assert result.is_folder

    async def test_move_removes_the_old_parent(self) -> None:
        """Drive has no move. Adding a parent without removing the old one
        leaves the file in both places, which reads as a failed move."""
        recorder = Recorder({"/files/f1": FILE})
        await client(recorder).move_file("f1", "folder2")

        query = recorder.query()
        assert query["addParents"] == "folder2"
        assert query["removeParents"] == "folder1"

    async def test_move_reads_the_current_parents_first(self) -> None:
        recorder = Recorder({"/files/f1": FILE})
        await client(recorder).move_file("f1", "folder2")

        assert recorder.requests[0].method == "GET"
        assert recorder.requests[1].method == "PATCH"

    async def test_an_explicit_parent_skips_the_read(self) -> None:
        recorder = Recorder({"/files/f1": FILE})
        await client(recorder).move_file("f1", "folder2", remove_from="folder1")

        assert len(recorder.requests) == 1
        assert recorder.query()["removeParents"] == "folder1"

    async def test_moving_into_the_current_parent_removes_nothing(self) -> None:
        """Otherwise the destination is added and immediately removed."""
        recorder = Recorder({"/files/f1": FILE})
        await client(recorder).move_file("f1", "folder1")

        assert "removeParents" not in recorder.query()

    async def test_rename_patches_only_the_name(self) -> None:
        recorder = Recorder({"/files/f1": FILE})
        await client(recorder).update_file("f1", {"name": "new.pdf"})

        assert recorder.body() == {"name": "new.pdf"}

    async def test_trash_is_a_flag_not_a_delete(self) -> None:
        recorder = Recorder({"/files/f1": {**FILE, "trashed": True}})
        result = await client(recorder).trash_file("f1")

        assert recorder.last.method == "PATCH"
        assert recorder.body() == {"trashed": True}
        assert result.trashed

    async def test_restore_clears_it(self) -> None:
        recorder = Recorder({"/files/f1": FILE})
        await client(recorder).restore_file("f1")

        assert recorder.body() == {"trashed": False}

    async def test_delete_is_a_real_delete(self) -> None:
        recorder = Recorder()
        await client(recorder).delete_file("f1")

        assert recorder.last.method == "DELETE"

    async def test_copy_can_rename_and_reparent(self) -> None:
        recorder = Recorder({"/copy": FILE})
        await client(recorder).copy_file("f1", "Copy for legal", "folder2")

        assert recorder.body() == {
            "name": "Copy for legal",
            "parents": ["folder2"],
        }

    async def test_every_mutation_supports_shared_drives(self) -> None:
        """Without the flag, a write to a file on a shared drive fails with an
        error naming nothing the caller can act on."""
        recorder = Recorder({"/files": FILE, "/copy": FILE})
        drive = client(recorder)

        await drive.create_folder("x")
        await drive.trash_file("f1")
        await drive.copy_file("f1")
        await drive.delete_file("f1")

        for index in range(len(recorder.requests)):
            assert recorder.query(index)["supportsAllDrives"] == "true"


class TestSharing:
    async def test_nobody_is_emailed_by_default(self) -> None:
        """A workflow sharing two hundred files should not send two hundred
        emails as a side effect of a default."""
        recorder = Recorder({"/permissions": {"id": "p1", "role": "reader"}})
        await client(recorder).share_file("f1", email="ada@example.com")

        assert recorder.query()["sendNotificationEmail"] == "false"

    async def test_notify_is_passed_through_when_asked_for(self) -> None:
        recorder = Recorder({"/permissions": {"id": "p1"}})
        await client(recorder).share_file(
            "f1", email="ada@example.com", notify=True, message="Here you go"
        )

        query = recorder.query()
        assert query["sendNotificationEmail"] == "true"
        assert query["emailMessage"] == "Here you go"

    async def test_a_user_grant_carries_the_address(self) -> None:
        recorder = Recorder({"/permissions": {"id": "p1"}})
        await client(recorder).share_file(
            "f1", email="ada@example.com", role="writer"
        )

        assert recorder.body() == {
            "role": "writer",
            "type": "user",
            "emailAddress": "ada@example.com",
        }

    async def test_public_sharing_needs_no_address(self) -> None:
        recorder = Recorder({"/permissions": {"id": "p1", "type": "anyone"}})
        await client(recorder).share_file("f1", type="anyone")

        assert recorder.body() == {"role": "reader", "type": "anyone"}

    async def test_permissions_flatten_to_models(self) -> None:
        recorder = Recorder(
            {
                "/permissions": {
                    "permissions": [
                        {
                            "id": "p1",
                            "type": "user",
                            "role": "writer",
                            "emailAddress": "ada@example.com",
                            "displayName": "Ada",
                        }
                    ]
                }
            }
        )
        found = await client(recorder).list_permissions("f1")

        assert found[0].email_address == "ada@example.com"
        assert found[0].role == "writer"

    async def test_revoking_deletes_the_rule(self) -> None:
        recorder = Recorder()
        await client(recorder).remove_permission("f1", "p1")

        assert recorder.last.method == "DELETE"
        assert "/files/f1/permissions/p1" in str(recorder.last.url)


class TestQuota:
    async def test_an_unlimited_account_reports_zero_rather_than_raising(self) -> None:
        """``limit`` is absent, not large, on an unlimited account — indexing
        it would raise on exactly the accounts with the most headroom."""
        recorder = Recorder({"/about": {"storageQuota": {"usage": "500"}}})
        quota = await client(recorder).get_storage_quota()

        assert quota == {"limit": 0, "usage": 500, "usage_in_drive": 0}


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    async def test_a_missing_file_id_names_the_argument(self) -> None:
        """``quote(None)`` raises "quote_from_bytes() expected bytes", which
        names neither the argument nor the mistake."""
        recorder = Recorder()
        with pytest.raises(ValueError, match="file_id"):
            await client(recorder).get_file(None)  # type: ignore[arg-type]

    async def test_an_empty_file_id_is_refused(self) -> None:
        recorder = Recorder()
        with pytest.raises(ValueError, match="file_id"):
            await client(recorder).get_file("")

    async def test_a_file_id_is_percent_encoded(self) -> None:
        recorder = Recorder({"/files": FILE})
        await client(recorder).get_file("a/b")

        assert "files/a%2Fb" in str(recorder.last.url)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    async def test_a_401_reauthenticates_once_then_succeeds(self) -> None:
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(1)
            if len(seen) == 1:
                return httpx.Response(401, json={"error": {"message": "expired"}})
            return httpx.Response(200, json=FILE)

        drive = DriveClient(token_auth(), transport=httpx.MockTransport(handler))
        assert (await drive.get_file("f1")).id == "f1"
        assert len(seen) == 2

    async def test_a_download_classifies_its_errors_too(self) -> None:
        """The raw path must not swallow a JSON error body."""
        recorder = Recorder(
            {
                "alt=media": httpx.Response(
                    404, json={"error": {"message": "File not found"}}
                ),
                "/files/f1": FILE,
            }
        )
        with pytest.raises(GooglePermanentError, match="File not found"):
            await client(recorder).download_file("f1")

    async def test_an_upload_classifies_its_errors_too(self) -> None:
        recorder = Recorder(
            {
                "/upload/": httpx.Response(
                    403, json={"error": {"errors": [{"reason": "storageQuotaExceeded"}],
                                          "message": "out of space"}}
                )
            }
        )
        with pytest.raises(GooglePermanentError, match="out of space"):
            await client(recorder).upload_file("x.txt", b"x")


# ---------------------------------------------------------------------------
# Through a real Runtime
# ---------------------------------------------------------------------------


class TestThroughAWorkflow:
    async def test_a_paged_read_keeps_its_coverage_across_the_journal(self) -> None:
        """The sharp edge Results exists for. ``complete`` is computed at the
        call site, and a replay must not turn "first 2 of many" into "2"."""
        from loom.toolsets.google.drive import client as drive_client
        from loom.toolsets.google.drive.tools import drive_list_files

        recorder = Recorder(
            {"/files": {"files": [FILE, FILE], "nextPageToken": "more"}}
        )
        drive_client._default_client = DriveClient(
            token_auth(), transport=recorder.transport()
        )

        @workflow(name="drive-coverage")
        async def flow(ctx: Context, _input: Any) -> dict[str, Any]:
            found = await ctx.step(drive_list_files, "name contains 'x'", 2)
            return {"count": len(found), "complete": found.complete,
                    "summary": found.summary(), "first": found[0].name}

        try:
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.output == {
                "count": 2,
                "complete": False,
                "summary": "first 2 (more available)",
                "first": "Q3 report.pdf",
            }

            calls = len(recorder.requests)
            replayed = await runtime.replay(result.run_id)
            assert replayed.output == result.output
            assert len(recorder.requests) == calls, "replay re-called the API"
        finally:
            drive_client._default_client = None

    async def test_typed_models_survive_the_journal(self) -> None:
        from loom.toolsets.google.drive import client as drive_client
        from loom.toolsets.google.drive.tools import drive_get_file

        recorder = Recorder({"/files/f1": FILE})
        drive_client._default_client = DriveClient(
            token_auth(), transport=recorder.transport()
        )

        @workflow(name="drive-models")
        async def flow(ctx: Context, _input: Any) -> dict[str, Any]:
            first = await ctx.step(drive_get_file, "f1")
            again = await ctx.step(drive_get_file, "f1")
            # The second is served from the journal, so this asserts a decoded
            # entry is still a model and not a dict.
            return {"size": again.size, "folder": again.is_folder,
                    "same": first.id == again.id}

        try:
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.output == {"size": 2048, "folder": False, "same": True}
        finally:
            drive_client._default_client = None

    async def test_a_permanent_error_fails_fast_instead_of_retrying(self) -> None:
        """A 404 will fail identically every time; three attempts with backoff
        just makes the workflow slower to say so."""
        from loom.toolsets.google.drive import client as drive_client
        from loom.toolsets.google.drive.tools import drive_get_file

        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(404, json={"error": {"message": "not found"}})

        drive_client._default_client = DriveClient(
            token_auth(), transport=httpx.MockTransport(handler)
        )

        @workflow(name="drive-permanent")
        async def flow(ctx: Context, _input: Any) -> str:
            found = await ctx.step(drive_get_file, "missing")
            return found.id

        try:
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.status.value == "failed"
            assert len(attempts) == 1
        finally:
            drive_client._default_client = None


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_it_registers_and_is_findable_by_what_an_agent_would_ask(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.google import GOOGLE_DRIVE_MANIFEST

        registry = ToolsetRegistry()
        registry.register(GOOGLE_DRIVE_MANIFEST)

        assert GOOGLE_DRIVE_MANIFEST.qualified_id == "app:loom:google_drive"
        assert "google_drive" in {
            card.toolset_id for card in registry.search("upload file to drive")
        }

    def test_permanent_delete_is_destructive_and_trash_is_not_delete(self) -> None:
        from loom.toolsets.google import GOOGLE_DRIVE_MANIFEST
        from loom.toolsets.manifest import EffectClass

        delete = GOOGLE_DRIVE_MANIFEST.find_operation("files.delete")
        trash = GOOGLE_DRIVE_MANIFEST.find_operation("files.trash")
        upload = GOOGLE_DRIVE_MANIFEST.find_operation("files.upload")

        assert delete.effect is EffectClass.DESTRUCTIVE
        assert trash.effect is EffectClass.DESTRUCTIVE
        assert upload.effect is EffectClass.WRITE
        # Trash is recoverable and so is safe to repeat; a permanent delete
        # is not marked idempotent because the second call 404s.
        assert trash.idempotent and not delete.idempotent

    def test_the_folder_lookup_is_declared_as_a_resolver(self) -> None:
        """Filtering by a name a human typed is how a query returns zero rows
        and no error; this is what tells the agent to resolve first."""
        from loom.toolsets.google import GOOGLE_DRIVE_MANIFEST

        assert GOOGLE_DRIVE_MANIFEST.resolvers()["folder"].function == (
            "drive_find_folder"
        )

    def test_every_operation_declares_a_scope_and_a_summary(self) -> None:
        from loom.toolsets.google import GOOGLE_DRIVE_MANIFEST

        for op in GOOGLE_DRIVE_MANIFEST.all_operations():
            assert op.summary, f"{op.id} has no summary"
            assert op.scopes, f"{op.id} declares no OAuth scope"

    def test_the_paged_reads_are_exactly_the_ones_that_page(self) -> None:
        from loom.toolsets.google import GOOGLE_DRIVE_MANIFEST

        paged = {op.function for op in GOOGLE_DRIVE_MANIFEST.paginated()}
        assert paged == {
            "drive_list_files",
            "drive_list_permissions",
            "drive_list_shared_drives",
        }
        assert "drive_get_file" not in paged

    def test_tool_docs_name_every_step(self) -> None:
        """The docs are what the coding agent reads; a missing tool is
        invisible to it."""
        from loom.toolsets.google.drive import tools

        steps = [name for name in tools.__all__ if not name.isupper()]
        missing = [name for name in steps if name not in tools.DRIVE_TOOL_DOCS]
        assert not missing, f"undocumented: {missing}"

    def test_the_docs_warn_about_the_two_silent_traps(self) -> None:
        from loom.toolsets.google.drive import tools

        docs = tools.DRIVE_TOOL_DOCS
        assert "no bytes" in docs, "nothing tells the agent a Doc cannot download"
        assert ".complete" in docs, "nothing tells the agent a read may be capped"


class TestRenaming:
    async def test_renaming_patches_only_the_name(self) -> None:
        recorder = Recorder({"/files/f1": FILE})
        await client(recorder).update_file("f1", {"name": "renamed.pdf"})

        assert recorder.last.method == "PATCH"
        assert recorder.body() == {"name": "renamed.pdf"}

    async def test_renaming_does_not_touch_parents(self) -> None:
        """A rename that re-parented would move the file as a side effect."""
        recorder = Recorder({"/files/f1": FILE})
        await client(recorder).update_file("f1", {"name": "renamed.pdf"})

        query = recorder.query()
        assert "addParents" not in query
        assert "removeParents" not in query


class TestConfigurableTimeouts:
    """A large Drive export legitimately takes minutes; a 30-second API budget
    would fail it and look like a Drive problem rather than a client setting."""

    def test_the_api_timeout_reaches_the_session(self) -> None:
        drive = DriveClient(token_auth(), timeout=5.0)
        assert drive._session._timeout == 5.0

    def test_transfers_and_uploads_get_the_longer_budget(self) -> None:
        from loom.toolsets.google.http import DEFAULT_TIMEOUT, DEFAULT_TRANSFER_TIMEOUT

        assert DEFAULT_TRANSFER_TIMEOUT > DEFAULT_TIMEOUT
        drive = DriveClient(token_auth())
        assert drive._transfers._timeout == DEFAULT_TRANSFER_TIMEOUT
        assert drive._uploads._timeout == DEFAULT_TRANSFER_TIMEOUT

    def test_both_are_settable_independently(self) -> None:
        drive = DriveClient(token_auth(), timeout=5.0, transfer_timeout=900.0)
        assert drive._session._timeout == 5.0
        assert drive._transfers._timeout == 900.0
