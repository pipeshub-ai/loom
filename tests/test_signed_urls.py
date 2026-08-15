"""Signed URL sessions, upload confirmation, and artifact labels."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from loom.blobs.artifact import ArtifactService, StoreBackedArtifactStore
from loom.blobs.blob import BlobService, LocalBlobBackend
from loom.blobs.signed_urls import (
    SignedUrlService,
    UploadNotFound,
    UploadTooLarge,
)
from loom.core.exceptions import ConfigurationError
from loom.stores.memory import MemoryStore


@pytest.fixture
def blobs(tmp_path: Path) -> BlobService:
    return BlobService(
        LocalBlobBackend(tmp_path / "blobs", base_url="http://localhost:8000")
    )


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def artifacts(blobs: BlobService, store: MemoryStore) -> ArtifactService:
    return ArtifactService(blobs, StoreBackedArtifactStore(store))


@pytest.fixture
def urls(blobs: BlobService, store: MemoryStore) -> SignedUrlService:
    return SignedUrlService(blobs, store)


class TestDownloadUrl:
    async def test_download_url_is_fresh(self, urls: SignedUrlService) -> None:
        ref = "blob:" + "a" * 64
        first = await urls.download_url(ref, expires_in=60)
        second = await urls.download_url(ref, expires_in=60)
        assert first.url != ""
        assert "sig=" in first.url
        assert first.ref == ref
        # Two mints are independent — not a stored field on the artifact.
        assert first.expires_at <= second.expires_at or first.url != second.url

    async def test_expires_capped_at_a_day(self, urls: SignedUrlService) -> None:
        from loom.blobs.signed_urls import MAX_EXPIRES_IN

        assert urls._clamp(999_999, 3600) == MAX_EXPIRES_IN

    async def test_bare_local_cannot_sign(self, tmp_path: Path, store: MemoryStore) -> None:
        service = SignedUrlService(
            BlobService(LocalBlobBackend(tmp_path / "blobs")), store
        )
        with pytest.raises(ConfigurationError, match="signed URLs"):
            await service.create_upload_session("report.pdf")


class TestUploadSession:
    async def test_create_and_confirm(
        self, urls: SignedUrlService, blobs: BlobService, artifacts: ArtifactService
    ) -> None:
        session = await urls.create_upload_session(
            "report.pdf", mime="application/pdf", max_size=1024
        )
        assert session.upload_id
        assert session.key.startswith("staging/")
        assert "method=PUT" in session.url
        # URL is omitted from repr so logs cannot leak it.
        assert session.url not in repr(session)

        data = b"%PDF-fake"
        await blobs.backend.put(session.key, data, session.mime)
        version = await urls.confirm_upload(
            session.upload_id, artifacts=artifacts, run_id="run_1"
        )
        assert version.name == "report.pdf"
        assert version.sha256 == hashlib.sha256(data).hexdigest()
        assert await artifacts.read("report.pdf") == data
        assert not await blobs.backend.exists(session.key)

    async def test_confirm_is_idempotent(
        self, urls: SignedUrlService, blobs: BlobService, artifacts: ArtifactService
    ) -> None:
        session = await urls.create_upload_session("once.txt", mime="text/plain")
        await blobs.backend.put(session.key, b"hello", session.mime)
        first = await urls.confirm_upload(session.upload_id, artifacts=artifacts)
        second = await urls.confirm_upload(session.upload_id, artifacts=artifacts)
        assert first.qualified_name == second.qualified_name
        assert first.sha256 == second.sha256

    async def test_confirm_missing_upload_raises(self, urls: SignedUrlService) -> None:
        with pytest.raises(UploadNotFound):
            await urls.confirm_upload("no-such")

    async def test_oversized_upload_is_rejected(
        self, urls: SignedUrlService, blobs: BlobService, artifacts: ArtifactService
    ) -> None:
        session = await urls.create_upload_session("big.bin", max_size=4)
        await blobs.backend.put(session.key, b"12345", session.mime)
        with pytest.raises(UploadTooLarge):
            await urls.confirm_upload(session.upload_id, artifacts=artifacts)
        assert not await blobs.backend.exists(session.key)

    async def test_abort_cleans_up(
        self, urls: SignedUrlService, blobs: BlobService
    ) -> None:
        session = await urls.create_upload_session("gone.txt")
        await blobs.backend.put(session.key, b"x", session.mime)
        await urls.abort_upload(session.upload_id)
        assert not await blobs.backend.exists(session.key)
        with pytest.raises(UploadNotFound):
            await urls._load_session(session.upload_id)


class TestArtifactLabels:
    async def test_put_records_labels(self, artifacts: ArtifactService) -> None:
        version = await artifacts.put(
            "labelled.txt",
            b"hi",
            mime="text/plain",
            labels={"env": "test"},
            content_disposition='attachment; filename="labelled.txt"',
        )
        assert version.labels == {"env": "test"}
        assert "labelled.txt" in version.content_disposition

    async def test_names_lists_published(self, artifacts: ArtifactService) -> None:
        await artifacts.put("a.txt", b"a")
        await artifacts.put("b.txt", b"b")
        assert set(await artifacts.names()) == {"a.txt", "b.txt"}
