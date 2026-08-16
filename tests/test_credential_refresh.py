"""Proactive OAuth refresh: the policy, the sweeper, and the CLI around them.

The store's own early-refresh behaviour is in
``tests/test_credential_store_conformance.py``, where it runs against all three
store implementations. This file covers what sits either side of it: the policy
object that decides *when*, and the background service that asks *without being
asked* — the half that matters for a process which stays up for a week and
touches a credential twice.

Everything runs on a ``ManualClock``, so a ten-minute window and an hour of
backoff are exercised in milliseconds.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from loom.cli import auth_commands
from loom.cli.output import Exit
from loom.connectors.credentials import (
    DEFAULT_REFRESH_SKEW,
    REFRESH_SKEW_ENV,
    MemoryCredentialStore,
    RefreshPolicy,
    StoredCredential,
)
from loom.connectors.refresh import (
    INITIAL_BACKOFF,
    MAX_BACKOFF,
    CredentialRefreshService,
    service_for,
)
from loom.core.exceptions import AuthExpired
from loom.core.secret import Secret
from loom.runtime.clock import ManualClock

NOON = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def credential(
    token: str = "tok",
    *,
    minutes_left: float = 60.0,
    lifetime_minutes: float = 120.0,
    refreshable: bool = True,
) -> StoredCredential:
    expires_at = NOON + timedelta(minutes=minutes_left)
    return StoredCredential(
        token=Secret(token),
        refresh_token=Secret("refresh-me") if refreshable else None,
        expires_at=expires_at,
        issued_at=expires_at - timedelta(minutes=lifetime_minutes),
    )


class Refresher:
    """Mints a fresh, long-lived credential and counts how often it is asked."""

    def __init__(self, *, fails: bool = False, clock: ManualClock | None = None) -> None:
        self.calls = 0
        self.fails = fails
        self._clock = clock

    async def refresh(self, name: str, stored: StoredCredential) -> StoredCredential:
        self.calls += 1
        if self.fails:
            raise AuthExpired(f"cannot refresh {name}", name=name)
        now = self._clock.now() if self._clock else NOON
        return StoredCredential(
            token=Secret(f"fresh-{self.calls}"),
            refresh_token=Secret("refresh-me"),
            expires_at=now + timedelta(hours=2),
            issued_at=now,
        )


def build(
    credentials: dict[str, StoredCredential] | None = None, *, fails: bool = False
) -> tuple[MemoryCredentialStore, ManualClock, Refresher]:
    clock = ManualClock(NOON)
    refresher = Refresher(fails=fails, clock=clock)
    store = MemoryCredentialStore(refresher=refresher, clock=clock)
    for name, cred in (credentials or {}).items():
        store._data[name] = cred
    return store, clock, refresher


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------


class TestRefreshPolicy:
    def test_the_default_window_is_ten_minutes(self) -> None:
        assert RefreshPolicy().skew == DEFAULT_REFRESH_SKEW == timedelta(minutes=10)

    def test_a_credential_with_no_expiry_is_never_due(self) -> None:
        """The issuer said nothing about a lifetime; renewing on a schedule
        this code invented would spend a refresh token to answer a question
        nobody asked."""
        policy = RefreshPolicy()
        forever = StoredCredential(token=Secret("t"))

        assert policy.due_at(forever) is None
        assert not policy.is_due(forever, ManualClock(NOON))

    def test_due_at_is_expiry_minus_the_window(self) -> None:
        policy = RefreshPolicy()
        cred = credential(minutes_left=60)

        assert policy.due_at(cred) == NOON + timedelta(minutes=50)

    def test_the_window_is_clamped_to_half_a_short_lifetime(self) -> None:
        """A five-minute token under a ten-minute window would otherwise be due
        the instant it was minted — every call refreshing, forever."""
        policy = RefreshPolicy()
        short = credential(minutes_left=5, lifetime_minutes=5)

        assert policy.effective_skew(short) == timedelta(minutes=2.5)
        assert not policy.is_due(short, ManualClock(NOON))

    def test_a_long_lifetime_is_not_clamped(self) -> None:
        policy = RefreshPolicy()
        long = credential(minutes_left=60, lifetime_minutes=120)

        assert policy.effective_skew(long) == DEFAULT_REFRESH_SKEW

    def test_an_unknown_lifetime_uses_the_whole_window(self) -> None:
        policy = RefreshPolicy()
        legacy = StoredCredential(
            token=Secret("t"), expires_at=NOON + timedelta(minutes=5)
        )

        assert policy.effective_skew(legacy) == DEFAULT_REFRESH_SKEW
        assert policy.is_due(legacy, ManualClock(NOON))

    def test_an_already_expired_credential_is_due(self) -> None:
        assert RefreshPolicy().is_due(credential(minutes_left=-30), ManualClock(NOON))

    def test_a_zero_lifetime_does_not_divide_by_anything(self) -> None:
        """A malformed record must not crash the sweep for every other one."""
        broken = StoredCredential(token=Secret("t"), expires_at=NOON, issued_at=NOON)

        assert RefreshPolicy().effective_skew(broken) == timedelta(0)

    def test_a_freshly_minted_token_is_never_immediately_due(self) -> None:
        """What makes it safe for OAuthClient to judge another process's write
        by this policy: otherwise two processes could bounce one credential
        back and forth forever."""
        policy = RefreshPolicy()
        for lifetime in (1, 5, 30, 60, 3600):
            fresh = credential(minutes_left=lifetime, lifetime_minutes=lifetime)
            assert not policy.is_due(fresh, ManualClock(NOON)), lifetime


class TestPolicyFromEnv:
    def test_it_reads_seconds(self) -> None:
        assert RefreshPolicy.from_env({REFRESH_SKEW_ENV: "300"}).skew == timedelta(
            minutes=5
        )

    def test_an_unset_variable_is_the_default(self) -> None:
        assert RefreshPolicy.from_env({}).skew == DEFAULT_REFRESH_SKEW

    @pytest.mark.parametrize("bad", ["soon", "", "-60"])
    def test_a_bad_value_falls_back_rather_than_disabling_refresh(
        self, bad: str
    ) -> None:
        """Adopting a negative or unparseable value would silently turn early
        refresh off, which is the failure this setting exists to prevent."""
        assert RefreshPolicy.from_env({REFRESH_SKEW_ENV: bad}).skew == (
            DEFAULT_REFRESH_SKEW
        )

    def test_a_bad_value_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="workflow.credentials"):
            RefreshPolicy.from_env({REFRESH_SKEW_ENV: "soon"})
        assert REFRESH_SKEW_ENV in caplog.text


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


class TestSweep:
    async def test_it_renews_what_is_due_and_leaves_the_rest(self) -> None:
        store, clock, refresher = build(
            {
                "soon": credential("soon-old", minutes_left=5),
                "later": credential("later-current", minutes_left=90),
            }
        )
        report = await CredentialRefreshService(store, clock=clock).sweep()

        assert [o.name for o in report.refreshed] == ["soon"]
        assert (await store.peek("soon")).token.reveal() == "fresh-1"
        assert (await store.peek("later")).token.reveal() == "later-current"
        assert refresher.calls == 1

    async def test_it_reports_every_credential_not_only_the_renewed_ones(
        self,
    ) -> None:
        store, clock, _ = build(
            {"a": credential(minutes_left=5), "b": credential(minutes_left=90)}
        )
        report = await CredentialRefreshService(store, clock=clock).sweep()

        assert {o.name: o.status for o in report.outcomes} == {
            "a": "refreshed",
            "b": "current",
        }

    async def test_a_credential_with_no_refresh_token_is_skipped_not_attempted(
        self,
    ) -> None:
        """Asking is a guaranteed AuthExpired and a wasted request every sweep,
        forever."""
        store, clock, refresher = build(
            {"manual": credential(minutes_left=1, refreshable=False)}
        )
        report = await CredentialRefreshService(store, clock=clock).sweep()

        assert report.outcomes[0].status == "skipped"
        assert "loom connect manual" in report.outcomes[0].detail
        assert refresher.calls == 0

    async def test_it_can_be_narrowed_to_named_credentials(self) -> None:
        store, clock, _ = build(
            {"a": credential(minutes_left=5), "b": credential(minutes_left=5)}
        )
        report = await CredentialRefreshService(store, clock=clock).sweep(["a"])

        assert [o.name for o in report.outcomes] == ["a"]
        assert (await store.peek("b")).token.reveal() == "tok"

    async def test_an_empty_store_sweeps_cleanly(self) -> None:
        store, clock, _ = build()
        assert len(await CredentialRefreshService(store, clock=clock).sweep()) == 0

    async def test_force_renews_something_that_is_not_due(self) -> None:
        store, clock, refresher = build({"a": credential(minutes_left=90)})
        report = await CredentialRefreshService(store, clock=clock).sweep(force=True)

        assert report.outcomes[0].status == "refreshed"
        assert refresher.calls == 1

    async def test_a_store_that_cannot_be_read_does_not_raise(self) -> None:
        """A background loop that dies on a corrupt store leaves the process
        serving with no safety net and nothing saying so."""

        class Unreadable(MemoryCredentialStore):
            async def peek_all(self) -> dict[str, StoredCredential]:
                raise OSError("credential file is unreadable")

        report = await CredentialRefreshService(Unreadable()).sweep()
        assert len(report) == 0


class TestFailureHandling:
    async def test_a_failure_is_an_outcome_not_an_exception(self) -> None:
        store, clock, _ = build({"a": credential(minutes_left=5)}, fails=True)
        report = await CredentialRefreshService(store, clock=clock).sweep()

        assert [o.name for o in report.failed] == ["a"]

    async def test_one_failure_does_not_stop_the_others(self) -> None:
        clock = ManualClock(NOON)
        store = MemoryCredentialStore(clock=clock)

        class Selective:
            calls = 0

            async def refresh(self, name, stored):
                Selective.calls += 1
                if name == "bad":
                    raise AuthExpired("nope", name=name)
                return StoredCredential(
                    token=Secret("fresh"),
                    expires_at=clock.now() + timedelta(hours=2),
                    issued_at=clock.now(),
                )

        store._refresher = Selective()
        store._data["bad"] = credential(minutes_left=5)
        store._data["good"] = credential(minutes_left=5)

        report = await CredentialRefreshService(store, clock=clock).sweep()

        assert {o.name: o.status for o in report.outcomes} == {
            "bad": "failed",
            "good": "refreshed",
        }

    async def test_a_failure_leaves_the_stored_credential_intact(self) -> None:
        store, clock, _ = build({"a": credential("keep-me", minutes_left=5)}, fails=True)
        await CredentialRefreshService(store, clock=clock).sweep()

        stored = await store.peek("a")
        assert stored.token.reveal() == "keep-me"
        assert stored.refresh_token is not None

    async def test_a_failed_credential_is_not_retried_immediately(self) -> None:
        """Without backoff a permanently dead refresh token is retried every
        sweep forever — a self-inflicted flood that gets the *working*
        credentials rate-limited too."""
        store, clock, refresher = build({"a": credential(minutes_left=5)}, fails=True)
        service = CredentialRefreshService(store, clock=clock)

        await service.sweep()
        assert refresher.calls == 1

        report = await service.sweep()
        assert refresher.calls == 1, "retried inside the backoff window"
        assert report.outcomes[0].status == "skipped"

    async def test_the_retry_happens_once_the_backoff_elapses(self) -> None:
        store, clock, refresher = build({"a": credential(minutes_left=5)}, fails=True)
        service = CredentialRefreshService(store, clock=clock)

        await service.sweep()
        clock.advance(seconds=INITIAL_BACKOFF + 1)
        await service.sweep()

        assert refresher.calls == 2

    async def test_the_backoff_grows_and_is_capped(self) -> None:
        store, clock, _ = build({"a": credential(minutes_left=5)}, fails=True)
        service = CredentialRefreshService(store, clock=clock)

        delay = INITIAL_BACKOFF
        for _ in range(12):
            await service.sweep()
            clock.advance(seconds=delay + 1)
            delay = min(delay * 2, MAX_BACKOFF)

        assert service._backoff["a"].delay == MAX_BACKOFF

    async def test_force_ignores_the_backoff(self) -> None:
        """An operator who just fixed the credential should not wait an hour."""
        store, clock, refresher = build({"a": credential(minutes_left=5)}, fails=True)
        service = CredentialRefreshService(store, clock=clock)

        await service.sweep()
        await service.sweep(force=True)

        assert refresher.calls == 2

    async def test_a_success_clears_the_backoff(self) -> None:
        store, clock, refresher = build({"a": credential(minutes_left=5)}, fails=True)
        service = CredentialRefreshService(store, clock=clock)
        await service.sweep()

        refresher.fails = False
        clock.advance(seconds=INITIAL_BACKOFF + 1)
        await service.sweep()

        assert "a" not in service._backoff


class TestSecrets:
    async def test_no_token_value_reaches_the_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store, clock, _ = build({"a": credential("SUPER-SECRET-TOKEN", minutes_left=5)})
        with caplog.at_level(logging.DEBUG, logger="workflow.credentials"):
            await CredentialRefreshService(store, clock=clock).sweep()

        assert "SUPER-SECRET-TOKEN" not in caplog.text
        assert "refresh-me" not in caplog.text
        assert "fresh-1" not in caplog.text
        assert "'a'" in caplog.text, "the credential name should be logged"

    async def test_a_failure_logs_the_name_and_not_the_secret(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store, clock, _ = build(
            {"a": credential("SUPER-SECRET-TOKEN", minutes_left=5)}, fails=True
        )
        with caplog.at_level(logging.DEBUG, logger="workflow.credentials"):
            await CredentialRefreshService(store, clock=clock).sweep()

        assert "SUPER-SECRET-TOKEN" not in caplog.text
        assert "a" in caplog.text

    async def test_the_report_carries_no_token(self) -> None:
        store, clock, _ = build({"a": credential("SUPER-SECRET-TOKEN", minutes_left=5)})
        report = await CredentialRefreshService(store, clock=clock).sweep()

        assert "SUPER-SECRET-TOKEN" not in repr(report)


# ---------------------------------------------------------------------------
# The background loop
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_sweeps_immediately(self) -> None:
        """The "on restart" half: a process back up after being down longer
        than a token's lifetime renews before serving its first request."""
        store, clock, refresher = build({"a": credential(minutes_left=5)})
        service = CredentialRefreshService(store, clock=clock, interval=3600)

        await service.start()
        try:
            assert refresher.calls == 1
        finally:
            await service.stop()

    async def test_it_keeps_sweeping(self) -> None:
        """Counted as sweeps, not refreshes: the first sweep renews the
        credential, after which there is correctly nothing left to do."""
        store, clock, _ = build({"a": credential(minutes_left=5)})
        service = CredentialRefreshService(store, clock=clock, interval=60)
        sweeps = 0
        original = service.sweep

        async def counting(*args, **kwargs):
            nonlocal sweeps
            sweeps += 1
            return await original(*args, **kwargs)

        service.sweep = counting  # type: ignore[method-assign]

        await service.start()
        try:
            assert sweeps == 1, "start() sweeps immediately"
            for _ in range(20):
                await asyncio.sleep(0)
            assert sweeps > 1, "the loop never ran a second sweep"
        finally:
            await service.stop()

    async def test_stop_is_safe_before_start_and_twice(self) -> None:
        store, clock, _ = build()
        service = CredentialRefreshService(store, clock=clock)

        await service.stop()
        await service.start()
        await service.stop()
        await service.stop()

    async def test_starting_twice_runs_one_loop(self) -> None:
        store, clock, _ = build()
        service = CredentialRefreshService(store, clock=clock)

        await service.start()
        first = service._task
        await service.start()
        try:
            assert service._task is first
        finally:
            await service.stop()

    async def test_a_runtime_shutdown_stops_it(self) -> None:
        """Registered through the same supervise() seam TriggerDispatcher uses,
        so a host does not have to know which services it wired up."""
        from loom.runtime.engine import Runtime
        from loom.stores.memory import MemoryStore

        store, clock, _ = build({"a": credential(minutes_left=5)})
        runtime = Runtime(store=MemoryStore())
        service = CredentialRefreshService(store, clock=clock, runtime=runtime)

        await service.start()
        assert service._task is not None

        await runtime.shutdown(drain=0)
        assert service._task is None


class TestServiceFor:
    async def test_a_runtime_with_no_credentials_gets_no_service(self) -> None:
        """A bare Runtime must start no background task, so every host and test
        that predates this behaves exactly as before."""
        from loom.runtime.engine import Runtime
        from loom.stores.memory import MemoryStore

        assert service_for(Runtime(store=MemoryStore())) is None

    async def test_a_runtime_with_credentials_gets_one(self) -> None:
        from loom.runtime.engine import Runtime
        from loom.stores.memory import MemoryStore

        store, _, _ = build()
        runtime = Runtime(store=MemoryStore(), credentials=store)

        assert isinstance(service_for(runtime), CredentialRefreshService)

    async def test_it_accepts_a_facade(self) -> None:
        from loom.facade import LocalFacade
        from loom.runtime.engine import Runtime
        from loom.stores.memory import MemoryStore

        store, _, _ = build()
        runtime = Runtime(store=MemoryStore(), credentials=store)

        assert service_for(LocalFacade(runtime)) is not None

    def test_it_adopts_the_stores_policy(self) -> None:
        """One threshold for "due", not two — a sweep and an on-use renewal
        disagreeing is how a credential gets refreshed twice, or never."""
        from loom.runtime.engine import Runtime
        from loom.stores.memory import MemoryStore

        policy = RefreshPolicy(skew=timedelta(minutes=42))
        store = MemoryCredentialStore(refresh_policy=policy)
        runtime = Runtime(store=MemoryStore(), credentials=store)

        assert service_for(runtime)._policy is policy


# ---------------------------------------------------------------------------
# loom refresh
# ---------------------------------------------------------------------------


def _args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {"name": [], "force": False, "json": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestRefreshCommand:
    @pytest.fixture(autouse=True)
    def _store(self, monkeypatch: pytest.MonkeyPatch):
        store, clock, refresher = build(
            {
                "gmail": credential("gmail-old", minutes_left=5),
                "jira": credential("jira-current", minutes_left=90),
            }
        )
        monkeypatch.setattr(auth_commands, "_store", lambda *a, **k: store)
        self.store, self.clock, self.refresher = store, clock, refresher
        return store

    def test_it_renews_what_is_due(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert auth_commands.cmd_refresh(_args(json=True)) == Exit.OK

        import json

        payload = json.loads(capsys.readouterr().out)
        assert payload["refreshed"] == ["gmail"]
        assert payload["failed"] == []

    def test_it_can_name_one_credential(self) -> None:
        assert auth_commands.cmd_refresh(_args(name=["jira"])) == Exit.OK
        assert self.refresher.calls == 0, "jira was not due"

    def test_an_unknown_name_is_a_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit 2, not 1: nothing failed to refresh, the request was wrong."""
        assert auth_commands.cmd_refresh(_args(name=["slack"])) == Exit.USAGE
        assert "not connected" in capsys.readouterr().err

    def test_force_renews_a_credential_that_is_not_due(self) -> None:
        assert auth_commands.cmd_refresh(_args(name=["jira"], force=True)) == Exit.OK
        assert self.refresher.calls == 1

    def test_a_failure_is_exit_one_so_a_scheduled_run_notices(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A cron entry that always exits 0 reports a dead refresh token as
        success, which is the whole reason to run it on a schedule."""
        self.refresher.fails = True

        assert auth_commands.cmd_refresh(_args(json=True)) == Exit.FAILED

        import json

        payload = json.loads(capsys.readouterr().out)
        assert [f["name"] for f in payload["failed"]] == ["gmail"]

    def test_nothing_connected_is_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            auth_commands, "_store", lambda *a, **k: MemoryCredentialStore()
        )
        assert auth_commands.cmd_refresh(_args(json=True)) == Exit.OK

    def test_no_token_is_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        auth_commands.cmd_refresh(_args(json=True))
        out = capsys.readouterr().out
        assert "gmail-old" not in out
        assert "refresh-me" not in out
        assert "fresh-1" not in out


class TestWhoamiShowsTheWindow:
    def test_a_credential_inside_the_window_reads_as_due(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Three states, not two. Repeated 'due' tells an operator renewal is
        failing, long before it becomes 'expired' and something breaks."""
        import json

        store, _, _ = build(
            {
                "soon": credential(minutes_left=5),
                "later": credential(minutes_left=90),
                "gone": credential(minutes_left=-5),
            }
        )
        monkeypatch.setattr(auth_commands, "_store", lambda *a, **k: store)
        monkeypatch.setattr(
            "loom.runtime.clock.SystemClock.now", lambda self: NOON
        )

        auth_commands.cmd_whoami(argparse.Namespace(json=True))

        payload = json.loads(capsys.readouterr().out)
        by_name = {entry["name"]: entry for entry in payload["connected"]}
        assert by_name["soon"]["refresh_due"] and not by_name["soon"]["expired"]
        assert not by_name["later"]["refresh_due"]
        assert by_name["gone"]["expired"]

    def test_it_reports_whether_renewal_is_even_possible(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        store, _, _ = build({"manual": credential(refreshable=False)})
        monkeypatch.setattr(auth_commands, "_store", lambda *a, **k: store)

        auth_commands.cmd_whoami(argparse.Namespace(json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["connected"][0]["can_refresh"] is False


# ---------------------------------------------------------------------------
# The CLI's store reaches a workflow
# ---------------------------------------------------------------------------


class TestCliCredentialsReachTheRuntime:
    """`loom connect <name>` must produce something a workflow can read.

    Otherwise the CLI authenticates a credential it cannot use, which is
    indistinguishable from the connect having silently failed.
    """

    def test_resolve_attaches_the_credential_store(self, tmp_path) -> None:
        from loom.cli.targets import resolve

        target = resolve(None, modules=[])
        assert target.backend.runtime.credentials is not None

    def test_the_store_is_locked_against_the_runtimes_store(self) -> None:
        """Two processes sharing this credential file must not refresh the same
        credential at once — on a server that rotates refresh tokens, each
        rotation invalidates the other's."""
        from loom.runtime.engine import Runtime
        from loom.stores.memory import MemoryStore

        runtime = Runtime(store=MemoryStore())
        store = auth_commands.credential_store_for(runtime)

        assert store._refresher._lock is runtime.store

    async def test_a_per_run_credential_still_wins_over_the_cli_store(self) -> None:
        """The security property behind wiring the CLI's store in at all.

        It is attached as *ambient* fallback, so a caller that supplied its own
        credentials for a run keeps that identity. If the ambient store could
        satisfy a name the caller had supplied, the CLI's connected account
        would silently stand in for whoever the run was meant to act as.
        """
        from loom.runtime.engine import Runtime
        from loom.stores.memory import MemoryStore

        ambient, _, _ = build()
        await ambient.put("jira", StoredCredential(token=Secret("cli-account")))

        per_run = MemoryCredentialStore()
        await per_run.put("jira", StoredCredential(token=Secret("run-account")))

        runtime = Runtime(store=MemoryStore(), credentials=ambient)
        record = type(
            "Record", (), {"run_id": "r1", "metadata": {}}
        )()
        runtime._run_credentials["r1"] = per_run

        layered = await runtime._credentials_for(record)
        assert (await layered.get("jira")).reveal() == "run-account"

    async def test_the_cli_store_answers_names_the_run_did_not_supply(self) -> None:
        """Ambient still means reachable — that is the point of wiring it in."""
        from loom.runtime.engine import Runtime
        from loom.stores.memory import MemoryStore

        ambient, _, _ = build()
        await ambient.put("gmail", StoredCredential(token=Secret("cli-account")))

        runtime = Runtime(store=MemoryStore(), credentials=ambient)
        record = type("Record", (), {"run_id": "r2", "metadata": {}})()

        layered = await runtime._credentials_for(record)
        assert (await layered.get("gmail")).reveal() == "cli-account"
