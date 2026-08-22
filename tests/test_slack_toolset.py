"""The Slack toolset.

Everything runs against an ``httpx.MockTransport``, so the request the test sees
is the one the client would put on the wire — URL, query string, headers, body —
with no network and no patched internals.

The weight is on one fact: **a Slack failure is an HTTP 200.** A client written
to the shape every other toolset here uses would treat every failure as an empty
success, so the tests assert that failures *raise*, and raise the right type.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from loom import Context, Runtime, workflow
from loom.core.retry import PERMANENT_ERRORS
from loom.stores.memory import MemoryStore
from loom.toolsets.factory import use_clients
from loom.toolsets.slack.client import (
    SlackClient,
    flatten_channel,
    flatten_message,
    flatten_user,
)
from loom.toolsets.slack.errors import (
    SlackAPIError,
    SlackAuthError,
    SlackMissingScope,
    SlackPermanentError,
    SlackRateLimited,
    classify,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class Recorder:
    """Records requests and replays canned bodies; a list is a page run."""

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
        return httpx.Response(200, json={"ok": True})

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def query(self, index: int = -1) -> dict[str, str]:
        return dict(httpx.QueryParams(self.requests[index].url.query.decode()))

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)


def client(recorder: Recorder) -> SlackClient:
    return SlackClient("xoxb-test", transport=recorder.transport())


CHANNEL = {
    "id": "C024BE91L",
    "name": "incidents",
    "is_private": False,
    "is_member": True,
    "num_members": 12,
    "topic": {"value": "Production incidents", "creator": "U1", "last_set": 0},
    "purpose": {"value": "Where incidents go", "creator": "U1", "last_set": 0},
}

MESSAGE = {
    "ts": "1718280000.123456",
    "user": "U023BECGF",
    "text": "Deploy finished.",
    "reply_count": 2,
}

USER = {
    "id": "U023BECGF",
    "name": "ada",
    "real_name": "Ada Lovelace",
    "is_bot": False,
    "tz": "Europe/London",
    "profile": {
        "display_name": "ada",
        "email": "ada@example.com",
        "real_name": "Ada Lovelace",
        "title": "Engineer",
    },
}


# ---------------------------------------------------------------------------
# The ok:false envelope
# ---------------------------------------------------------------------------


class TestFailuresAreNotStatusCodes:
    """The whole reason this toolset has its own errors module."""

    async def test_ok_false_on_an_http_200_raises(self) -> None:
        """Without this, every failure is an empty success."""
        recorder = Recorder(
            {"conversations.list": {"ok": False, "error": "invalid_auth"}}
        )
        with pytest.raises(SlackAPIError):
            await client(recorder).list_channels()

    async def test_a_bad_channel_raises_rather_than_returning_nothing(self) -> None:
        recorder = Recorder(
            {"conversations.history": {"ok": False, "error": "channel_not_found"}}
        )
        with pytest.raises(SlackPermanentError, match="channel_not_found"):
            await client(recorder).history("C000")

    async def test_a_post_to_a_channel_we_are_not_in_raises(self) -> None:
        """The most common Slack integration failure, and the one that would
        otherwise report a message as delivered when nothing was sent."""
        recorder = Recorder(
            {"chat.postMessage": {"ok": False, "error": "not_in_channel"}}
        )
        with pytest.raises(SlackPermanentError, match="not_in_channel"):
            await client(recorder).post_message("C024BE91L", "hello")

    def test_an_argument_error_is_permanent(self) -> None:
        """Retrying it three times arrives at the same answer more slowly."""
        error = classify({"ok": False, "error": "channel_not_found"})
        assert isinstance(error, PERMANENT_ERRORS)

    def test_a_slack_side_error_is_retryable(self) -> None:
        error = classify({"ok": False, "error": "service_unavailable"})
        assert not isinstance(error, PERMANENT_ERRORS)

    def test_auth_errors_name_the_fix(self) -> None:
        error = classify({"ok": False, "error": "invalid_auth"})
        assert isinstance(error, SlackAuthError)
        assert "loom connect slack" in str(error)

    def test_a_missing_scope_says_which_scope(self) -> None:
        """A different fix in kind — a reinstall, by a person, once — so it
        gets its own type and names the scope Slack asked for."""
        error = classify(
            {"ok": False, "error": "missing_scope", "needed": "channels:history"}
        )
        assert isinstance(error, SlackMissingScope)
        assert error.needed == "channels:history"
        assert "channels:history" in str(error)

    def test_rate_limiting_arrives_both_ways(self) -> None:
        """As an ok:false body, and as a real HTTP 429."""
        from_body = classify({"ok": False, "error": "ratelimited"})
        from_status = classify({}, status=429, retry_after=30.0)

        assert isinstance(from_body, SlackRateLimited)
        assert isinstance(from_status, SlackRateLimited)
        assert from_status.retry_after == 30.0
        assert not isinstance(from_body, PERMANENT_ERRORS)

    async def test_a_429_carries_retry_after_from_the_header(self) -> None:
        recorder = Recorder(
            {
                "conversations.list": httpx.Response(
                    429, json={"ok": False, "error": "ratelimited"},
                    headers={"Retry-After": "12"},
                )
            }
        )
        with pytest.raises(SlackRateLimited) as exc:
            await client(recorder).list_channels()
        assert exc.value.retry_after == 12.0

    async def test_a_non_json_body_still_classifies(self) -> None:
        """An HTML error page from a proxy must not surface as a JSON crash."""
        recorder = Recorder(
            {"conversations.list": httpx.Response(502, text="<html>bad gateway")}
        )
        with pytest.raises(SlackAPIError):
            await client(recorder).list_channels()

    def test_the_error_code_is_kept_for_branching(self) -> None:
        error = classify({"ok": False, "error": "users_not_found"}, method="users.info")
        assert error.error == "users_not_found"
        assert error.method == "users.info"

    async def test_a_successful_call_is_not_disturbed(self) -> None:
        recorder = Recorder({"conversations.info": {"ok": True, "channel": CHANNEL}})
        assert (await client(recorder).get_channel("C024BE91L")).name == "incidents"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    async def test_the_cursor_is_read_from_the_nested_envelope(self) -> None:
        """Slack puts it at response_metadata.next_cursor, not at the top."""
        recorder = Recorder(
            {
                "conversations.list": [
                    {
                        "ok": True,
                        "channels": [CHANNEL],
                        "response_metadata": {"next_cursor": "dXNlcjpVMEc5V0ZYTlo="},
                    },
                    {"ok": True, "channels": [{**CHANNEL, "id": "C2"}]},
                ]
            }
        )
        found = await client(recorder).list_channels(max_results=10)

        assert [c.id for c in found] == ["C024BE91L", "C2"]
        assert found.complete is True
        assert recorder.query(1)["cursor"] == "dXNlcjpVMEc5V0ZYTlo="

    async def test_an_empty_cursor_means_exhausted(self) -> None:
        """Slack sends "" as often as it omits the field. Reading only for
        absence would loop to the page ceiling against a real workspace."""
        recorder = Recorder(
            {
                "conversations.list": {
                    "ok": True,
                    "channels": [CHANNEL],
                    "response_metadata": {"next_cursor": ""},
                }
            }
        )
        found = await client(recorder).list_channels(max_results=50)

        assert found.complete is True
        assert len(recorder.requests) == 1

    async def test_an_absent_envelope_means_exhausted(self) -> None:
        recorder = Recorder({"conversations.list": {"ok": True, "channels": [CHANNEL]}})
        found = await client(recorder).list_channels(max_results=50)

        assert found.complete is True
        assert len(recorder.requests) == 1

    async def test_a_truncated_read_says_so(self) -> None:
        recorder = Recorder(
            {
                "conversations.list": {
                    "ok": True,
                    "channels": [CHANNEL, {**CHANNEL, "id": "C2"}],
                    "response_metadata": {"next_cursor": "more"},
                }
            }
        )
        found = await client(recorder).list_channels(max_results=2)

        assert found.complete is False
        assert found.summary() == "first 2 (more available)"

    async def test_the_page_size_is_slacks_recommendation_not_its_ceiling(
        self,
    ) -> None:
        """1000 is allowed and 100-200 is advised: a big page is slower to
        build server-side and likelier to time out."""
        recorder = Recorder({"conversations.list": {"ok": True, "channels": []}})
        await client(recorder).list_channels(max_results=1000)

        assert int(recorder.query()["limit"]) == 200

    async def test_history_pages_too(self) -> None:
        recorder = Recorder(
            {
                "conversations.history": [
                    {
                        "ok": True,
                        "messages": [MESSAGE],
                        "response_metadata": {"next_cursor": "p2"},
                    },
                    {"ok": True, "messages": [{**MESSAGE, "ts": "2.0"}]},
                ]
            }
        )
        found = await client(recorder).history("C024BE91L", max_results=10)

        assert [m.ts for m in found] == ["1718280000.123456", "2.0"]


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


class TestShaping:
    def test_ts_stays_a_string(self) -> None:
        """It is an id as well as a timestamp. Parsed to a float it loses the
        microsecond precision and then matches no message at all."""
        message = flatten_message(MESSAGE)

        assert message.ts == "1718280000.123456"
        assert isinstance(message.ts, str)

    def test_topic_and_purpose_are_unwrapped(self) -> None:
        """Both arrive as {"value": ..., "creator": ..., "last_set": ...}."""
        channel = flatten_channel(CHANNEL)

        assert channel.topic == "Production incidents"
        assert channel.purpose == "Where incidents go"

    def test_a_channel_with_no_topic_is_empty_not_a_crash(self) -> None:
        assert flatten_channel({"id": "C1", "name": "x"}).topic == ""

    def test_email_comes_out_of_the_profile(self) -> None:
        assert flatten_user(USER).email == "ada@example.com"

    def test_a_user_without_the_email_scope_is_blank_not_missing(self) -> None:
        """users:read.email is separate from users:read, so this is the common
        case on an otherwise working token."""
        without = {**USER, "profile": {"display_name": "ada"}}
        assert flatten_user(without).email == ""

    def test_an_app_message_is_distinguishable_from_a_person(self) -> None:
        """How a workflow avoids replying to its own messages forever."""
        from_app = flatten_message({"ts": "1.0", "bot_id": "B1", "text": "hi"})
        from_person = flatten_message(MESSAGE)

        assert from_app.from_app and not from_person.from_app

    def test_a_thread_reply_is_distinguishable_from_its_parent(self) -> None:
        """Slack marks a thread root by setting thread_ts equal to ts, so a
        naive `if thread_ts` treats the parent as one of its own replies."""
        parent = flatten_message({"ts": "1.0", "thread_ts": "1.0"})
        reply = flatten_message({"ts": "2.0", "thread_ts": "1.0"})

        assert not parent.is_thread_reply
        assert reply.is_thread_reply

    def test_files_are_flattened_off_the_message(self) -> None:
        with_file = {
            **MESSAGE,
            "files": [
                {"id": "F1", "name": "log.txt", "mimetype": "text/plain",
                 "size": 12, "url_private": "https://files.slack.com/x"}
            ],
        }
        message = flatten_message(with_file)

        assert message.files[0].name == "log.txt"
        assert message.files[0].url_private.endswith("/x")


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class TestRequests:
    async def test_reads_go_as_query_parameters(self) -> None:
        recorder = Recorder({"conversations.history": {"ok": True, "messages": []}})
        await client(recorder).history("C024BE91L", oldest="1.0")

        assert recorder.last.method == "GET"
        assert recorder.query()["channel"] == "C024BE91L"
        assert recorder.query()["oldest"] == "1.0"

    async def test_writes_go_as_a_json_body(self) -> None:
        """Several arguments are structured; form-encoding them would mean
        hand-serialising JSON into a string field."""
        recorder = Recorder({"chat.postMessage": {"ok": True, "ts": "1.0"}})
        await client(recorder).post_message("C1", "hello")

        assert recorder.last.method == "POST"
        assert recorder.last.headers["content-type"].startswith("application/json")
        assert recorder.body()["text"] == "hello"

    async def test_the_bearer_token_is_sent(self) -> None:
        recorder = Recorder({"conversations.list": {"ok": True, "channels": []}})
        await client(recorder).list_channels()

        assert recorder.last.headers["authorization"] == "Bearer xoxb-test"

    async def test_unset_arguments_are_omitted_not_blanked(self) -> None:
        """Slack rejects thread_ts="" where omitting it posts to the channel."""
        recorder = Recorder({"chat.postMessage": {"ok": True, "ts": "1.0"}})
        await client(recorder).post_message("C1", "hello")

        assert "thread_ts" not in recorder.body()

    async def test_a_thread_reply_sends_the_parent(self) -> None:
        recorder = Recorder({"chat.postMessage": {"ok": True, "ts": "2.0"}})
        await client(recorder).post_message("C1", "reply", thread_ts="1.0")

        assert recorder.body()["thread_ts"] == "1.0"

    async def test_join_notices_are_dropped_by_default(self) -> None:
        """They are most of a busy channel's history and none of its
        conversation — an agent asked to summarise otherwise summarises who
        joined."""
        recorder = Recorder(
            {
                "conversations.history": {
                    "ok": True,
                    "messages": [
                        MESSAGE,
                        {"ts": "2.0", "subtype": "channel_join", "text": "x joined"},
                    ],
                }
            }
        )
        found = await client(recorder).history("C1")

        assert [m.ts for m in found] == ["1718280000.123456"]

    async def test_dropping_them_keeps_the_coverage(self) -> None:
        """A comprehension here would compute .complete and throw it away."""
        recorder = Recorder(
            {
                "conversations.history": {
                    "ok": True,
                    "messages": [MESSAGE, MESSAGE],
                    "response_metadata": {"next_cursor": "more"},
                }
            }
        )
        found = await client(recorder).history("C1", max_results=2)

        assert found.complete is False

    async def test_they_can_be_kept(self) -> None:
        recorder = Recorder(
            {
                "conversations.history": {
                    "ok": True,
                    "messages": [{"ts": "2.0", "subtype": "channel_join"}],
                }
            }
        )
        assert len(await client(recorder).history("C1", include_joins=True)) == 1


class TestResolution:
    async def test_finding_a_channel_matches_exactly(self) -> None:
        """A prefix match would return #eng-alerts for #eng and post to the
        wrong room, which is worse than finding nothing."""
        recorder = Recorder(
            {
                "conversations.list": {
                    "ok": True,
                    "channels": [
                        {**CHANNEL, "id": "C1", "name": "eng-alerts"},
                        {**CHANNEL, "id": "C2", "name": "eng"},
                    ],
                }
            }
        )
        found = await client(recorder).find_channel("eng")

        assert found is not None
        assert found.id == "C2"

    async def test_a_leading_hash_is_accepted(self) -> None:
        """That is how people write it."""
        recorder = Recorder({"conversations.list": {"ok": True, "channels": [CHANNEL]}})
        assert (await client(recorder).find_channel("#incidents")).id == "C024BE91L"

    async def test_a_missing_channel_is_none_not_a_raise(self) -> None:
        recorder = Recorder({"conversations.list": {"ok": True, "channels": []}})
        assert await client(recorder).find_channel("nope") is None

    async def test_finding_a_channel_searches_private_ones_too(self) -> None:
        recorder = Recorder({"conversations.list": {"ok": True, "channels": []}})
        await client(recorder).find_channel("x")

        assert "private_channel" in recorder.query()["types"]

    async def test_an_unknown_email_is_none_not_a_raise(self) -> None:
        """"Nobody has that address" is an ordinary answer to branch on."""
        recorder = Recorder(
            {"users.lookupByEmail": {"ok": False, "error": "users_not_found"}}
        )
        assert await client(recorder).find_user_by_email("nope@example.com") is None

    async def test_a_real_failure_still_raises_from_the_lookup(self) -> None:
        """Swallowing everything would turn a missing scope into 'no such
        person', which sends the reader to the wrong problem entirely."""
        recorder = Recorder(
            {"users.lookupByEmail": {"ok": False, "error": "missing_scope",
                                     "needed": "users:read.email"}}
        )
        with pytest.raises(SlackMissingScope):
            await client(recorder).find_user_by_email("ada@example.com")

    async def test_a_found_email_returns_the_user(self) -> None:
        recorder = Recorder({"users.lookupByEmail": {"ok": True, "user": USER}})
        found = await client(recorder).find_user_by_email("ada@example.com")

        assert found is not None
        assert found.id == "U023BECGF"


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


class TestUpload:
    def _recorder(self) -> Recorder:
        return Recorder(
            {
                "files.getUploadURLExternal": {
                    "ok": True,
                    "upload_url": "https://files.slack.com/upload/xyz",
                    "file_id": "F123",
                },
                "files.slack.com/upload": httpx.Response(200, text="OK"),
                "files.completeUploadExternal": {
                    "ok": True,
                    "files": [{"id": "F123", "name": "log.txt",
                               "permalink": "https://slack.com/files/F123"}],
                },
            }
        )

    async def test_it_performs_all_three_steps_in_order(self) -> None:
        """files.upload stopped working in March 2025; its replacement is a
        protocol, and skipping the last step abandons the upload."""
        recorder = self._recorder()
        await client(recorder).upload_file("C1", b"hello", "log.txt")

        urls = [str(r.url) for r in recorder.requests]
        assert len(urls) == 3
        assert "files.getUploadURLExternal" in urls[0]
        assert urls[1] == "https://files.slack.com/upload/xyz"
        assert "files.completeUploadExternal" in urls[2]

    async def test_the_bytes_go_to_the_url_slack_named(self) -> None:
        recorder = self._recorder()
        await client(recorder).upload_file("C1", b"hello", "log.txt")

        assert recorder.requests[1].content == b"hello"

    async def test_the_bot_token_is_not_sent_to_the_upload_host(self) -> None:
        """It is a pre-signed URL on a different host — sending the credential
        there would leak it outside slack.com."""
        recorder = self._recorder()
        await client(recorder).upload_file("C1", b"hello", "log.txt")

        assert "authorization" not in recorder.requests[1].headers

    async def test_the_length_is_declared_up_front(self) -> None:
        recorder = self._recorder()
        await client(recorder).upload_file("C1", b"hello", "log.txt")

        assert recorder.query(0)["length"] == "5"

    async def test_text_is_encoded_as_utf8(self) -> None:
        recorder = self._recorder()
        await client(recorder).upload_file("C1", "héllo", "log.txt")

        assert recorder.requests[1].content == "héllo".encode()

    async def test_the_channel_and_comment_reach_the_completion_step(self) -> None:
        recorder = self._recorder()
        await client(recorder).upload_file(
            "C1", b"x", "log.txt", initial_comment="see this"
        )

        body = recorder.body(2)
        assert body["channel_id"] == "C1"
        assert body["initial_comment"] == "see this"
        assert body["files"] == [{"id": "F123", "title": "log.txt"}]

    async def test_a_failed_storage_post_is_reported(self) -> None:
        recorder = self._recorder()
        recorder._responses["files.slack.com/upload"] = httpx.Response(500, text="no")

        from loom.core.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError, match=r"log\.txt"):
            await client(recorder).upload_file("C1", b"x", "log.txt")


# ---------------------------------------------------------------------------
# Through a real Runtime
# ---------------------------------------------------------------------------


class TestThroughAWorkflow:
    async def test_a_paged_read_keeps_its_coverage_across_the_journal(self) -> None:
        from loom.toolsets.slack.tools import slack_read_channel

        recorder = Recorder(
            {
                "conversations.history": {
                    "ok": True,
                    "messages": [MESSAGE, {**MESSAGE, "ts": "2.0"}],
                    "response_metadata": {"next_cursor": "more"},
                }
            }
        )
        client = SlackClient("xoxb-test", transport=recorder.transport())

        @workflow(name="slack-coverage")
        async def flow(ctx: Context, _input: Any) -> dict[str, Any]:
            found = await ctx.step(slack_read_channel, "C1", 2)
            return {"count": len(found), "complete": found.complete,
                    "summary": found.summary(), "first": found[0].text}

        with use_clients(slack=client):
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.output == {
                "count": 2, "complete": False,
                "summary": "first 2 (more available)",
                "first": "Deploy finished.",
            }

            calls = len(recorder.requests)
            replayed = await runtime.replay(result.run_id)
            assert replayed.output == result.output
            assert len(recorder.requests) == calls, "replay re-called the API"

    async def test_posting_is_not_retried(self) -> None:
        """A retry posts the message twice, visibly, to everyone in the
        channel — so a transient failure surfaces instead."""
        from loom.toolsets.slack.tools import slack_post_message

        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(200, json={"ok": False, "error": "internal_error"})

        client = SlackClient("xoxb-test", transport=httpx.MockTransport(handler))

        @workflow(name="slack-post")
        async def flow(ctx: Context, _input: Any) -> str:
            posted = await ctx.step(slack_post_message, "C1", "hello")
            return posted.ts

        with use_clients(slack=client):
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.status.value == "failed"
            # internal_error is retryable in general; this step opts out
            # because the duplicate it would create is not recallable.
            assert len(attempts) == 1

    async def test_a_permanent_failure_fails_fast(self) -> None:
        from loom.toolsets.slack.tools import slack_read_channel

        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(
                200, json={"ok": False, "error": "channel_not_found"}
            )

        client = SlackClient("xoxb-test", transport=httpx.MockTransport(handler))

        @workflow(name="slack-permanent")
        async def flow(ctx: Context, _input: Any) -> int:
            return len(await ctx.step(slack_read_channel, "C000"))

        with use_clients(slack=client):
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.status.value == "failed"
            assert len(attempts) == 1, "a 200-with-ok:false must not be retried"

    async def test_typed_models_survive_the_journal(self) -> None:
        from loom.toolsets.slack.tools import slack_get_channel

        recorder = Recorder({"conversations.info": {"ok": True, "channel": CHANNEL}})
        client = SlackClient("xoxb-test", transport=recorder.transport())

        @workflow(name="slack-models")
        async def flow(ctx: Context, _input: Any) -> dict[str, Any]:
            first = await ctx.step(slack_get_channel, "C024BE91L")
            again = await ctx.step(slack_get_channel, "C024BE91L")
            return {"topic": again.topic, "member": again.is_member,
                    "same": first.id == again.id}

        with use_clients(slack=client):
            runtime = Runtime(store=MemoryStore())
            runtime.register(flow)
            result = await runtime.run(flow, {})
            assert result.output == {
                "topic": "Production incidents", "member": True, "same": True,
            }


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_no_token_anywhere_names_the_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.core.exceptions import ConfigurationError

        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_TOKEN", raising=False)

        with pytest.raises(ConfigurationError) as exc:
            SlackClient()
        assert "SLACK_BOT_TOKEN" in str(exc.value)
        assert "loom connect slack" in str(exc.value)

    async def test_a_connected_credential_beats_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`loom connect slack` is the newer, refreshable credential; a stale
        env var must not shadow it."""
        from loom.connectors.credentials import (
            MemoryCredentialStore,
            StoredCredential,
            credential_store_scope,
        )
        from loom.core.secret import Secret

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
        store = MemoryCredentialStore()
        await store.put("slack", StoredCredential(token=Secret("xoxb-connected")))

        recorder = Recorder({"conversations.list": {"ok": True, "channels": []}})
        with credential_store_scope(store):
            await SlackClient(transport=recorder.transport()).list_channels()

        assert recorder.last.headers["authorization"] == "Bearer xoxb-connected"

    async def test_the_token_it_was_given_reaches_the_wire(self) -> None:
        """Where the token came from is `ChainProvider`'s question now — this
        client reads no environment, so there is nothing here to fall back to.
        What it still owns is sending what it was handed, correctly prefixed."""
        recorder = Recorder({"conversations.list": {"ok": True, "channels": []}})

        await SlackClient("xoxb-given", transport=recorder.transport()).list_channels()

        assert recorder.last.headers["authorization"] == "Bearer xoxb-given"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_it_registers_and_is_findable(self) -> None:
        from loom.agents.tool_registry import ToolsetRegistry
        from loom.toolsets.slack import SLACK_MANIFEST

        registry = ToolsetRegistry()
        registry.register(SLACK_MANIFEST)

        assert SLACK_MANIFEST.qualified_id == "app:loom:slack"
        assert "slack" in {c.toolset_id for c in registry.search("post a message")}

    def test_both_resolvers_are_declared(self) -> None:
        """Channels and people are the two things a spec names and Slack does
        not accept."""
        from loom.toolsets.slack import SLACK_MANIFEST

        resolvers = {k: v.function for k, v in SLACK_MANIFEST.resolvers().items()}
        assert resolvers == {
            "channel": "slack_find_channel",
            "user": "slack_find_user_by_email",
        }

    def test_destructive_operations_are_marked(self) -> None:
        from loom.toolsets.manifest import EffectClass
        from loom.toolsets.slack import SLACK_MANIFEST

        for op_id in ("messages.delete", "conversations.archive"):
            assert (
                SLACK_MANIFEST.find_operation(op_id).effect
                is EffectClass.DESTRUCTIVE
            ), op_id
        assert (
            SLACK_MANIFEST.find_operation("messages.post").effect is EffectClass.WRITE
        )

    def test_posting_is_not_marked_idempotent(self) -> None:
        """It is the one claim that would license a retry."""
        from loom.toolsets.slack import SLACK_MANIFEST

        assert not SLACK_MANIFEST.find_operation("messages.post").idempotent
        assert SLACK_MANIFEST.find_operation("messages.update").idempotent

    def test_every_operation_declares_a_scope_and_a_summary(self) -> None:
        from loom.toolsets.slack import SLACK_MANIFEST

        for op in SLACK_MANIFEST.all_operations():
            assert op.summary, f"{op.id} has no summary"
            assert op.scopes, f"{op.id} declares no scope"

    def test_the_paged_reads_are_exactly_the_ones_that_page(self) -> None:
        from loom.toolsets.slack import SLACK_MANIFEST

        assert {op.function for op in SLACK_MANIFEST.paginated()} == {
            "slack_list_channels",
            "slack_read_channel",
            "slack_get_thread",
            "slack_list_channel_members",
            "slack_list_users",
        }

    def test_tool_docs_name_every_step(self) -> None:
        from loom.toolsets.slack import tools

        steps = [n for n in tools.__all__ if not n.isupper()]
        missing = [n for n in steps if n not in tools.SLACK_TOOL_DOCS]
        assert not missing, f"undocumented: {missing}"

    def test_the_docs_lead_with_the_id_rule(self) -> None:
        """The single most likely wrong turn: passing "#incidents" straight to
        post, which is a channel_not_found."""
        from loom.toolsets.slack import tools

        assert "NEVER A NAME" in tools.SLACK_TOOL_DOCS
        assert "slack_find_channel" in tools.SLACK_TOOL_DOCS


# ---------------------------------------------------------------------------
# The operations the earlier classes did not reach
# ---------------------------------------------------------------------------


class TestChannelManagement:
    async def test_joining_posts_the_channel(self) -> None:
        recorder = Recorder({"conversations.join": {"ok": True, "channel": CHANNEL}})
        joined = await client(recorder).join_channel("C024BE91L")

        assert recorder.body() == {"channel": "C024BE91L"}
        assert joined.is_member is True

    async def test_creating_sends_the_privacy_flag(self) -> None:
        recorder = Recorder({"conversations.create": {"ok": True, "channel": CHANNEL}})
        await client(recorder).create_channel("incidents", is_private=True)

        assert recorder.body() == {"name": "incidents", "is_private": True}

    async def test_inviting_joins_the_ids_slack_expects(self) -> None:
        """Slack takes a comma-separated string here, not a JSON array."""
        recorder = Recorder({"conversations.invite": {"ok": True, "channel": CHANNEL}})
        await client(recorder).invite("C1", ["U1", "U2"])

        assert recorder.body()["users"] == "U1,U2"

    async def test_inviting_more_than_slack_allows_is_refused_up_front(self) -> None:
        """Slack's own answer names neither the limit nor the argument."""
        recorder = Recorder()
        with pytest.raises(ValueError, match="1000"):
            await client(recorder).invite("C1", [f"U{i}" for i in range(1001)])

        assert not recorder.requests, "it must not reach the network"

    async def test_setting_a_topic(self) -> None:
        recorder = Recorder({"conversations.setTopic": {"ok": True, "channel": CHANNEL}})
        await client(recorder).set_topic("C1", "Now with fewer incidents")

        assert recorder.body()["topic"] == "Now with fewer incidents"

    async def test_archiving_returns_the_id_it_archived(self) -> None:
        recorder = Recorder({"conversations.archive": {"ok": True}})
        assert await client(recorder).archive_channel("C1") == "C1"

    async def test_members_come_back_as_bare_ids(self) -> None:
        recorder = Recorder(
            {"conversations.members": {"ok": True, "members": ["U1", "U2"]}}
        )
        found = await client(recorder).members("C1")

        assert list(found) == ["U1", "U2"]
        assert found.complete is True


class TestMessageOperations:
    async def test_a_thread_reply_can_broadcast(self) -> None:
        recorder = Recorder({"chat.postMessage": {"ok": True, "ts": "2.0"}})
        await client(recorder).post_message(
            "C1", "done", thread_ts="1.0", reply_broadcast=True
        )

        body = recorder.body()
        assert body["thread_ts"] == "1.0"
        assert body["reply_broadcast"] is True

    async def test_not_broadcasting_omits_the_flag_entirely(self) -> None:
        """Slack treats reply_broadcast=false and absent differently enough
        that sending the false is not worth the risk."""
        recorder = Recorder({"chat.postMessage": {"ok": True, "ts": "2.0"}})
        await client(recorder).post_message("C1", "done", thread_ts="1.0")

        assert "reply_broadcast" not in recorder.body()

    async def test_an_ephemeral_message_returns_a_non_durable_id(self) -> None:
        recorder = Recorder({"chat.postEphemeral": {"ok": True, "message_ts": "3.0"}})
        assert await client(recorder).post_ephemeral("C1", "U1", "just you") == "3.0"

    async def test_updating_carries_the_ts(self) -> None:
        recorder = Recorder(
            {"chat.update": {"ok": True, "ts": "1.0", "channel": "C1",
                             "message": {"text": "edited"}}}
        )
        updated = await client(recorder).update_message("C1", "1.0", "edited")

        assert recorder.body()["ts"] == "1.0"
        assert updated.text == "edited"

    async def test_deleting_returns_the_ts_it_deleted(self) -> None:
        recorder = Recorder({"chat.delete": {"ok": True}})
        assert await client(recorder).delete_message("C1", "1.0") == "1.0"

    async def test_scheduling_returns_the_scheduled_id_not_a_ts(self) -> None:
        """A scheduled message has no ts until it is actually sent, and
        cancelling one takes this id instead."""
        recorder = Recorder(
            {
                "chat.scheduleMessage": {
                    "ok": True, "channel": "C1", "post_at": 1900000000,
                    "scheduled_message_id": "Q1234",
                }
            }
        )
        scheduled = await client(recorder).schedule_message("C1", "later", 1900000000)

        assert scheduled.scheduled_message_id == "Q1234"
        assert recorder.body()["post_at"] == 1900000000

    async def test_a_reaction_strips_the_colons_people_type(self) -> None:
        recorder = Recorder({"reactions.add": {"ok": True}})
        await client(recorder).add_reaction("C1", "1.0", ":white_check_mark:")

        assert recorder.body()["name"] == "white_check_mark"
        assert recorder.body()["timestamp"] == "1.0"

    async def test_a_permalink_is_returned_as_a_string(self) -> None:
        recorder = Recorder(
            {"chat.getPermalink": {"ok": True,
                                   "permalink": "https://x.slack.com/archives/C1/p1"}}
        )
        assert (await client(recorder).permalink("C1", "1.0")).endswith("p1")


class TestUsersAndFiles:
    async def test_fetching_one_user(self) -> None:
        recorder = Recorder({"users.info": {"ok": True, "user": USER}})
        found = await client(recorder).get_user("U023BECGF")

        assert found.real_name == "Ada Lovelace"
        assert recorder.query()["user"] == "U023BECGF"

    async def test_listing_users_reads_the_members_key(self) -> None:
        recorder = Recorder({"users.list": {"ok": True, "members": [USER]}})
        assert [u.id for u in await client(recorder).list_users()] == ["U023BECGF"]

    async def test_downloading_a_file_sends_the_bot_token(self) -> None:
        """url_private is not public — a plain fetch returns a sign-in page."""
        recorder = Recorder(
            {"files.slack.com": httpx.Response(200, content=b"log contents")}
        )
        attachment = await client(recorder).download_file(
            "https://files.slack.com/files-pri/T1-F1/log.txt", "log.txt"
        )

        assert attachment.data == b"log contents"
        assert recorder.last.headers["authorization"] == "Bearer xoxb-test"

    async def test_a_failed_download_is_reported(self) -> None:
        from loom.core.exceptions import ConfigurationError

        recorder = Recorder({"files.slack.com": httpx.Response(404, text="gone")})
        with pytest.raises(ConfigurationError, match="log"):
            await client(recorder).download_file(
                "https://files.slack.com/x", "log.txt"
            )


class TestResolutionIsHonestAboutItsScan:
    """A resolver that pages has a third answer, and it matters.

    "Not found" is a fact a caller acts on — creates the channel, reports the
    gap. It is only a fact if the whole list was searched. Returning None from
    a truncated scan silently loses channels that plainly exist.
    """

    async def test_a_truncated_scan_raises_rather_than_saying_not_found(
        self,
    ) -> None:
        recorder = Recorder(
            {
                "conversations.list": {
                    "ok": True,
                    "channels": [{**CHANNEL, "name": "other"}],
                    "response_metadata": {"next_cursor": "still-more"},
                }
            }
        )
        with pytest.raises(SlackPermanentError) as exc:
            await client(recorder).find_channel("incidents")

        assert "not a 'does not exist'" in str(exc.value)
        assert exc.value.error == "resolution_incomplete"

    async def test_a_complete_scan_still_answers_none(self) -> None:
        recorder = Recorder(
            {"conversations.list": {"ok": True, "channels": [],
                                    "response_metadata": {"next_cursor": ""}}}
        )
        assert await client(recorder).find_channel("nope") is None

    async def test_a_hit_inside_a_truncated_scan_is_still_a_hit(self) -> None:
        """Only the *absence* is ambiguous."""
        recorder = Recorder(
            {
                "conversations.list": {
                    "ok": True,
                    "channels": [CHANNEL],
                    "response_metadata": {"next_cursor": "more"},
                }
            }
        )
        found = await client(recorder).find_channel("incidents")

        assert found is not None and found.id == "C024BE91L"


class TestConfigurableTimeouts:
    """A 30-second budget suits an API call and fails every large transfer.

    A caller who has to subclass the client to say so does not have a
    configurable timeout, which is why the two are separate and both settable.
    """

    def test_the_api_timeout_reaches_the_session(self) -> None:
        assert SlackClient("xoxb-t", timeout=5.0)._session._timeout == 5.0

    def test_transfers_have_their_own_longer_default(self) -> None:
        from loom.toolsets.slack.client import (
            DEFAULT_TIMEOUT,
            DEFAULT_TRANSFER_TIMEOUT,
        )

        assert DEFAULT_TRANSFER_TIMEOUT > DEFAULT_TIMEOUT
        assert SlackClient("xoxb-t")._transfer_timeout == DEFAULT_TRANSFER_TIMEOUT

    def test_the_transfer_timeout_is_settable(self) -> None:
        assert SlackClient("xoxb-t", transfer_timeout=900.0)._transfer_timeout == 900.0
