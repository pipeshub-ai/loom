"""Capability tests for blob backends — signing, head, factory, ref safety.

``tests/test_phase5.py`` already covers LocalBlobBackend put/get/exists/delete
and BlobService offload/dedup. This file does not repeat those.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from loom.blobs.blob import (
    BlobMeta,
    BlobNotFoundError,
    BlobService,
    HeadableBackend,
    LocalBlobBackend,
    SignableBackend,
    blob_backend_from_url,
    blob_service_from_env,
)
from loom.core.exceptions import ConfigurationError
from loom.runtime.engine import Runtime


@pytest.fixture
def backend(tmp_path: Path) -> LocalBlobBackend:
    return LocalBlobBackend(tmp_path / "blobs", base_url="http://localhost:8000")


class TestLocalSigning:
    async def test_signed_url_contains_hmac_and_expiry(
        self, backend: LocalBlobBackend
    ) -> None:
        ref = "a" * 64
        url = await backend.signed_url(ref, method="GET", expires_in=60)
        assert url.startswith("http://localhost:8000/blobs/")
        assert "sig=" in url
        assert "expires=" in url
        assert "method=GET" in url

    async def test_verify_accepts_a_fresh_url(self, backend: LocalBlobBackend) -> None:
        ref = "b" * 64
        url = await backend.signed_url(ref, expires_in=60)
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(url).query)
        assert backend.verify_signed_url(
            ref, int(query["expires"][0]), query["sig"][0], query["method"][0]
        )

    async def test_verify_rejects_tampered_sig(self, backend: LocalBlobBackend) -> None:
        assert not backend.verify_signed_url("c" * 64, int(time.time()) + 60, "0" * 64)

    async def test_verify_rejects_expired(self, backend: LocalBlobBackend) -> None:
        ref = "d" * 64
        url = await backend.signed_url(ref, expires_in=-1)
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(url).query)
        assert not backend.verify_signed_url(
            ref, int(query["expires"][0]), query["sig"][0]
        )

    async def test_signed_url_without_base_url_raises(self, tmp_path: Path) -> None:
        bare = LocalBlobBackend(tmp_path / "blobs")
        with pytest.raises(ConfigurationError, match="base_url"):
            await bare.signed_url("e" * 64)

    async def test_delete_method_is_refused(self, backend: LocalBlobBackend) -> None:
        with pytest.raises(ConfigurationError, match="GET and PUT"):
            await backend.signed_url("f" * 64, method="DELETE")


class TestLocalHeadAndStream:
    async def test_head_returns_size(self, backend: LocalBlobBackend) -> None:
        await backend.put("abc123", b"hello", "text/plain")
        meta = await backend.head("abc123")
        assert isinstance(meta, BlobMeta)
        assert meta.size == 5
        assert meta.ref == "abc123"

    async def test_head_missing_raises(self, backend: LocalBlobBackend) -> None:
        with pytest.raises(BlobNotFoundError):
            await backend.head("missing")

    async def test_stream_yields_chunks(self, backend: LocalBlobBackend) -> None:
        await backend.put("streamed", b"abcdefghij", "text/plain")
        collected = b""
        async with backend.stream("streamed", chunk_size=3) as chunks:
            async for chunk in chunks:
                collected += chunk
        assert collected == b"abcdefghij"


class TestBlobServiceCapabilities:
    def test_supports_signed_urls_false_for_bare_local(self, tmp_path: Path) -> None:
        service = BlobService(LocalBlobBackend(tmp_path / "blobs"))
        assert not service.supports_signed_urls
        assert service.supports_head
        assert isinstance(service.backend, SignableBackend)
        assert isinstance(service.backend, HeadableBackend)

    def test_supports_signed_urls_true_when_base_url_set(self, tmp_path: Path) -> None:
        service = BlobService(
            LocalBlobBackend(tmp_path / "blobs", base_url="http://localhost:8000")
        )
        assert service.supports_signed_urls

    async def test_signed_url_strips_blob_prefix(self, tmp_path: Path) -> None:
        backend = LocalBlobBackend(tmp_path / "blobs", base_url="http://x")
        service = BlobService(backend)
        url = await service.signed_url("blob:" + "a" * 64, expires_in=30)
        assert "/blobs/" + "a" * 64 in url

    async def test_empty_bytes_are_stored(self, tmp_path: Path) -> None:
        service = BlobService(LocalBlobBackend(tmp_path / "blobs"))
        ref = await service.store(b"", mime="application/octet-stream")
        assert await service.load(ref) == b""
        assert hashlib.sha256(b"").hexdigest() in ref

    async def test_non_hex_ref_rejected(self, tmp_path: Path) -> None:
        service = BlobService(LocalBlobBackend(tmp_path / "blobs"))
        with pytest.raises(ValueError, match="64 hex"):
            await service.load("blob:not-a-hash")

    async def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        backend = LocalBlobBackend(tmp_path / "blobs")
        with pytest.raises(ValueError, match="invalid blob ref"):
            await backend.put("../etc/passwd", b"x", "text/plain")


class TestFactory:
    def test_file_url(self, tmp_path: Path) -> None:
        backend = blob_backend_from_url(tmp_path.as_uri())
        assert isinstance(backend, LocalBlobBackend)

    def test_unknown_scheme_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="unknown blob URL scheme"):
            blob_backend_from_url("ftp://nope")

    def test_s3_url_needs_the_extra(self) -> None:
        try:
            import aioboto3  # noqa: F401
        except ImportError:
            with pytest.raises(ConfigurationError, match="aioboto3"):
                blob_backend_from_url("s3://bucket/prefix")
            return
        backend = blob_backend_from_url("s3://bucket/prefix")
        from loom.blobs.blob import S3BlobBackend

        assert isinstance(backend, S3BlobBackend)

    def test_from_env_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOOM_BLOBS", raising=False)
        assert blob_service_from_env() is None

    def test_from_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOM_BLOBS", tmp_path.as_uri())
        service = blob_service_from_env()
        assert service is not None
        assert isinstance(service.backend, LocalBlobBackend)

    def test_runtime_from_env_wires_blobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOOM_BLOBS", tmp_path.as_uri())
        monkeypatch.delenv("LOOM_STORE", raising=False)
        rt = Runtime.from_env()
        assert rt.blobs is not None
        assert rt.artifacts is not None
        assert rt.staging is not None
        assert rt.signed_urls is not None
