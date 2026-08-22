"""One conformance suite, run against every ``CredentialStore`` implementation.

Mirrors ``tests/test_store_conformance.py``'s reasoning: divergence between
implementations is the bug class this catches — a behaviour that happens to
hold for ``MemoryCredentialStore`` because it is a dict, and quietly stops
holding once credentials go through encryption and a file.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from keyring.backend import KeyringBackend

from loom.connectors.credentials import (
    CredentialStore,
    EncryptedFileCredentialStore,
    KeyringCredentialStore,
    MemoryCredentialStore,
    Refresher,
    StoredCredential,
)
from loom.connectors.encryption import EnvKeyProvider
from loom.core.exceptions import AuthExpired, CredentialNotFound
from loom.core.secret import Secret
from loom.runtime.clock import ManualClock


class _FakeKeyring(KeyringBackend):
    """An in-memory keyring backend, so tests never touch a real OS keychain."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._data.pop((service, username), None)


@pytest.fixture(params=["memory", "encrypted_file", "keyring"])
def store(request: pytest.FixtureRequest, tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Each test in this module runs once per ``CredentialStore`` implementation."""
    if request.param == "memory":
        yield MemoryCredentialStore()
        return

    if request.param == "encrypted_file":
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", _generate_key())
        yield EncryptedFileCredentialStore(
            tmp_path / "creds.enc", key_provider=EnvKeyProvider()
        )
        return

    import keyring

    # Without this the store takes `conftest.py`'s pinned `LOOM_CREDENTIAL_KEY`
    # — which is rung 1 of the key order and now reaches this store too — and
    # the "keyring" parameter would exercise the env path under a keyring
    # label. A parameter that silently tests its neighbour is worse than one
    # that fails.
    monkeypatch.delenv("LOOM_CREDENTIAL_KEY", raising=False)

    original = keyring.get_keyring()
    keyring.set_keyring(_FakeKeyring())
    try:
        yield KeyringCredentialStore(tmp_path / "creds.enc")
    finally:
        keyring.set_keyring(original)


def _generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def _cred(token: str = "tok", **overrides: object) -> StoredCredential:
    base: dict[str, object] = {"token": Secret(token)}
    return StoredCredential(**{**base, **overrides})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


def test_every_implementation_satisfies_the_protocol(store) -> None:
    assert isinstance(store, CredentialStore)


def test_a_refresher_only_needs_one_method() -> None:
    class Minimal:
        async def refresh(self, name: str, stored: StoredCredential) -> StoredCredential:
            return stored

    assert isinstance(Minimal(), Refresher)


# ---------------------------------------------------------------------------
# put / get / forget / names
# ---------------------------------------------------------------------------


class TestRoundTrip:
    async def test_put_then_get_returns_the_token(self, store) -> None:
        await store.put("jira", _cred("tok-1"))
        got = await store.get("jira")
        assert isinstance(got, Secret)
        assert got.reveal() == "tok-1"

    async def test_get_missing_raises_credential_not_found(self, store) -> None:
        with pytest.raises(CredentialNotFound):
            await store.get("nope")

    async def test_put_overwrites_an_existing_entry(self, store) -> None:
        await store.put("jira", _cred("old"))
        await store.put("jira", _cred("new"))
        assert (await store.get("jira")).reveal() == "new"

    async def test_forget_removes_it(self, store) -> None:
        await store.put("jira", _cred("tok"))
        await store.forget("jira")
        with pytest.raises(CredentialNotFound):
            await store.get("jira")

    async def test_forgetting_an_absent_name_is_a_no_op(self, store) -> None:
        await store.forget("never-existed")  # must not raise

    async def test_names_lists_everything_sorted(self, store) -> None:
        await store.put("zeta", _cred())
        await store.put("alpha", _cred())
        assert await store.names() == ["alpha", "zeta"]

    async def test_names_is_empty_for_a_fresh_store(self, store) -> None:
        assert await store.names() == []

    async def test_names_drops_a_forgotten_entry(self, store) -> None:
        await store.put("jira", _cred())
        await store.forget("jira")
        assert await store.names() == []

    async def test_metadata_and_scopes_round_trip(self, store) -> None:
        cred = _cred(
            "tok", scopes=frozenset({"issues:read", "issues:write"}),
            metadata={"account": "acme"}, token_type="bearer",
        )
        await store.put("jira", cred)
        # Round-tripping metadata/scopes is only observable by re-reading the
        # full record, which the port does not expose — so exercise it through
        # a refresher that receives the stored value back.
        seen: list[StoredCredential] = []

        class Capture:
            async def refresh(self, name, stored):
                seen.append(stored)
                return stored

        clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        expired = _cred(
            "tok", scopes=frozenset({"a"}), metadata={"k": "v"},
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        store._refresher = Capture()  # type: ignore[attr-defined]
        store.clock = clock  # type: ignore[attr-defined]
        await store.put("scoped", expired)
        await store.get("scoped")
        assert seen[0].scopes == frozenset({"a"})
        assert seen[0].metadata == {"k": "v"}


# ---------------------------------------------------------------------------
# Expiry and refresh
# ---------------------------------------------------------------------------


class _CountingRefresher:
    """Counts calls and returns a fresh, non-expiring credential."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls = 0
        self._delay = delay

    async def refresh(self, name: str, stored: StoredCredential) -> StoredCredential:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return StoredCredential(token=Secret(f"refreshed-{self.calls}"))


class TestExpiry:
    async def test_a_credential_with_no_expiry_never_expires(self, store) -> None:
        await store.put("jira", _cred("tok"))
        assert (await store.get("jira")).reveal() == "tok"

    async def test_expired_with_no_refresher_raises_auth_expired(self, store) -> None:
        clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        store.clock = clock  # type: ignore[attr-defined]
        await store.put(
            "jira", _cred("stale", expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        with pytest.raises(AuthExpired, match="jira"):
            await store.get("jira")

    async def test_expired_with_a_refresher_gets_a_fresh_token(self, store) -> None:
        clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        refresher = _CountingRefresher()
        store.clock = clock  # type: ignore[attr-defined]
        store._refresher = refresher  # type: ignore[attr-defined]
        await store.put(
            "jira", _cred("stale", expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )

        got = await store.get("jira")

        assert got.reveal() == "refreshed-1"
        assert refresher.calls == 1

    async def test_a_refreshed_credential_is_persisted(self, store) -> None:
        clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        store.clock = clock  # type: ignore[attr-defined]
        store._refresher = _CountingRefresher()  # type: ignore[attr-defined]
        await store.put(
            "jira", _cred("stale", expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        await store.get("jira")

        # A second read, with the refresher swapped for one that must not be
        # called, proves the refreshed (non-expiring) value was written back.
        class MustNotBeCalled:
            async def refresh(self, name, stored):
                raise AssertionError("refresher called again; refresh was not persisted")

        store._refresher = MustNotBeCalled()  # type: ignore[attr-defined]
        assert (await store.get("jira")).reveal() == "refreshed-1"

    async def test_concurrent_gets_on_an_expired_credential_refresh_once(
        self, store
    ) -> None:
        """Single-flight within one process: N callers, one token request."""
        clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        refresher = _CountingRefresher(delay=0.01)
        store.clock = clock  # type: ignore[attr-defined]
        store._refresher = refresher  # type: ignore[attr-defined]
        await store.put(
            "jira", _cred("stale", expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )

        results = await asyncio.gather(*[store.get("jira") for _ in range(10)])

        assert refresher.calls == 1
        assert {r.reveal() for r in results} == {"refreshed-1"}


# ---------------------------------------------------------------------------
# LayeredCredentialStore
# ---------------------------------------------------------------------------


class TestLayeredCredentialStore:
    """Layered is a protocol implementer that delegates ``get()``, so it is
    not parametrized with the others — those tests poke ``_refresher`` and
    ``clock`` on the store itself."""

    async def test_it_satisfies_the_protocol(self) -> None:
        from loom.connectors.credentials import LayeredCredentialStore

        layered = LayeredCredentialStore(MemoryCredentialStore())
        assert isinstance(layered, CredentialStore)

    async def test_get_delegates_to_the_layer_so_refresh_still_runs(self) -> None:
        from datetime import UTC, datetime

        from loom.connectors.credentials import LayeredCredentialStore

        inner = MemoryCredentialStore()
        clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        refresher = _CountingRefresher()
        inner.clock = clock
        inner._refresher = refresher
        await inner.put(
            "jira", _cred("stale", expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        layered = LayeredCredentialStore(inner)

        got = await layered.get("jira")

        assert got.reveal() == "refreshed-1"
        assert refresher.calls == 1

    async def test_credential_not_found_falls_through(self) -> None:
        from loom.connectors.credentials import LayeredCredentialStore

        first = MemoryCredentialStore()
        second = MemoryCredentialStore()
        await second.put("jira", _cred("from-second"))
        layered = LayeredCredentialStore(first, second)
        assert (await layered.get("jira")).reveal() == "from-second"

    async def test_auth_expired_does_not_fall_through(self) -> None:
        from datetime import UTC, datetime

        from loom.connectors.credentials import LayeredCredentialStore

        first = MemoryCredentialStore()
        first.clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
        await first.put(
            "jira", _cred("stale", expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        second = MemoryCredentialStore()
        await second.put("jira", _cred("ambient"))
        layered = LayeredCredentialStore(first, second)

        with pytest.raises(AuthExpired, match="jira"):
            await layered.get("jira")

    async def test_required_name_raises_auth_expired_before_ambient(self) -> None:
        from loom.connectors.credentials import LayeredCredentialStore

        ambient = MemoryCredentialStore()
        await ambient.put("jira", _cred("ambient"))
        layered = LayeredCredentialStore(
            required=frozenset({"jira"}), ambient=(ambient,)
        )
        with pytest.raises(AuthExpired, match="jira"):
            await layered.get("jira")

    async def test_unrequired_name_uses_ambient(self) -> None:
        from loom.connectors.credentials import LayeredCredentialStore

        ambient = MemoryCredentialStore()
        await ambient.put("google", _cred("ambient-google"))
        layered = LayeredCredentialStore(
            required=frozenset({"jira"}), ambient=(ambient,)
        )
        assert (await layered.get("google")).reveal() == "ambient-google"

    async def test_peek_finds_the_first_layer_with_a_record(self) -> None:
        from loom.connectors.credentials import LayeredCredentialStore

        first = MemoryCredentialStore()
        second = MemoryCredentialStore()
        await second.put("jira", _cred("hidden"))
        layered = LayeredCredentialStore(first, second)
        found = await layered.peek("jira")
        assert found is not None
        assert found.token.reveal() == "hidden"

    def test_repr_reports_layer_count_only(self) -> None:
        from loom.connectors.credentials import LayeredCredentialStore

        layered = LayeredCredentialStore(MemoryCredentialStore(), MemoryCredentialStore())
        assert repr(layered) == "<LayeredCredentialStore layers=2>"


# ---------------------------------------------------------------------------
# Early refresh — the window before expiry
# ---------------------------------------------------------------------------


NOON = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def _timed(
    token: str = "tok", *, minutes_left: float, lifetime_minutes: float = 60.0
) -> StoredCredential:
    """A credential expiring *minutes_left* from NOON, with a known lifetime."""
    expires_at = NOON + timedelta(minutes=minutes_left)
    return StoredCredential(
        token=Secret(token),
        refresh_token=Secret("refresh-me"),
        expires_at=expires_at,
        issued_at=expires_at - timedelta(minutes=lifetime_minutes),
    )


class TestEarlyRefresh:
    """Renewing at the moment of expiry is renewing too late.

    A token with two seconds left passes an ``is_expired`` check and then 401s
    on the request it was fetched for. The window also absorbs clock drift
    between this machine and the authorization server, which is otherwise
    indistinguishable from a token that expired early.
    """

    def _armed(self, store) -> _CountingRefresher:
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]
        refresher = _CountingRefresher()
        store._refresher = refresher  # type: ignore[attr-defined]
        return refresher

    async def test_a_token_inside_the_window_is_renewed_before_it_expires(
        self, store
    ) -> None:
        refresher = self._armed(store)
        await store.put("jira", _timed("old", minutes_left=5))

        assert (await store.get("jira")).reveal() == "refreshed-1"
        assert refresher.calls == 1

    async def test_a_token_outside_the_window_is_left_alone(self, store) -> None:
        refresher = self._armed(store)
        await store.put("jira", _timed("current", minutes_left=30))

        assert (await store.get("jira")).reveal() == "current"
        assert refresher.calls == 0

    async def test_the_boundary_is_inclusive(self, store) -> None:
        """Exactly at the window: renew. Off by a second the other way: do not."""
        refresher = self._armed(store)
        await store.put("jira", _timed("edge", minutes_left=10))
        assert (await store.get("jira")).reveal() == "refreshed-1"

        refresher.calls = 0
        await store.put("jira", _timed("just-outside", minutes_left=10.02))
        assert (await store.get("jira")).reveal() == "just-outside"
        assert refresher.calls == 0

    async def test_a_short_lived_token_does_not_refresh_on_every_call(
        self, store
    ) -> None:
        """The failure a flat window would cause.

        A provider issuing five-minute tokens under a ten-minute window would
        report every token as due the moment it was minted: every call
        refreshes, the authorization server sees a storm, and on a server that
        rotates refresh tokens each rotation invalidates the last.
        """
        refresher = self._armed(store)
        # Four of its five minutes remain — nowhere near due, despite the whole
        # lifetime being shorter than the configured window.
        await store.put("jira", _timed("fresh", minutes_left=4, lifetime_minutes=5))

        assert (await store.get("jira")).reveal() == "fresh"
        assert refresher.calls == 0

    async def test_a_short_lived_token_still_refreshes_past_its_half_life(
        self, store
    ) -> None:
        """Clamped, not disabled."""
        refresher = self._armed(store)
        await store.put("jira", _timed("aging", minutes_left=2, lifetime_minutes=5))

        assert (await store.get("jira")).reveal() == "refreshed-1"
        assert refresher.calls == 1

    async def test_a_credential_with_no_lifetime_recorded_uses_the_full_window(
        self, store
    ) -> None:
        """Written before ``issued_at`` existed. Nothing to clamp against, and
        inventing a lifetime would manufacture one nobody measured."""
        refresher = self._armed(store)
        await store.put(
            "jira",
            StoredCredential(
                token=Secret("legacy"),
                refresh_token=Secret("r"),
                expires_at=NOON + timedelta(minutes=5),
            ),
        )

        assert (await store.get("jira")).reveal() == "refreshed-1"
        assert refresher.calls == 1


class TestEarlyRefreshFailsSoft:
    """Inside the window the token still works, so a failed renewal must not
    take it away. At expiry there is nothing to fall back to and the caller
    has to hear about it."""

    class _Broken:
        async def refresh(self, name, stored):
            raise AuthExpired("the authorization server is having a bad day")

    async def test_a_failed_early_refresh_returns_the_still_valid_token(
        self, store
    ) -> None:
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]
        store._refresher = self._Broken()  # type: ignore[attr-defined]
        await store.put("jira", _timed("still-good", minutes_left=5))

        assert (await store.get("jira")).reveal() == "still-good"

    async def test_a_failed_refresh_after_expiry_raises(self, store) -> None:
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]
        store._refresher = self._Broken()  # type: ignore[attr-defined]
        await store.put("jira", _timed("dead", minutes_left=-1))

        with pytest.raises(AuthExpired):
            await store.get("jira")

    async def test_no_refresher_inside_the_window_is_not_an_error(
        self, store
    ) -> None:
        """The regression this guards: making `due` behave like `expired`
        would break every store with no refresher ten minutes early."""
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]
        await store.put("jira", _timed("usable", minutes_left=5))

        assert (await store.get("jira")).reveal() == "usable"

    async def test_no_refresher_after_expiry_still_raises(self, store) -> None:
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]
        await store.put("jira", _timed("gone", minutes_left=-1))

        with pytest.raises(AuthExpired, match="jira"):
            await store.get("jira")

    async def test_a_failed_early_refresh_does_not_damage_the_stored_record(
        self, store
    ) -> None:
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]
        store._refresher = self._Broken()  # type: ignore[attr-defined]
        await store.put("jira", _timed("intact", minutes_left=5))
        await store.get("jira")

        stored = await store.peek("jira")
        assert stored.token.reveal() == "intact"
        assert stored.refresh_token is not None


class TestExplicitRefresh:
    """``store.refresh(name)`` renews now, whether or not it is due — what
    ``loom refresh`` runs. Distinct from ``get()``, and sharing its code."""

    async def test_it_renews_a_credential_that_is_not_due(self, store) -> None:
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]
        refresher = _CountingRefresher()
        store._refresher = refresher  # type: ignore[attr-defined]
        await store.put("jira", _timed("current", minutes_left=45))

        assert (await store.refresh("jira")).reveal() == "refreshed-1"
        assert refresher.calls == 1

    async def test_it_persists_like_get_does(self, store) -> None:
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]
        store._refresher = _CountingRefresher()  # type: ignore[attr-defined]
        await store.put("jira", _timed("current", minutes_left=45))
        await store.refresh("jira")

        assert (await store.peek("jira")).token.reveal() == "refreshed-1"

    async def test_it_raises_rather_than_failing_soft(self, store) -> None:
        """The caller asked for this to happen; reporting success because the
        old token still works would answer a different question."""
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]
        store._refresher = TestEarlyRefreshFailsSoft._Broken()  # type: ignore[attr-defined]
        await store.put("jira", _timed("current", minutes_left=45))

        with pytest.raises(AuthExpired):
            await store.refresh("jira")

    async def test_an_unknown_name_is_not_found(self, store) -> None:
        with pytest.raises(CredentialNotFound):
            await store.refresh("nope")


class TestPeekAll:
    """What the background sweep reads. The encrypted stores override it to
    decrypt once rather than once per credential."""

    async def test_it_returns_every_record(self, store) -> None:
        await store.put("a", _cred("tok-a"))
        await store.put("b", _cred("tok-b"))

        found = await store.peek_all()

        assert set(found) == {"a", "b"}
        assert found["a"].token.reveal() == "tok-a"

    async def test_it_neither_expires_nor_refreshes(self, store) -> None:
        store.clock = ManualClock(NOON)  # type: ignore[attr-defined]

        class MustNotBeCalled:
            async def refresh(self, name, stored):
                raise AssertionError("peek_all must not trigger a refresh")

        store._refresher = MustNotBeCalled()  # type: ignore[attr-defined]
        await store.put("jira", _timed("stale", minutes_left=-5))

        found = await store.peek_all()
        assert found["jira"].token.reveal() == "stale"

    async def test_an_empty_store_is_an_empty_mapping(self, store) -> None:
        assert await store.peek_all() == {}
