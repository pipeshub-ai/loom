"""Content-addressed blob storage for large payloads.

Payloads exceeding 256 KB are stored externally and referenced by
``blob:<sha256>`` in the journal. This keeps journal rows small and
database queries fast.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol, runtime_checkable


class BlobNotFoundError(Exception):
    """Raised when a requested blob does not exist in the backend."""


@runtime_checkable
class BlobBackend(Protocol):
    """Async protocol for content-addressed blob persistence."""

    async def put(self, ref: str, data: bytes, mime: str) -> None:
        """Store *data* under the given *ref* with content type *mime*."""
        ...

    async def get(self, ref: str) -> bytes:
        """Retrieve the blob identified by *ref*.

        Raises:
            BlobNotFoundError: If *ref* does not exist.
        """
        ...

    async def exists(self, ref: str) -> bool:
        """Return ``True`` if *ref* is stored in the backend."""
        ...

    async def delete(self, ref: str) -> None:
        """Remove the blob identified by *ref*.  No-op if missing."""
        ...


class LocalBlobBackend:
    """Filesystem-backed blob backend.

    Blobs are stored under *base_dir* using a two-character prefix
    directory for fan-out::

        base_dir/ab/abcdef0123...
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, ref: str) -> Path:
        return self._base_dir / ref[:2] / ref

    async def put(
        self, ref: str, data: bytes, mime: str
    ) -> None:
        dest = self._path_for(ref)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    async def get(self, ref: str) -> bytes:
        dest = self._path_for(ref)
        if not dest.exists():
            raise BlobNotFoundError(ref)
        return dest.read_bytes()

    async def exists(self, ref: str) -> bool:
        return self._path_for(ref).exists()

    async def delete(self, ref: str) -> None:
        dest = self._path_for(ref)
        if dest.exists():
            dest.unlink()


class BlobService:
    """High-level content-addressed blob service.

    Provides helper methods for deciding when to offload payloads,
    computing content hashes, and managing blob lifecycle through
    a pluggable :class:`BlobBackend`.
    """

    OFFLOAD_THRESHOLD: int = 256 * 1024  # 256 KB

    def __init__(self, backend: BlobBackend) -> None:
        self._backend = backend

    def should_offload(self, data: bytes) -> bool:
        """Return ``True`` if *data* exceeds the offload threshold."""
        return len(data) > self.OFFLOAD_THRESHOLD

    async def store(
        self, data: bytes, mime: str = "application/json"
    ) -> str:
        """Hash *data*, persist via the backend, and return a blob ref.

        Returns:
            A string of the form ``blob:<sha256-hex>``.
        """
        content_hash = hashlib.sha256(data).hexdigest()
        if not await self._backend.exists(content_hash):
            await self._backend.put(content_hash, data, mime)
        return f"blob:{content_hash}"

    async def load(self, ref: str) -> bytes:
        """Load the blob identified by a ``blob:<hash>`` reference.

        Raises:
            BlobNotFoundError: If the underlying blob is missing.
        """
        content_hash = ref.removeprefix("blob:")
        return await self._backend.get(content_hash)

    async def delete(self, ref: str) -> None:
        """Delete the blob identified by a ``blob:<hash>`` reference."""
        content_hash = ref.removeprefix("blob:")
        await self._backend.delete(content_hash)

    @staticmethod
    def is_blob_ref(ref: str) -> bool:
        """Return ``True`` if *ref* looks like a blob reference."""
        return ref.startswith("blob:")
