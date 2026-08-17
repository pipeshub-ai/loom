"""Content-addressed blob storage for large payloads.

Payloads exceeding 256 KB are stored externally and referenced by
``blob:<sha256>`` in the journal. This keeps journal rows small and
database queries fast.

Backends may additionally implement :class:`SignableBackend`,
:class:`HeadableBackend`, or :class:`StreamableBackend`. Capability
checks go through :class:`BlobService`, not scattered ``isinstance``
calls on the backend itself.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from loom.core.exceptions import ConfigurationError

BLOB_URL_ENV = "LOOM_BLOBS"
"""Environment variable :func:`blob_service_from_env` reads."""

_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_SIGNED_METHODS = frozenset({"GET", "PUT"})


class BlobNotFoundError(Exception):
    """Raised when a requested blob does not exist in the backend."""


class BlobMeta(BaseModel):
    """Metadata returned by :meth:`HeadableBackend.head` without downloading content."""

    ref: str
    size: int
    mime: str = "application/octet-stream"
    etag: str = ""
    last_modified: datetime | None = None


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


@runtime_checkable
class SignableBackend(Protocol):
    """A blob backend that can produce time-limited signed URLs.

    Local backends produce HMAC-signed URLs verified by a download handler.
    Cloud backends delegate to the provider's native signing (S3 presigned
    URL, Azure SAS, GCS signed URL).
    """

    async def signed_url(
        self,
        ref: str,
        *,
        method: str = "GET",
        expires_in: int = 3600,
        content_type: str | None = None,
    ) -> str: ...


@runtime_checkable
class HeadableBackend(Protocol):
    """A blob backend that can return metadata without downloading content."""

    async def head(self, ref: str) -> BlobMeta: ...


@runtime_checkable
class StreamableBackend(Protocol):
    """A blob backend that supports chunked reads.

    ``stream()`` returns an async context manager so cloud clients can hold
    the connection open while chunks are consumed::

        async with backend.stream(ref) as chunks:
            async for chunk in chunks:
                ...
    """

    def stream(
        self, ref: str, *, chunk_size: int = 65_536
    ) -> Any: ...


def content_hash_of(ref: str) -> str:
    """Strip a ``blob:`` prefix and require a 64-character hex digest.

    Used by :class:`BlobService` for content-addressed load/delete. Staging
    keys (``staging/{uuid}``) are not content-addressed and must not go
    through this.
    """
    digest = ref.removeprefix("blob:")
    if not _CONTENT_HASH.fullmatch(digest):
        raise ValueError(
            f"invalid blob ref {ref!r}; content-addressed refs are 64 hex characters"
        )
    return digest


def _require_signed_method(method: str) -> str:
    verb = method.upper()
    if verb not in _SIGNED_METHODS:
        raise ConfigurationError(
            f"signed URLs only support GET and PUT, not {method!r}"
        )
    return verb


def _safe_path(base_dir: Path, ref: str) -> Path:
    """Join *ref* under *base_dir* with two-character fan-out, rejecting traversal."""
    if not ref or ref.startswith("/") or ".." in Path(ref).parts:
        raise ValueError(f"invalid blob ref {ref!r}")
    dest = (base_dir / ref[:2] / ref).resolve()
    try:
        dest.relative_to(base_dir.resolve())
    except ValueError:
        raise ValueError(f"invalid blob ref {ref!r}") from None
    return dest


class LocalBlobBackend:
    """Filesystem-backed blob backend.

    Blobs are stored under *base_dir* using a two-character prefix
    directory for fan-out::

        base_dir/ab/abcdef0123...

    Pass *base_url* to enable HMAC-signed download URLs verified by the
    LOOM HTTP handler. Without it, :meth:`signed_url` raises rather than
    producing an unreachable URL.
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        base_url: str = "",
        signing_secret: str = "",
    ) -> None:
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._base_url = base_url.rstrip("/")
        # Random when unset so a default install cannot be signed against
        # with a well-known secret. Pin this in production so URLs survive
        # a process restart.
        self._signing_secret = signing_secret or secrets.token_hex(32)

    def _path_for(self, ref: str) -> Path:
        return _safe_path(self._base_dir, ref)

    def can_sign(self) -> bool:
        """Whether this backend can produce a reachable signed URL."""
        return bool(self._base_url)

    async def put(self, ref: str, data: bytes, mime: str) -> None:
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

    async def signed_url(
        self,
        ref: str,
        *,
        method: str = "GET",
        expires_in: int = 3600,
        content_type: str | None = None,
    ) -> str:
        """HMAC-signed URL verified by the LOOM download handler.

        Raises:
            ConfigurationError: When *base_url* was not set at construction.
        """
        del content_type  # local URLs do not bind Content-Type
        if not self._base_url:
            raise ConfigurationError(
                "LocalBlobBackend.signed_url() requires base_url to be set. "
                "Use LocalBlobBackend(base_dir, base_url='http://localhost:8000') "
                "or configure $LOOM_BLOBS_BASE_URL."
            )
        verb = _require_signed_method(method)
        expires_at = int(time.time()) + expires_in
        sig = self._signature(verb, ref, expires_at)
        return (
            f"{self._base_url}/blobs/{ref}"
            f"?expires={expires_at}&sig={sig}&method={verb}"
        )

    def verify_signed_url(
        self, ref: str, expires: int, sig: str, method: str = "GET"
    ) -> bool:
        """Verify an HMAC signature. Used by the HTTP download handler."""
        if int(time.time()) > int(expires):
            return False
        expected = self._signature(method.upper(), ref, int(expires))
        return hmac.compare_digest(sig, expected)

    def _signature(self, method: str, ref: str, expires: int) -> str:
        payload = f"{method}:{ref}:{expires}"
        return hmac.new(
            self._signing_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    async def head(self, ref: str) -> BlobMeta:
        dest = self._path_for(ref)
        if not dest.exists():
            raise BlobNotFoundError(ref)
        stat = dest.stat()
        return BlobMeta(
            ref=ref,
            size=stat.st_size,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    @asynccontextmanager
    async def stream(
        self, ref: str, *, chunk_size: int = 65_536
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        dest = self._path_for(ref)
        if not dest.exists():
            raise BlobNotFoundError(ref)
        handle = dest.open("rb")
        try:

            async def chunks() -> AsyncIterator[bytes]:
                while True:
                    data = handle.read(chunk_size)
                    if not data:
                        break
                    yield data

            yield chunks()
        finally:
            handle.close()


class S3BlobBackend:
    """Blob storage on S3 or any S3-compatible service (MinIO, R2, Spaces).

    Requires ``aioboto3``. The client is created lazily so importing this module
    costs nothing when S3 is not in use.

    Parameters
    ----------
    bucket:
        Target bucket. It must already exist — creating buckets is a deployment
        decision, not something a workflow engine should do implicitly.
    prefix:
        Key prefix, so one bucket can hold several environments.
    session_kwargs:
        Passed to ``aioboto3.Session``; ``client_kwargs`` to ``session.client``
        (``endpoint_url`` for MinIO and friends).
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "loom/blobs",
        session_kwargs: dict[str, Any] | None = None,
        client_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._session_kwargs = session_kwargs or {}
        self._client_kwargs = client_kwargs or {}

    def _key_for(self, ref: str) -> str:
        # Two-character fan-out, matching the local backend, so migrating
        # between them is a copy rather than a re-layout.
        return f"{self._prefix}/{ref[:2]}/{ref}"

    def _client(self) -> Any:
        import aioboto3

        session = aioboto3.Session(**self._session_kwargs)
        return session.client("s3", **self._client_kwargs)

    async def put(self, ref: str, data: bytes, mime: str) -> None:
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self._bucket,
                Key=self._key_for(ref),
                Body=data,
                ContentType=mime,
            )

    async def get(self, ref: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            async with self._client() as s3:
                response = await s3.get_object(
                    Bucket=self._bucket, Key=self._key_for(ref)
                )
                return await response["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise BlobNotFoundError(ref) from exc
            raise

    async def exists(self, ref: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            async with self._client() as s3:
                await s3.head_object(Bucket=self._bucket, Key=self._key_for(ref))
                return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return False
            raise

    async def delete(self, ref: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self._bucket, Key=self._key_for(ref))

    async def signed_url(
        self,
        ref: str,
        *,
        method: str = "GET",
        expires_in: int = 3600,
        content_type: str | None = None,
    ) -> str:
        """S3 presigned URL via ``generate_presigned_url``."""
        verb = _require_signed_method(method)
        client_method = "put_object" if verb == "PUT" else "get_object"
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": self._key_for(ref),
        }
        if content_type and verb == "PUT":
            params["ContentType"] = content_type
        async with self._client() as s3:
            url = s3.generate_presigned_url(
                ClientMethod=client_method,
                Params=params,
                ExpiresIn=expires_in,
            )
            if hasattr(url, "__await__"):
                return await url
            return url

    async def head(self, ref: str) -> BlobMeta:
        from botocore.exceptions import ClientError

        try:
            async with self._client() as s3:
                response = await s3.head_object(
                    Bucket=self._bucket, Key=self._key_for(ref)
                )
                last_modified = response.get("LastModified")
                return BlobMeta(
                    ref=ref,
                    size=int(response.get("ContentLength", 0) or 0),
                    mime=response.get("ContentType", "application/octet-stream"),
                    etag=str(response.get("ETag", "")).strip('"'),
                    last_modified=last_modified,
                )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise BlobNotFoundError(ref) from exc
            raise

    @asynccontextmanager
    async def stream(
        self, ref: str, *, chunk_size: int = 65_536
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        from botocore.exceptions import ClientError

        try:
            async with self._client() as s3:
                response = await s3.get_object(
                    Bucket=self._bucket, Key=self._key_for(ref)
                )
                body = response["Body"]

                async def chunks() -> AsyncIterator[bytes]:
                    while True:
                        data = await body.read(chunk_size)
                        if not data:
                            break
                        yield data

                yield chunks()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise BlobNotFoundError(ref) from exc
            raise


class BlobService:
    """High-level content-addressed blob service.

    Provides helper methods for deciding when to offload payloads,
    computing content hashes, and managing blob lifecycle through
    a pluggable :class:`BlobBackend`.
    """

    OFFLOAD_THRESHOLD: int = 256 * 1024  # 256 KB
    """Default size above which a journal payload is stored by content hash.

    Kept as a class attribute so existing code reading it still works; pass
    ``threshold=`` to tune one service. It was *only* a class attribute before,
    which made the single number governing every journal payload in a
    deployment adjustable exclusively by monkeypatching the class.
    """

    def __init__(self, backend: BlobBackend, *, threshold: int | None = None) -> None:
        self._backend = backend
        self._threshold = self.OFFLOAD_THRESHOLD if threshold is None else threshold

    @property
    def threshold(self) -> int:
        """Bytes above which :meth:`should_offload` says yes."""
        return self._threshold

    @property
    def backend(self) -> BlobBackend:
        """Expose the backend for capability checks. Read-only."""
        return self._backend

    @property
    def supports_signed_urls(self) -> bool:
        """Whether :meth:`signed_url` can produce a reachable URL."""
        if not isinstance(self._backend, SignableBackend):
            return False
        can_sign = getattr(self._backend, "can_sign", None)
        if callable(can_sign):
            return bool(can_sign())
        return True

    @property
    def supports_head(self) -> bool:
        return isinstance(self._backend, HeadableBackend)

    def should_offload(self, data: bytes) -> bool:
        """Return ``True`` if *data* exceeds the offload threshold."""
        return len(data) > self._threshold

    async def store(
        self, data: bytes, mime: str = "application/json"
    ) -> str:
        """Hash *data*, persist via the backend, and return a blob ref.

        Empty bytes are accepted (marker files). Duplicate content is stored
        once.

        Returns:
            A string of the form ``blob:<sha256-hex>``.
        """
        digest = hashlib.sha256(data).hexdigest()
        if not await self._backend.exists(digest):
            await self._backend.put(digest, data, mime)
        return f"blob:{digest}"

    async def load(self, ref: str) -> bytes:
        """Load the blob identified by a ``blob:<hash>`` reference.

        Raises:
            BlobNotFoundError: If the underlying blob is missing.
            ValueError: If *ref* is not a 64-character hex digest.
        """
        return await self._backend.get(content_hash_of(ref))

    async def delete(self, ref: str) -> None:
        """Delete the blob identified by a ``blob:<hash>`` reference."""
        await self._backend.delete(content_hash_of(ref))

    async def signed_url(
        self,
        ref: str,
        *,
        method: str = "GET",
        expires_in: int = 3600,
        content_type: str | None = None,
    ) -> str:
        """Delegate to the backend's signing, translating ``blob:`` refs to raw refs."""
        if not self.supports_signed_urls:
            raise ConfigurationError(
                f"{type(self._backend).__name__} does not support signed URLs. "
                "Use S3BlobBackend, AzureBlobBackend, GCSBlobBackend, or "
                "LocalBlobBackend with base_url set."
            )
        raw = ref.removeprefix("blob:")
        return await self._backend.signed_url(
            raw,
            method=method,
            expires_in=expires_in,
            content_type=content_type,
        )

    async def head(self, ref: str) -> BlobMeta:
        """Metadata without downloading. Raises if the backend is not headable."""
        if not self.supports_head:
            raise ConfigurationError(
                f"{type(self._backend).__name__} does not support head(). "
                "Use a cloud backend or LocalBlobBackend."
            )
        raw = ref.removeprefix("blob:")
        return await self._backend.head(raw)

    @staticmethod
    def is_blob_ref(ref: str) -> bool:
        """Return ``True`` if *ref* looks like a blob reference."""
        return ref.startswith("blob:")


def blob_backend_from_url(url: str, **kwargs: Any) -> BlobBackend:
    """Construct a blob backend from a URL scheme.

    ============================  ====================================
    Scheme                        Backend
    ============================  ====================================
    ``file:///path/to/dir``       :class:`LocalBlobBackend`
    ``s3://bucket/prefix``        :class:`S3BlobBackend` (extra: s3)
    ``az://container/prefix``     :class:`AzureBlobBackend` (extra: azure)
    ``gs://bucket/prefix``        :class:`GCSBlobBackend` (extra: gcs)
    ============================  ====================================
    """
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    scheme = (parsed.scheme or "file").lower()

    if scheme == "file":
        path = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc not in ("localhost", ""):
            path = f"/{parsed.netloc}{path}"
        if not path:
            raise ConfigurationError(
                f"blob URL {url!r} has no path; expected file:///path/to/dir"
            )
        base_url = kwargs.pop("base_url", "") or os.environ.get(
            "LOOM_BLOBS_BASE_URL", ""
        )
        signing_secret = kwargs.pop("signing_secret", "") or os.environ.get(
            "LOOM_BLOBS_SIGNING_SECRET", ""
        )
        return LocalBlobBackend(
            Path(path),
            base_url=base_url,
            signing_secret=signing_secret,
            **kwargs,
        )

    if scheme == "s3":
        try:
            import aioboto3  # noqa: F401
        except ImportError as exc:
            raise ConfigurationError(
                "S3BlobBackend needs aioboto3: pip install 'loomflow[s3]'"
            ) from exc
        bucket = parsed.netloc
        if not bucket:
            raise ConfigurationError(
                f"blob URL {url!r} has no bucket; expected s3://bucket/prefix"
            )
        prefix = parsed.path.lstrip("/") or "loom/blobs"
        return S3BlobBackend(bucket, prefix=prefix, **kwargs)

    if scheme in ("az", "azure"):
        try:
            import azure.storage.blob  # noqa: F401
        except ImportError as exc:
            raise ConfigurationError(
                "AzureBlobBackend needs azure-storage-blob: "
                "pip install 'loomflow[azure]'"
            ) from exc
        from loom.blobs.blob_azure import AzureBlobBackend

        container = parsed.netloc
        if not container:
            raise ConfigurationError(
                f"blob URL {url!r} has no container; expected az://container/prefix"
            )
        prefix = parsed.path.lstrip("/") or "loom/blobs"
        connection_string = kwargs.pop("connection_string", "") or os.environ.get(
            "AZURE_STORAGE_CONNECTION_STRING", ""
        )
        return AzureBlobBackend(
            container, connection_string=connection_string, prefix=prefix, **kwargs
        )

    if scheme in ("gs", "gcs"):
        try:
            import google.cloud.storage  # noqa: F401
        except ImportError as exc:
            raise ConfigurationError(
                "GCSBlobBackend needs google-cloud-storage: "
                "pip install 'loomflow[gcs]'"
            ) from exc
        from loom.blobs.blob_gcs import GCSBlobBackend

        bucket = parsed.netloc
        if not bucket:
            raise ConfigurationError(
                f"blob URL {url!r} has no bucket; expected gs://bucket/prefix"
            )
        prefix = parsed.path.lstrip("/") or "loom/blobs"
        return GCSBlobBackend(bucket, prefix=prefix, **kwargs)

    raise ConfigurationError(
        f"unknown blob URL scheme {scheme!r} in {url!r}. "
        "Supported: file://, s3://, az://, gs://"
    )


def blob_service_from_env(default: str = "") -> BlobService | None:
    """Build a :class:`BlobService` from ``$LOOM_BLOBS``, or ``None`` when unset."""
    url = os.environ.get(BLOB_URL_ENV, default)
    if not url:
        return None
    return BlobService(blob_backend_from_url(url))
