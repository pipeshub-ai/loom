"""Encryption, key sourcing, and the file-storage corner cases from the plan.

Each test here pins down one specific failure mode called out in the design
doc's "Corner cases and their handling" section — a keyring that is locked,
a truncated file, a rotated key — rather than only the happy path the
conformance suite already covers.
"""

from __future__ import annotations

import stat

import pytest
from cryptography.fernet import Fernet
from keyring.backend import KeyringBackend
from keyring.errors import KeyringLocked

from loom.connectors.credentials import (
    EncryptedFileCredentialStore,
    KeyringCredentialStore,
    StoredCredential,
)
from loom.connectors.encryption import (
    DecryptionError,
    Envelope,
    EnvKeyProvider,
    GeneratedFileKeyProvider,
    KeyringKeyProvider,
    atomic_write_bytes,
    default_key_provider,
)
from loom.core.secret import Secret


def _cred(token: str = "tok") -> StoredCredential:
    return StoredCredential(token=Secret(token))


# ---------------------------------------------------------------------------
# EnvKeyProvider
# ---------------------------------------------------------------------------


class TestEnvKeyProvider:
    def test_reads_the_configured_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = Fernet.generate_key().decode("ascii")
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", key)
        assert EnvKeyProvider().keys() == [key.encode("ascii")]

    def test_missing_variable_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOOM_CREDENTIAL_KEY", raising=False)
        with pytest.raises(LookupError):
            EnvKeyProvider().keys()


# ---------------------------------------------------------------------------
# GeneratedFileKeyProvider
# ---------------------------------------------------------------------------


class TestGeneratedFileKeyProvider:
    def test_generates_once_and_reuses_it(self, tmp_path) -> None:
        path = tmp_path / "sub" / "credentials.key"
        provider = GeneratedFileKeyProvider(path, warn=lambda _msg: None)

        first = provider.keys()
        second = GeneratedFileKeyProvider(path, warn=lambda _msg: None).keys()

        assert first == second
        assert path.exists()

    def test_warns_loudly_on_generation(self, tmp_path) -> None:
        warned: list[str] = []
        GeneratedFileKeyProvider(tmp_path / "credentials.key", warn=warned.append).keys()

        assert len(warned) == 1
        assert "machine-local" in warned[0]
        assert str(tmp_path) in warned[0]

    def test_does_not_warn_on_reuse(self, tmp_path) -> None:
        path = tmp_path / "credentials.key"
        GeneratedFileKeyProvider(path, warn=lambda _msg: None).keys()

        warned: list[str] = []
        GeneratedFileKeyProvider(path, warn=warned.append).keys()

        assert warned == []

    def test_the_key_file_is_0600(self, tmp_path) -> None:
        path = tmp_path / "credentials.key"
        GeneratedFileKeyProvider(path, warn=lambda _msg: None).keys()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_default_warn_uses_the_warnings_module(self, tmp_path) -> None:
        with pytest.warns(UserWarning, match="machine-local"):
            GeneratedFileKeyProvider(tmp_path / "credentials.key").keys()

    def test_an_empty_key_file_raises_rather_than_silently_regenerating(
        self, tmp_path
    ) -> None:
        path = tmp_path / "credentials.key"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        with pytest.raises(DecryptionError):
            GeneratedFileKeyProvider(path, warn=lambda _msg: None).keys()


# ---------------------------------------------------------------------------
# KeyringKeyProvider
# ---------------------------------------------------------------------------


class _FakeKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._data.pop((service, username), None)


class _BrokenKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def get_password(self, service: str, username: str) -> str | None:
        raise KeyringLocked("the keychain is locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise KeyringLocked("the keychain is locked")

    def delete_password(self, service: str, username: str) -> None:
        raise KeyringLocked("the keychain is locked")


@pytest.fixture
def fake_keyring():
    import keyring

    original = keyring.get_keyring()
    keyring.set_keyring(_FakeKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(original)


@pytest.fixture
def broken_keyring():
    import keyring

    original = keyring.get_keyring()
    keyring.set_keyring(_BrokenKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(original)


class TestKeyringKeyProvider:
    def test_generates_and_reuses_a_key_via_the_keyring(self, fake_keyring) -> None:
        provider = KeyringKeyProvider()
        first = provider.keys()
        second = KeyringKeyProvider().keys()
        assert first == second

    def test_never_asks_for_the_payload_only_the_key(self, fake_keyring) -> None:
        """The whole point of the split: what lands in the keyring is one
        Fernet key (44 bytes, base64), never a token of arbitrary size."""
        import keyring

        KeyringKeyProvider().keys()
        stored = keyring.get_password("loom-credential-store", "master-key")
        assert stored is not None
        assert len(stored) < 100  # nowhere near Windows Credential Manager's ~2.5KB cap

    def test_a_locked_keyring_falls_back(self, broken_keyring) -> None:
        provider = KeyringKeyProvider(fallback=EnvKeyProviderStub(b"fallback-key-000000000000000="))
        assert provider.keys() == [b"fallback-key-000000000000000="]

    def test_a_locked_keyring_with_no_fallback_raises(self, broken_keyring) -> None:
        with pytest.raises(LookupError):
            KeyringKeyProvider().keys()


class EnvKeyProviderStub:
    """A trivial fixed-key provider, for asserting exactly which key a
    fallback returned without involving the filesystem or environment."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def keys(self) -> list[bytes]:
        return [self._key]


# ---------------------------------------------------------------------------
# default_key_provider — the priority order
# ---------------------------------------------------------------------------


class TestDefaultKeyProvider:
    def test_prefers_the_env_var_when_set(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = Fernet.generate_key().decode("ascii")
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", key)
        provider = default_key_provider(app_dir=tmp_path)
        assert isinstance(provider, EnvKeyProvider)
        assert provider.keys() == [key.encode("ascii")]

    def test_falls_to_keyring_when_env_var_is_unset(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, fake_keyring
    ) -> None:
        monkeypatch.delenv("LOOM_CREDENTIAL_KEY", raising=False)
        provider = default_key_provider(app_dir=tmp_path)
        assert isinstance(provider, KeyringKeyProvider)


# ---------------------------------------------------------------------------
# Envelope + MultiFernet rotation
# ---------------------------------------------------------------------------


class _StaticKeyProvider:
    def __init__(self, *keys: bytes) -> None:
        self._keys = list(keys)

    def keys(self) -> list[bytes]:
        return self._keys


class TestEnvelopeRotation:
    def test_encrypts_and_decrypts_round_trip(self) -> None:
        key = Fernet.generate_key()
        envelope = Envelope(_StaticKeyProvider(key))
        assert envelope.decrypt(envelope.encrypt(b"secret")) == b"secret"

    def test_an_old_key_still_decrypts_after_rotation(self) -> None:
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()

        written_with_old = Envelope(_StaticKeyProvider(old_key)).encrypt(b"payload")

        # Rotated: new key first (for new writes), old key still present (for
        # decrypting what was written before the rotation).
        rotated = Envelope(_StaticKeyProvider(new_key, old_key))
        assert rotated.decrypt(written_with_old) == b"payload"

    def test_new_writes_after_rotation_use_the_new_key_only(self) -> None:
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()

        rotated = Envelope(_StaticKeyProvider(new_key, old_key))
        ciphertext = rotated.encrypt(b"payload")

        # Decryptable with only the new key — proves it was not encrypted
        # with the old one.
        assert Envelope(_StaticKeyProvider(new_key)).decrypt(ciphertext) == b"payload"
        with pytest.raises(DecryptionError):
            Envelope(_StaticKeyProvider(old_key)).decrypt(ciphertext)

    def test_a_key_no_longer_present_after_rotation_cannot_decrypt(self) -> None:
        retired_key = Fernet.generate_key()
        ciphertext = Envelope(_StaticKeyProvider(retired_key)).encrypt(b"payload")

        current = Envelope(_StaticKeyProvider(Fernet.generate_key()))
        with pytest.raises(DecryptionError):
            current.decrypt(ciphertext)

    def test_decryption_error_message_never_contains_the_plaintext(self) -> None:
        ciphertext = Envelope(_StaticKeyProvider(Fernet.generate_key())).encrypt(
            b"super-secret-marker-xyz"
        )
        try:
            Envelope(_StaticKeyProvider(Fernet.generate_key())).decrypt(ciphertext)
        except DecryptionError as exc:
            assert "super-secret-marker-xyz" not in str(exc)
        else:
            pytest.fail("expected DecryptionError")


# ---------------------------------------------------------------------------
# atomic_write_bytes
# ---------------------------------------------------------------------------


class TestAtomicWriteBytes:
    def test_writes_the_file_with_the_requested_mode(self, tmp_path) -> None:
        path = tmp_path / "sub" / "file.bin"
        atomic_write_bytes(path, b"data", mode=0o600)
        assert path.read_bytes() == b"data"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_creates_the_parent_directory_as_0700(self, tmp_path) -> None:
        path = tmp_path / "nested" / "file.bin"
        atomic_write_bytes(path, b"x")
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_no_temp_file_survives_a_successful_write(self, tmp_path) -> None:
        path = tmp_path / "file.bin"
        atomic_write_bytes(path, b"data")
        leftovers = [p for p in tmp_path.iterdir() if p.name != "file.bin"]
        assert leftovers == []

    def test_overwrites_an_existing_file_atomically(self, tmp_path) -> None:
        path = tmp_path / "file.bin"
        atomic_write_bytes(path, b"old")
        atomic_write_bytes(path, b"new")
        assert path.read_bytes() == b"new"


# ---------------------------------------------------------------------------
# EncryptedFileCredentialStore: corrupt / truncated files
# ---------------------------------------------------------------------------


class TestCorruptStore:
    async def test_an_empty_file_raises_rather_than_reading_as_no_credentials(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", Fernet.generate_key().decode())
        path = tmp_path / "creds.enc"
        path.write_bytes(b"")

        store = EncryptedFileCredentialStore(path, key_provider=EnvKeyProvider())
        with pytest.raises(DecryptionError, match="re-run"):
            await store.names()

    async def test_garbage_bytes_raise_a_decryption_error(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", Fernet.generate_key().decode())
        path = tmp_path / "creds.enc"
        path.write_bytes(b"not even close to a fernet token")

        store = EncryptedFileCredentialStore(path, key_provider=EnvKeyProvider())
        with pytest.raises(DecryptionError):
            await store.names()

    async def test_a_missing_file_is_empty_not_an_error(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", Fernet.generate_key().decode())
        store = EncryptedFileCredentialStore(
            tmp_path / "does-not-exist.enc", key_provider=EnvKeyProvider()
        )
        assert await store.names() == []


# ---------------------------------------------------------------------------
# KeyringCredentialStore: payload size stays independent of the keyring cap
# ---------------------------------------------------------------------------


class TestKeyringPayloadSplit:
    async def test_a_large_credential_set_never_touches_the_keyring_size_limit(
        self, tmp_path, fake_keyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for the Windows Credential Manager ~2.5KB cap:
        the payload always goes to the file, however large it gets.

        The `delenv` is what keeps this test about the keyring. The session
        fixture in `conftest.py` pins `LOOM_CREDENTIAL_KEY` so nothing reaches
        a developer's real keychain, and `KeyringCredentialStore` now honours
        it — so without this line the store would take the env branch and the
        assertion below would be about a keyring nothing had written to.
        """
        import keyring

        monkeypatch.delenv("LOOM_CREDENTIAL_KEY", raising=False)
        store = KeyringCredentialStore(tmp_path / "creds.enc")
        large_metadata = {"blob": "x" * 10_000}
        for i in range(20):
            await store.put(
                f"conn-{i}",
                StoredCredential(token=Secret("t" * 500), metadata=large_metadata),
            )

        assert len(await store.names()) == 20
        stored_key = keyring.get_password("loom-credential-store", "master-key")
        assert stored_key is not None
        assert len(stored_key) < 100
        assert (tmp_path / "creds.enc").stat().st_size > 10_000 * 20 * 0.5


# ---------------------------------------------------------------------------
# Which key source the CLI's own store uses
# ---------------------------------------------------------------------------


class TestTheStoreTheCliBuildsFollowsTheKeyOrder:
    """`KeyringCredentialStore` used to pick its provider by hand.

    So `LOOM_CREDENTIAL_KEY` — rung 1 of the documented order, and the thing
    `default_key_provider`'s own docstring calls "what CI and containers use" —
    reached `EncryptedFileCredentialStore` and nothing else. Every CLI path
    (`loom connect`, `loom whoami`, `loom doctor`, `oauth_client`) builds this
    class, so the escape hatch was documented and unreachable.
    """

    def test_the_default_is_exactly_what_shipped_before(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property that matters most: with neither variable set, nothing
        about a deployment changes — same provider, same service, same
        fallback path."""
        monkeypatch.delenv("LOOM_CREDENTIAL_KEY", raising=False)
        monkeypatch.delenv("LOOM_CREDENTIAL_BACKEND", raising=False)

        store = KeyringCredentialStore(tmp_path / "creds.enc", service="svc")
        provider = store._envelope._key_provider

        assert isinstance(provider, KeyringKeyProvider)
        assert provider._service == "svc"
        assert isinstance(provider._fallback, GeneratedFileKeyProvider)
        assert provider._fallback._path == tmp_path / "credentials.key"

    async def test_an_explicit_key_never_consults_the_keyring(
        self, tmp_path, broken_keyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix for the macOS password prompt.

        A keyring that raises on every call stands in for one that would put a
        modal dialog on screen — which is not an exception anything can catch,
        so the only safe answer is never to reach for it. A round-trip that
        succeeds here, with no generated key file left behind, is what says the
        keychain was not on the path at all.
        """
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", Fernet.generate_key().decode("ascii"))

        store = KeyringCredentialStore(tmp_path / "creds.enc")
        await store.put("acme", _cred("tok-a"))

        assert (await store.get("acme")).reveal() == "tok-a"
        assert not (tmp_path / "credentials.key").exists()

    def test_a_malformed_key_is_refused_rather_than_fallen_back_from(
        self, tmp_path, fake_keyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who names a key has said where the key comes from.

        Quietly using a different one because theirs did not parse is the
        silent downgrade this ordering exists to prevent — and it would encrypt
        the next write under a key they do not have.
        """
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", "not-a-fernet-key")

        with pytest.raises(DecryptionError):
            KeyringCredentialStore(tmp_path / "creds.enc")._envelope.encrypt(b"x")
        assert not (tmp_path / "credentials.key").exists()

    def test_the_refusal_never_carries_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This message reaches logs and CI output."""
        secret = "totally-not-valid-but-secret-looking"
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", secret)

        with pytest.raises(DecryptionError) as caught:
            EnvKeyProvider().keys()
        assert secret not in str(caught.value)
        assert secret not in repr(EnvKeyProvider())

    def test_a_bad_key_in_a_list_is_named_by_position(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = Fernet.generate_key().decode("ascii")
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", f"{good},rubbish")

        with pytest.raises(DecryptionError, match="key 2"):
            EnvKeyProvider().keys()


class TestTheBackendCanBePinned:
    """`LOOM_CREDENTIAL_BACKEND`, and the rule that a pinned one never degrades."""

    def test_file_never_touches_the_keyring(
        self, tmp_path, broken_keyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOOM_CREDENTIAL_BACKEND", "file")
        monkeypatch.delenv("LOOM_CREDENTIAL_KEY", raising=False)

        store = KeyringCredentialStore(tmp_path / "creds.enc")
        store._envelope.encrypt(b"x")  # a broken keyring would raise

        assert (tmp_path / "credentials.key").exists()
        assert stat.S_IMODE((tmp_path / "credentials.key").stat().st_mode) == 0o600

    def test_keyring_raises_rather_than_writing_a_key_file(
        self, tmp_path, broken_keyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asking for the keyring and getting a file is the silent downgrade.

        Under `auto` the fallback is right — nobody stated a preference, and a
        working store beats a failed command. Stated explicitly, it is wrong:
        the key would land somewhere the operator ruled out.
        """
        monkeypatch.setenv("LOOM_CREDENTIAL_BACKEND", "keyring")
        monkeypatch.delenv("LOOM_CREDENTIAL_KEY", raising=False)

        with pytest.raises(LookupError):
            KeyringCredentialStore(tmp_path / "creds.enc")._envelope.encrypt(b"x")
        assert not (tmp_path / "credentials.key").exists()

    def test_env_requires_the_variable(
        self, tmp_path, fake_keyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOOM_CREDENTIAL_BACKEND", "env")
        monkeypatch.delenv("LOOM_CREDENTIAL_KEY", raising=False)

        with pytest.raises(LookupError):
            KeyringCredentialStore(tmp_path / "creds.enc")._envelope.encrypt(b"x")

    def test_an_unknown_backend_is_refused_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loom.connectors.encryption import CREDENTIAL_BACKENDS, selected_backend
        from loom.core.exceptions import ConfigurationError

        monkeypatch.setenv("LOOM_CREDENTIAL_BACKEND", "kechain")
        with pytest.raises(ConfigurationError) as caught:
            selected_backend()
        for name in CREDENTIAL_BACKENDS:
            assert name in str(caught.value)


class TestRotatingTheKey:
    """A comma-separated `LOOM_CREDENTIAL_KEY`: first encrypts, any decrypts.

    Without this, pinning a key is a one-way door — every credential already
    stored becomes unreadable, and the only way forward is re-connecting each
    integration by hand. That cost is what stops people adopting the setting
    at all.
    """

    async def test_an_old_key_still_reads_and_the_new_one_takes_over(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = Fernet.generate_key().decode("ascii")
        new = Fernet.generate_key().decode("ascii")
        path = tmp_path / "creds.enc"

        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", old)
        await KeyringCredentialStore(path).put("acme", _cred("tok-old"))

        # Both keys: the store written under `old` is readable again.
        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", f"{new},{old}")
        rotating = KeyringCredentialStore(path)
        assert (await rotating.get("acme")).reveal() == "tok-old"

        # A write re-encrypts the whole file under the first key.
        await rotating.put("beta", _cred("tok-new"))

        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", new)
        settled = KeyringCredentialStore(path)
        assert (await settled.get("acme")).reveal() == "tok-old"
        assert (await settled.get("beta")).reveal() == "tok-new"

    async def test_the_old_key_alone_no_longer_opens_it(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = Fernet.generate_key().decode("ascii")
        new = Fernet.generate_key().decode("ascii")
        path = tmp_path / "creds.enc"

        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", old)
        await KeyringCredentialStore(path).put("acme", _cred("tok"))

        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", f"{new},{old}")
        await KeyringCredentialStore(path).put("acme", _cred("tok"))

        monkeypatch.setenv("LOOM_CREDENTIAL_KEY", old)
        with pytest.raises(DecryptionError, match="LOOM_CREDENTIAL_KEY"):
            await KeyringCredentialStore(path).names()
