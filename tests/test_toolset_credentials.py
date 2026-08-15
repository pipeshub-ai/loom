"""Phase 7: toolsets read a run's ``CredentialStore``, with env/basic-auth fallback.

Three claims this file has to make good on, because they were the whole
point of the phase:

1. **No behaviour change when unconfigured.** Every toolset call site that
   worked before ``CredentialStore`` existed still does exactly the same
   thing when no store is bound to the current run — ``current_credential_
   store()`` returns ``None`` and every client falls straight through to its
   old env-var/constructor-arg resolution.
2. **Store first, environment second.** A store that *does* have a
   credential under a toolset's name wins over whatever env vars happen to
   be set, and is re-checked on every call rather than cached once.
3. **An expired, unrefreshable credential parks the run.** ``AuthExpired``
   raised from inside a step reaches ``runtime/engine.py`` and turns into a
   ``Suspend`` on ``credential:<name>`` — never a bare failure, and never a
   pointless retry loop first (see ``core/retry.py::PERMANENT_ERRORS``).
"""

from __future__ import annotations

from typing import Any

import pytest

from loom import Context, Runtime, workflow
from loom.connectors.credentials import (
    MemoryCredentialStore,
    StoredCredential,
    credential_store_scope,
    current_credential_store,
    resolve_bearer_token,
)
from loom.core.exceptions import AuthExpired
from loom.core.models import ExecutionStatus
from loom.core.retry import PERMANENT_ERRORS
from loom.core.secret import Secret
from loom.identity.facade import PRINCIPAL_KEY
from loom.identity.principal import ServicePrincipal
from loom.runtime.dispatcher import TriggerDispatcher
from loom.stores.memory import MemoryStore
from loom.toolsets.confluence.client import ConfluenceClient
from loom.toolsets.google.auth import GoogleAuth, GoogleCredentials
from loom.toolsets.google.errors import GoogleAuthError
from loom.toolsets.jira.client import JiraClient


def _cred(token: str, **overrides: Any) -> StoredCredential:
    return StoredCredential(token=Secret(token), **overrides)


@pytest.fixture(autouse=True)
def _no_ambient_toolset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip whatever real Jira/Google/Confluence credentials the developer's
    own ``.env`` (loaded by ``examples/cookbook/utils.py::load_dotenv`` when
    another test module imports a cookbook example) may have left sitting in
    ``os.environ`` for the rest of the process. Every "nothing configured"
    assertion below means it literally, not "nothing this repo's checkout
    happens to have"."""
    for name in (
        "JIRA_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "CONFLUENCE_URL",
        "CONFLUENCE_EMAIL",
        "CONFLUENCE_API_TOKEN",
        "GOOGLE_ACCESS_TOKEN",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# The contextvar plumbing itself
# ---------------------------------------------------------------------------


class TestCurrentCredentialStore:
    def test_defaults_to_none(self) -> None:
        assert current_credential_store() is None

    async def test_scope_binds_for_its_block_only(self) -> None:
        store = MemoryCredentialStore()
        assert current_credential_store() is None
        with credential_store_scope(store):
            assert current_credential_store() is store
        assert current_credential_store() is None

    async def test_scope_restores_the_previous_value_on_exit(self) -> None:
        outer = MemoryCredentialStore()
        inner = MemoryCredentialStore()
        with credential_store_scope(outer):
            with credential_store_scope(inner):
                assert current_credential_store() is inner
            assert current_credential_store() is outer

    async def test_resolve_bearer_token_is_none_with_no_store_bound(self) -> None:
        assert await resolve_bearer_token("jira") is None

    async def test_resolve_bearer_token_is_none_when_store_lacks_the_name(self) -> None:
        with credential_store_scope(MemoryCredentialStore()):
            assert await resolve_bearer_token("jira") is None

    async def test_resolve_bearer_token_reveals_the_stored_secret(self) -> None:
        store = MemoryCredentialStore()
        await store.put("jira", _cred("tok-123"))
        with credential_store_scope(store):
            assert await resolve_bearer_token("jira") == "tok-123"

    async def test_resolve_bearer_token_propagates_auth_expired(self) -> None:
        store = MemoryCredentialStore()  # no refresher configured
        await store.put(
            "jira",
            _cred("stale", expires_at=__import__("datetime").datetime(2000, 1, 1)),
        )
        with credential_store_scope(store), pytest.raises(AuthExpired):
            await resolve_bearer_token("jira")


# ---------------------------------------------------------------------------
# Jira / Confluence: Basic auth by default, bearer when a store covers it
# ---------------------------------------------------------------------------


class TestJiraClientCredentials:
    async def test_uses_basic_auth_when_no_store_is_bound(self) -> None:
        """Byte-for-byte the pre-Phase-7 behaviour."""
        client = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")
        headers = await client._headers()
        assert headers["Authorization"].startswith("Basic ")

    async def test_falls_back_to_basic_auth_when_store_lacks_jira(self) -> None:
        client = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")
        with credential_store_scope(MemoryCredentialStore()):
            headers = await client._headers()
        assert headers["Authorization"].startswith("Basic ")

    async def test_prefers_a_stored_credential_over_basic_auth(self) -> None:
        client = JiraClient(base_url="https://x.atlassian.net", email="a@b.c", api_token="t")
        store = MemoryCredentialStore()
        await store.put("jira", _cred("bearer-tok"))
        with credential_store_scope(store):
            headers = await client._headers()
        assert headers["Authorization"] == "Bearer bearer-tok"

    async def test_construction_defers_when_a_store_might_cover_it(self) -> None:
        """No email/token, but a store is bound — construction must not raise
        eagerly, since whether it actually has 'jira' can only be known with
        an await, at first real call."""
        with credential_store_scope(MemoryCredentialStore()):
            client = JiraClient(base_url="https://x.atlassian.net")
        assert client._credential_name == "jira"

    async def test_construction_still_raises_with_nothing_configured_at_all(self) -> None:
        with pytest.raises(ValueError, match="JIRA_EMAIL"):
            JiraClient(base_url="https://x.atlassian.net")

    async def test_headers_raise_when_the_deferred_store_has_nothing_either(self) -> None:
        with credential_store_scope(MemoryCredentialStore()):
            client = JiraClient(base_url="https://x.atlassian.net")
            with pytest.raises(ValueError, match="JIRA_EMAIL"):
                await client._headers()

    async def test_custom_credential_name_is_respected(self) -> None:
        client = JiraClient(
            base_url="https://x.atlassian.net",
            email="a@b.c",
            api_token="t",
            credential_name="jira-prod",
        )
        store = MemoryCredentialStore()
        await store.put("jira-prod", _cred("prod-tok"))
        with credential_store_scope(store):
            headers = await client._headers()
        assert headers["Authorization"] == "Bearer prod-tok"


class TestConfluenceClientCredentials:
    async def test_uses_basic_auth_when_no_store_is_bound(self) -> None:
        client = ConfluenceClient(
            base_url="https://x.atlassian.net", email="a@b.c", api_token="t"
        )
        headers = await client._headers()
        assert headers["Authorization"].startswith("Basic ")

    async def test_prefers_a_stored_credential_over_basic_auth(self) -> None:
        client = ConfluenceClient(
            base_url="https://x.atlassian.net", email="a@b.c", api_token="t"
        )
        store = MemoryCredentialStore()
        await store.put("confluence", _cred("bearer-tok"))
        with credential_store_scope(store):
            headers = await client._headers()
        assert headers["Authorization"] == "Bearer bearer-tok"


# ---------------------------------------------------------------------------
# Google: GoogleAuth.token() checks the store on every call, never caches it
# ---------------------------------------------------------------------------


class TestGoogleAuthCredentials:
    async def test_uses_env_credentials_when_no_store_is_bound(self) -> None:
        """Byte-for-byte the pre-Phase-7 behaviour."""
        auth = GoogleAuth(GoogleCredentials(access_token="env-tok"))
        assert await auth.token() == "env-tok"

    async def test_prefers_a_stored_credential_over_the_static_token(self) -> None:
        auth = GoogleAuth(GoogleCredentials(access_token="env-tok"))
        store = MemoryCredentialStore()
        await store.put("google", _cred("store-tok"))
        with credential_store_scope(store):
            assert await auth.token() == "store-tok"
        # And reverts the moment the scope exits — no caching leak.
        assert await auth.token() == "env-tok"

    async def test_construction_defers_when_a_store_might_cover_it(self) -> None:
        with credential_store_scope(MemoryCredentialStore()):
            auth = GoogleAuth()  # no env vars either
        assert auth.mode == ""

    async def test_construction_still_raises_with_nothing_configured_at_all(self) -> None:
        with pytest.raises(GoogleAuthError):
            GoogleAuth(GoogleCredentials())

    async def test_token_raises_when_the_deferred_store_has_nothing_either(self) -> None:
        with credential_store_scope(MemoryCredentialStore()):
            auth = GoogleAuth()
            with pytest.raises(GoogleAuthError):
                await auth.token()

    async def test_a_store_credential_is_rechecked_every_call_not_cached(self) -> None:
        """The whole reason this does not just resolve once and cache like the
        access-token mode does: a store manages its own refresh, so caching
        its answer here would serve a token an hour after the store refreshed."""
        auth = GoogleAuth(GoogleCredentials(access_token="env-tok"))
        store = MemoryCredentialStore()
        await store.put("google", _cred("first"))
        with credential_store_scope(store):
            assert await auth.token() == "first"
            await store.put("google", _cred("second"))
            assert await auth.token() == "second"

    async def test_custom_credential_name_is_respected(self) -> None:
        auth = GoogleAuth(GoogleCredentials(access_token="env-tok"), credential_name="gmail-2")
        store = MemoryCredentialStore()
        await store.put("gmail-2", _cred("store-tok"))
        with credential_store_scope(store):
            assert await auth.token() == "store-tok"


# ---------------------------------------------------------------------------
# Engine: AuthExpired parks the run instead of failing it
# ---------------------------------------------------------------------------


class TestAuthExpiredParksTheRun:
    async def test_auth_expired_is_a_permanent_error(self) -> None:
        """Retrying inside the step's own backoff window cannot produce the
        human reauthorization the credential needs — see PERMANENT_ERRORS."""
        assert AuthExpired in PERMANENT_ERRORS

    async def test_a_credential_lookup_that_raises_auth_expired_suspends_the_run(self) -> None:
        from loom import step

        @step
        async def touch_jira() -> str:
            raise AuthExpired("credential 'jira' has expired", name="jira")

        @workflow(name="uses_jira")
        async def uses_jira(ctx: Context, _: None = None) -> str:
            return await ctx.step(touch_jira)

        rt = Runtime(store=MemoryStore())
        result = await rt.run(uses_jira)

        assert result.status is ExecutionStatus.SUSPENDED
        record = await rt.get(result.run_id)
        assert record is not None
        assert record.awaiting_event == "credential:jira"

    async def test_resuming_after_reauthorization_re_enters_and_completes(self) -> None:
        """The exact shape 'loom connect jira' + event delivery produces:
        the second attempt sees working credentials and the run completes,
        with no special-casing needed in the step itself."""
        from loom import step

        attempts: list[int] = []

        @step
        async def touch_jira() -> str:
            attempts.append(1)
            if len(attempts) == 1:
                raise AuthExpired("credential 'jira' has expired", name="jira")
            return "ok"

        @workflow(name="uses_jira_2")
        async def uses_jira_2(ctx: Context, _: None = None) -> str:
            return await ctx.step(touch_jira)

        rt = Runtime(store=MemoryStore())
        first = await rt.run(uses_jira_2)
        assert first.status is ExecutionStatus.SUSPENDED

        second = await rt.resume(first.run_id)
        assert second.status is ExecutionStatus.COMPLETED
        assert second.output == "ok"

    async def test_broadcast_send_event_finds_and_resumes_a_parked_run(self) -> None:
        """The mechanism 'loom connect <name>' relies on: a broadcast by
        event name alone (no run_id) reaches every run parked on it."""
        from loom import step

        attempts: list[int] = []

        @step
        async def touch_jira() -> str:
            attempts.append(1)
            if len(attempts) == 1:
                raise AuthExpired("credential 'jira' has expired", name="jira")
            return "ok"

        @workflow(name="uses_jira_3")
        async def uses_jira_3(ctx: Context, _: None = None) -> str:
            return await ctx.step(touch_jira)

        rt = Runtime(store=MemoryStore())
        first = await rt.run(uses_jira_3)
        assert first.status is ExecutionStatus.SUSPENDED

        await rt.send_event(None, "credential:jira")
        second = await rt.wait(first.run_id, timeout=2.0)
        assert second.status is ExecutionStatus.COMPLETED
        assert second.output == "ok"

    async def test_park_and_resume_matches_the_clis_exit_codes_and_replays_nothing(
        self,
    ) -> None:
        """The full ``loom run`` -> ``loom connect`` -> resume shape, asserted
        against the CLI's own exit-code contract (``3`` suspended, ``0``
        completed) rather than against ``ExecutionStatus`` alone — that
        contract is what a calling script actually branches on. A first step
        that already completed must not re-run when the second step's parked
        credential is delivered and the workflow re-enters."""
        from loom import step
        from loom.cli.output import Exit, exit_for
        from loom.facade import LocalFacade

        first_step_runs: list[int] = []
        second_step_attempts: list[int] = []

        @step
        async def record_something() -> str:
            first_step_runs.append(1)
            return "recorded"

        @step
        async def touch_jira() -> str:
            second_step_attempts.append(1)
            if len(second_step_attempts) == 1:
                raise AuthExpired("credential 'jira' has expired", name="jira")
            return "ok"

        @workflow(name="park_and_resume_e2e")
        async def park_and_resume_e2e(ctx: Context, _: None = None) -> str:
            await ctx.step(record_something)
            return await ctx.step(touch_jira)

        rt = Runtime(store=MemoryStore())
        facade = LocalFacade(rt)
        first = await rt.run(park_and_resume_e2e)
        assert first.status is ExecutionStatus.SUSPENDED
        assert len(first_step_runs) == 1

        # facade.get() is what `loom run`/`loom show` actually branch on —
        # the CLI never sees an ExecutionRecord, only this JSON-shaped dict.
        parked_run = await facade.get(first.run_id)
        assert parked_run is not None
        assert exit_for(parked_run) == Exit.SUSPENDED == 3

        # "loom connect jira" resolves; the credential's park is answered by
        # name, exactly as rt.send_event(None, "credential:<name>") does.
        second = await rt.resume(first.run_id)

        assert second.status is ExecutionStatus.COMPLETED
        assert second.output == "ok"
        assert len(first_step_runs) == 1, "the already-completed step re-ran"
        assert len(second_step_attempts) == 2  # once parked, once resumed

        completed_run = await facade.get(second.run_id)
        assert completed_run is not None
        assert exit_for(completed_run) == Exit.OK == 0

    async def test_a_real_credential_store_expiry_flows_through_end_to_end(self) -> None:
        """The realistic path: a step reads ctx.credentials directly (as
        toolset clients do via the current_credential_store() contextvar),
        the store's own expiry check raises AuthExpired, and the run parks
        on the same store-derived name with no special-casing in the step."""
        from loom import step

        @step
        async def call_jira() -> str:
            # The store has 'jira', but it is expired with no refresher
            # configured -> resolve_bearer_token lets AuthExpired through
            # rather than quietly returning None, same as a real toolset call.
            await resolve_bearer_token("jira")
            return "unreachable"

        @workflow(name="uses_store_directly")
        async def uses_store_directly(ctx: Context, _: None = None) -> str:
            return await ctx.step(call_jira)

        store = MemoryCredentialStore()
        await store.put(
            "jira", _cred("stale", expires_at=__import__("datetime").datetime(2000, 1, 1))
        )
        rt = Runtime(store=MemoryStore(), credentials=store)
        result = await rt.run(uses_store_directly)

        assert result.status is ExecutionStatus.SUSPENDED
        record = await rt.get(result.run_id)
        assert record is not None
        assert record.awaiting_event == "credential:jira"

    async def test_no_credential_store_configured_behaves_exactly_as_before(self) -> None:
        """Runtime(credentials=None) is the default — nothing here should be
        reachable, and a step that fails for an ordinary reason still just
        fails, not suspends."""
        from loom import step

        @step
        async def boom() -> str:
            raise ValueError("ordinary failure")

        @workflow(name="ordinary_failure")
        async def ordinary_failure(ctx: Context, _: None = None) -> str:
            return await ctx.step(boom)

        rt = Runtime(store=MemoryStore())
        assert rt.credentials is None
        result = await rt.run(ordinary_failure)
        assert result.status is ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# No-secret-escapes: the test that would have caught the Credential-is-a-
# BaseModel leak. A workflow consumes a real credential end to end, and the
# literal secret string must appear in none of: the journal, the on-disk
# store dump, the HTTP response, or the CLI's own ``--json`` serialization.
# ---------------------------------------------------------------------------


class TestNoSecretEscapes:
    async def test_the_secret_never_appears_in_journal_store_http_or_cli_json(
        self, tmp_path: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        import httpx

        from loom import step
        from loom.cli.output import Printer
        from loom.connectors.credentials import EncryptedFileCredentialStore
        from loom.facade import LocalFacade
        from loom.server.app import create_app

        marker = "sk-super-secret-token-do-not-leak-9f3c2a"

        store = EncryptedFileCredentialStore(path=tmp_path / "creds.enc")
        await store.put("jira", _cred(marker))

        @step
        async def call_jira() -> str:
            token = await resolve_bearer_token("jira")
            assert token == marker
            # A well-behaved toolset call only ever *uses* the token (e.g. in
            # an Authorization header); it never returns or logs it.
            return f"called jira with a token of length {len(token)}"

        @workflow(name="consumes_a_credential")
        async def consumes_a_credential(ctx: Context, _: None = None) -> str:
            return await ctx.step(call_jira)

        rt = Runtime(store=MemoryStore(), credentials=store)
        result = await rt.run(consumes_a_credential)
        assert result.status is ExecutionStatus.COMPLETED
        assert marker not in result.output

        facade = LocalFacade(rt)

        # 1. The journal.
        journal = await facade.journal(result.run_id)
        journal_text = json.dumps(journal, default=str)
        assert marker not in journal_text

        # 2. The store dump — the raw encrypted bytes on disk, plus repr()
        # of the store and of the StoredCredential itself (a careless `log
        # .debug(store)` or `print(credential)` must not leak either).
        on_disk = (tmp_path / "creds.enc").read_bytes()
        assert marker.encode() not in on_disk
        assert marker not in repr(store)
        stored = await store.peek("jira")
        assert stored is not None
        assert marker not in repr(stored)
        assert marker not in str(stored)

        # 3. The HTTP response — /runs/{id} and /runs/{id}/journal.
        app = create_app(rt)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://loom.test"
        ) as http:
            run_response = await http.get(f"/runs/{result.run_id}")
            journal_response = await http.get(f"/runs/{result.run_id}/journal")
        assert marker not in run_response.text
        assert marker not in journal_response.text

        # 4. The CLI's own --json serialization path (Printer.json, the exact
        # method `loom show --json`/`loom run --json` call on the same dict).
        run_dict = await facade.get(result.run_id)
        assert run_dict is not None
        printer = Printer(as_json=True)
        printer.json(run_dict)
        printer.json(journal)
        printed = capsys.readouterr().out
        assert marker not in printed

    async def test_an_expired_unrefreshable_credential_leaks_nothing_either(
        self, tmp_path: Any
    ) -> None:
        """The failure path matters too: AuthExpired's own message must not
        embed the (already-expired) secret value."""
        from loom.connectors.credentials import EncryptedFileCredentialStore

        marker = "sk-expired-secret-should-not-appear-in-any-error-77abf1"
        store = EncryptedFileCredentialStore(path=tmp_path / "creds.enc")
        await store.put(
            "jira",
            _cred(marker, expires_at=__import__("datetime").datetime(2000, 1, 1)),
        )

        with credential_store_scope(store), pytest.raises(AuthExpired) as caught:
            await resolve_bearer_token("jira")
        assert marker not in str(caught.value)
        assert marker not in repr(caught.value)


# ---------------------------------------------------------------------------
# Service identity for scheduled runs
# ---------------------------------------------------------------------------


class TestServiceIdentity:
    def test_runtime_defaults_to_a_scheduler_service_principal(self) -> None:
        rt = Runtime(store=MemoryStore())
        assert isinstance(rt.service_principal, ServicePrincipal)
        assert rt.service_principal.subject == "scheduler"
        assert rt.service_principal.kind == "service"

    def test_a_custom_service_principal_is_honoured(self) -> None:
        custom = ServicePrincipal(subject="nightly-etl")
        rt = Runtime(store=MemoryStore(), service_principal=custom)
        assert rt.service_principal is custom

    async def test_a_scheduled_run_is_pinned_to_the_service_principal(self) -> None:
        from datetime import UTC, datetime, timedelta

        from loom.triggers.specs import Schedule

        @workflow(name="nightly", triggers=[Schedule("* * * * *")])
        async def nightly(ctx: Context, _: None = None) -> str:
            return "ran"

        rt = Runtime(store=MemoryStore())
        dispatcher = TriggerDispatcher(rt)
        await dispatcher.register(nightly)

        future = datetime.now(UTC) + timedelta(minutes=2)
        run_ids = await dispatcher.tick(future)
        assert len(run_ids) == 1

        record = await rt.get(run_ids[0])
        assert record is not None
        assert record.metadata.get(PRINCIPAL_KEY) == "scheduler"

    async def test_a_manually_submitted_run_is_not_pinned_to_the_scheduler(self) -> None:
        """Only TriggerDispatcher stamps this — a plain submit() keeps
        whatever metadata (or none) its own caller supplied."""

        @workflow(name="manual_wf")
        async def manual_wf(ctx: Context, _: None = None) -> str:
            return "ran"

        rt = Runtime(store=MemoryStore())
        run_id = await rt.submit(manual_wf)
        record = await rt.get(run_id)
        assert record is not None
        assert PRINCIPAL_KEY not in record.metadata
