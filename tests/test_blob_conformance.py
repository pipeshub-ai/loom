"""Backend-agnostic blob protocol conformance.

Local always runs. Cloud backends skip unless the matching extra is installed
*and* a live credential URL is in the environment — CI must not hit the
network by default.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from workflow_builder.storage.blob import (
    BlobBackend,
    BlobNotFoundError,
    LocalBlobBackend,
)


def _local(tmp_path: Path) -> LocalBlobBackend:
    return LocalBlobBackend(tmp_path / "conformance-blobs")


def _s3() -> BlobBackend | None:
    url = os.environ.get("LOOM_TEST_S3_URL")
    if not url:
        return None
    try:
        from workflow_builder.storage.blob import blob_backend_from_url

        return blob_backend_from_url(url)
    except Exception:
        return None


def _azure() -> BlobBackend | None:
    url = os.environ.get("LOOM_TEST_AZURE_URL")
    if not url:
        return None
    try:
        from workflow_builder.storage.blob import blob_backend_from_url

        return blob_backend_from_url(url)
    except Exception:
        return None


def _gcs() -> BlobBackend | None:
    url = os.environ.get("LOOM_TEST_GCS_URL")
    if not url:
        return None
    try:
        from workflow_builder.storage.blob import blob_backend_from_url

        return blob_backend_from_url(url)
    except Exception:
        return None


def _backends(tmp_path: Path) -> list[tuple[str, BlobBackend]]:
    found: list[tuple[str, BlobBackend]] = [("local", _local(tmp_path))]
    for name, factory in (("s3", _s3), ("azure", _azure), ("gcs", _gcs)):
        backend = factory()
        if backend is not None:
            found.append((name, backend))
    return found


@pytest.fixture
def backends(tmp_path: Path) -> list[tuple[str, BlobBackend]]:
    return _backends(tmp_path)


async def _exercise(backend: BlobBackend, ref: str) -> None:
    data = b"conformance-payload"
    await backend.put(ref, data, "text/plain")
    assert await backend.exists(ref)
    assert await backend.get(ref) == data
    head = getattr(backend, "head", None)
    if head is not None:
        meta = await head(ref)
        assert meta.size == len(data)
    await backend.delete(ref)
    assert not await backend.exists(ref)
    with pytest.raises(BlobNotFoundError):
        await backend.get(ref)
    await backend.delete(ref)  # no-op


class TestBlobConformance:
    async def test_put_get_exists_delete_round_trip(
        self, backends: list[tuple[str, BlobBackend]]
    ) -> None:
        digest = hashlib.sha256(b"conformance-payload").hexdigest()
        for name, backend in backends:
            await _exercise(backend, digest + name[:4])

    async def test_empty_bytes(self, backends: list[tuple[str, BlobBackend]]) -> None:
        digest = hashlib.sha256(b"").hexdigest()
        for _name, backend in backends:
            await backend.put(digest, b"", "application/octet-stream")
            assert await backend.get(digest) == b""
            await backend.delete(digest)

    async def test_missing_get_raises(
        self, backends: list[tuple[str, BlobBackend]]
    ) -> None:
        missing = "f" * 64
        for _name, backend in backends:
            with pytest.raises(BlobNotFoundError):
                await backend.get(missing)

    def test_cloud_backends_are_skippable(self) -> None:
        """Documented skip: no URL in env means the backend is not in the list."""
        if not os.environ.get("LOOM_TEST_S3_URL"):
            assert _s3() is None
        if not os.environ.get("LOOM_TEST_AZURE_URL"):
            assert _azure() is None
        if not os.environ.get("LOOM_TEST_GCS_URL"):
            assert _gcs() is None
