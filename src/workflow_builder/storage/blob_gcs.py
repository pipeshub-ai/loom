"""Google Cloud Storage backend.

Requires ``google-cloud-storage``. The Python SDK is synchronous, so every
I/O call is wrapped in ``asyncio.to_thread``. The client is created once
and reused (it is thread-safe).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from workflow_builder.core.exceptions import ConfigurationError
from workflow_builder.storage.blob import (
    BlobMeta,
    BlobNotFoundError,
    _require_signed_method,
)


class GCSBlobBackend:
    """Blob storage on Google Cloud Storage.

    Parameters
    ----------
    bucket:
        Target bucket name. It must already exist.
    prefix:
        Key prefix within the bucket.
    project:
        GCP project. Defaults to the SDK default.
    credentials_path:
        Path to a service account JSON. Defaults to Application Default
        Credentials.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "loom/blobs",
        project: str = "",
        credentials_path: str = "",
    ) -> None:
        self._bucket_name = bucket
        self._prefix = prefix.strip("/")
        self._project = project
        self._credentials_path = credentials_path
        self._client: Any = None

    def _key_for(self, ref: str) -> str:
        return f"{self._prefix}/{ref[:2]}/{ref}"

    def _gcs_client(self) -> Any:
        if self._client is not None:
            return self._client
        from google.cloud import storage

        kwargs: dict[str, Any] = {}
        if self._project:
            kwargs["project"] = self._project
        if self._credentials_path:
            from google.oauth2 import service_account

            kwargs["credentials"] = (
                service_account.Credentials.from_service_account_file(
                    self._credentials_path
                )
            )
        self._client = storage.Client(**kwargs)
        return self._client

    def _blob(self, ref: str) -> Any:
        bucket = self._gcs_client().bucket(self._bucket_name)
        return bucket.blob(self._key_for(ref))

    async def close(self) -> None:
        """Drop the cached client. GCS clients have no async close."""
        self._client = None

    async def put(self, ref: str, data: bytes, mime: str) -> None:
        blob = self._blob(ref)

        def _upload() -> None:
            blob.upload_from_string(data, content_type=mime)

        await asyncio.to_thread(_upload)

    async def get(self, ref: str) -> bytes:
        blob = self._blob(ref)

        def _download() -> bytes:
            if not blob.exists():
                raise BlobNotFoundError(ref)
            return blob.download_as_bytes()

        return await asyncio.to_thread(_download)

    async def exists(self, ref: str) -> bool:
        blob = self._blob(ref)
        return bool(await asyncio.to_thread(blob.exists))

    async def delete(self, ref: str) -> None:
        blob = self._blob(ref)

        def _delete() -> None:
            if blob.exists():
                blob.delete()

        await asyncio.to_thread(_delete)

    async def signed_url(
        self,
        ref: str,
        *,
        method: str = "GET",
        expires_in: int = 3600,
        content_type: str | None = None,
    ) -> str:
        """V4 signed URL. Requires service-account credentials that can sign."""
        verb = _require_signed_method(method)
        blob = self._blob(ref)
        kwargs: dict[str, Any] = {
            "version": "v4",
            "expiration": timedelta(seconds=expires_in),
            "method": verb,
        }
        if content_type and verb == "PUT":
            kwargs["content_type"] = content_type

        def _sign() -> str:
            return blob.generate_signed_url(**kwargs)

        try:
            return await asyncio.to_thread(_sign)
        except Exception as exc:
            raise ConfigurationError(
                "GCSBlobBackend.signed_url() needs service-account credentials "
                "that can sign. Pass credentials_path= or configure ADC with "
                "a service account."
            ) from exc

    async def head(self, ref: str) -> BlobMeta:
        blob = self._blob(ref)

        def _reload() -> BlobMeta:
            if not blob.exists():
                raise BlobNotFoundError(ref)
            blob.reload()
            mime = blob.content_type or "application/octet-stream"
            etag = (blob.etag or "").strip('"')
            return BlobMeta(
                ref=ref,
                size=int(blob.size or 0),
                mime=mime,
                etag=etag,
                last_modified=blob.updated,
            )

        return await asyncio.to_thread(_reload)
