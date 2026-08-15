"""Envelope encryption for credentials at rest, and where the key lives.

Two concerns kept separate on purpose:

``KeyProvider``
    *Where the encryption key comes from* — environment variable, OS keyring,
    or a generated local file. Each source has a failure mode (unset, locked,
    lost), and callers should be able to swap one for another without
    touching how encryption itself works.
``Envelope``
    *How a payload is encrypted* — always :class:`~cryptography.fernet.Fernet`,
    via :class:`~cryptography.fernet.MultiFernet` so a key rotation can decrypt
    with an old key while encrypting new writes with the new one.

Neither of them is the ``CredentialStore`` — see :mod:`.credentials` for the
port that composes both into something that stores actual credentials.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from workflow_builder.core.exceptions import WorkflowError

__all__ = [
    "DecryptionError",
    "EnvKeyProvider",
    "Envelope",
    "GeneratedFileKeyProvider",
    "KeyProvider",
    "KeyringKeyProvider",
    "atomic_write_bytes",
    "default_key_provider",
]


def _fernet_module() -> Any:
    """Import ``cryptography.fernet`` on first use, not at package import.

    Nothing in the core dependency set needs ``cryptography`` —
    ``MemoryCredentialStore`` works without it, and so does importing this
    module. Only a code path that actually encrypts something pays for the
    import, with a message that names the extra rather than a bare
    ``ModuleNotFoundError``.
    """
    try:
        import cryptography.fernet as fernet_module
    except ImportError as exc:
        raise ImportError(
            "Encrypted credential storage needs the 'credentials' extra: "
            "pip install 'workflow-builder[credentials]'"
        ) from exc
    return fernet_module


class DecryptionError(WorkflowError):
    """Ciphertext could not be decrypted with any key the provider has.

    Covers both a genuinely wrong/rotated-away key and a corrupt or truncated
    file — either way, the store must not silently treat this as "no
    credentials", which would be indistinguishable from a clean logout.
    """


@runtime_checkable
class KeyProvider(Protocol):
    """Resolves the Fernet key(s) that encrypt and decrypt a credential store."""

    def keys(self) -> list[bytes]:
        """Every key that should be able to decrypt, newest first.

        The first key is also the one new writes encrypt with. Returning more
        than one is what makes key rotation possible: old ciphertext keeps
        decrypting under an old key while everything newly written uses the
        new one.
        """
        ...


class EnvKeyProvider:
    """Reads a base64 Fernet key from an environment variable.

    The escape hatch for CI and containers: no keyring, no local file, one
    key supplied by whatever already manages secrets there.
    """

    def __init__(self, var: str = "LOOM_CREDENTIAL_KEY") -> None:
        self._var = var

    def keys(self) -> list[bytes]:
        value = os.environ.get(self._var)
        if not value:
            raise LookupError(f"{self._var} is not set")
        return [value.encode("ascii")]

    def __repr__(self) -> str:
        return f"<EnvKeyProvider {self._var}>"


class GeneratedFileKeyProvider:
    """Generates a key once and reuses it from a local ``0600`` file.

    The fallback of last resort. Warns loudly on generation because a key
    that lives only on this machine means credentials encrypted with it
    cannot be read after a reinstall, a move to another host, or losing the
    file — ``LOOM_CREDENTIAL_KEY`` is the portable alternative.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._warn = warn or (lambda msg: warnings.warn(msg, stacklevel=3))

    def keys(self) -> list[bytes]:
        if self._path.exists():
            key = self._path.read_bytes().strip()
            if not key:
                raise DecryptionError(
                    f"key file at {self._path} exists but is empty; "
                    "delete it to generate a new one, or restore a backup."
                )
            return [key]

        key = _fernet_module().Fernet.generate_key()
        atomic_write_bytes(self._path, key, mode=0o600)
        self._warn(
            f"Generated a machine-local credential encryption key at "
            f"{self._path}. Credentials encrypted with it cannot be read on "
            "another machine, or after this file is lost. Set "
            "LOOM_CREDENTIAL_KEY to use a portable key instead."
        )
        return [key]

    def __repr__(self) -> str:
        return f"<GeneratedFileKeyProvider {self._path}>"


class KeyringKeyProvider:
    """Stores the master key in the OS keyring; never the payload itself.

    Windows Credential Manager caps a stored value near 2.5 KB, which a
    realistic token set exceeds — so only the (tiny, fixed-size) Fernet key
    goes in the keyring. The encrypted credential payload always lives in a
    file; see :class:`~workflow_builder.connectors.credentials.EncryptedFileCredentialStore`.

    A keyring that is absent, locked, or otherwise unusable (headless Linux
    with no secret service, a locked macOS keychain) is only discovered by
    calling it — there is nothing to probe at import time — so every call
    here falls back to *fallback* rather than raising, unless none was given.
    """

    def __init__(
        self,
        *,
        service: str = "loom-credential-store",
        username: str = "master-key",
        fallback: KeyProvider | None = None,
    ) -> None:
        self._service = service
        self._username = username
        self._fallback = fallback

    def keys(self) -> list[bytes]:
        try:
            return self._keys_from_keyring()
        except Exception as exc:  # keyring's failure modes are backend-specific
            if self._fallback is not None:
                return self._fallback.keys()
            raise LookupError(f"OS keyring is unavailable: {exc}") from exc

    def _keys_from_keyring(self) -> list[bytes]:
        import keyring

        existing = keyring.get_password(self._service, self._username)
        if existing:
            return [existing.encode("ascii")]

        key = _fernet_module().Fernet.generate_key()
        keyring.set_password(self._service, self._username, key.decode("ascii"))
        return [key]

    def __repr__(self) -> str:
        return f"<KeyringKeyProvider service={self._service!r}>"


class Envelope:
    """Encrypts with the newest key; decrypts with any key the provider has."""

    def __init__(self, key_provider: KeyProvider) -> None:
        self._key_provider = key_provider

    def _cipher(self) -> Any:
        fernet = _fernet_module()
        keys = self._key_provider.keys()
        if not keys:
            raise LookupError(f"{self._key_provider!r} returned no keys")
        return fernet.MultiFernet([fernet.Fernet(key) for key in keys])

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._cipher().encrypt(plaintext)  # type: ignore[no-any-return]

    def decrypt(self, ciphertext: bytes) -> bytes:
        try:
            return self._cipher().decrypt(ciphertext)  # type: ignore[no-any-return]
        except _fernet_module().InvalidToken as exc:
            raise DecryptionError(
                "could not decrypt the credential store with any known key. "
                "The encryption key may have changed, or the file is "
                "corrupt. Re-run 'loom login'."
            ) from exc

    def __repr__(self) -> str:
        return f"<Envelope {self._key_provider!r}>"


def default_key_provider(*, app_dir: Path | str) -> KeyProvider:
    """The priority order from the plan's corner cases, in one place.

    1. ``LOOM_CREDENTIAL_KEY`` — explicit, portable, what CI and containers use.
    2. The OS keyring — the default for an interactive ``loom login``.
    3. A generated file under *app_dir*, machine-local, with a loud warning.
    """
    if os.environ.get("LOOM_CREDENTIAL_KEY"):
        return EnvKeyProvider()
    fallback = GeneratedFileKeyProvider(Path(app_dir) / "credentials.key")
    return KeyringKeyProvider(fallback=fallback)


def atomic_write_bytes(path: Path | str, data: bytes, *, mode: int = 0o600) -> None:
    """Write *data* to *path* so a reader never observes a partial write.

    A temp file in the same directory (so ``os.replace`` is same-filesystem
    and therefore atomic on every platform this targets) plus a rename. A
    crash or a concurrent read mid-write sees either the old file or the new
    one, never a half-written one — which is what "encrypted credential
    store" needs from "concurrent writers", per the plan's own scope: this
    prevents corruption, not a lost update between two writers that both read
    before either wrote.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
