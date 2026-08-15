"""One conformance suite, run against every ``CredentialStore`` implementation.

Mirrors ``tests/test_store_conformance.py``'s reasoning: divergence between
implementations is the bug class this catches — a behaviour that happens to
hold for ``MemoryCredentialStore`` because it is a dict, and quietly stops
holding once credentials go through encryption and a file.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from keyring.backend import KeyringBackend

from workflow_builder.connectors.credentials import (
    CredentialStore,
    EncryptedFileCredentialStore,
    KeyringCredentialStore,
    MemoryCredentialStore,
    Refresher,
    StoredCredential,
)
from workflow_builder.connectors.encryption import EnvKeyProvider
from workflow_builder.core.exceptions import AuthExpired, CredentialNotFound
from workflow_builder.core.secret import Secret
from workflow_builder.runtime.clock import ManualClock


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
