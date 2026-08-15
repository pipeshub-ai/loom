"""Signed URL generation and presigned-upload confirmation.

A signed URL is ephemeral: it expires. This module never stores one on an
:class:`~loom.blobs.artifact.ArtifactVersion`. Downloads are
minted on demand; uploads go to a staging key, then move to a
content-addressed location after confirmation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from loom.blobs.artifact import ArtifactVersion
from loom.blobs.blob import BlobNotFoundError, BlobService
from loom.core.exceptions import ConfigurationError

logger = logging.getLogger("workflow.storage.signed_urls")

MAX_EXPIRES_IN = 86_400
"""Hard cap on signed-URL lifetime (24 hours)."""

DEFAULT_DOWNLOAD_EXPIRES = 3_600
DEFAULT_UPLOAD_EXPIRES = 900
_SESSION_GRACE = 300
_SESSION_PREFIX = "upload-session:"


class UploadNotFound(Exception):  # noqa: N818
    """No upload session exists under that id, or it has expired."""


class UploadTooLarge(Exception):  # noqa: N818
    """The uploaded object exceeded the session's ``max_size``."""


class UploadSession(BaseModel):
    """Tracks a presigned upload from creation to confirmation."""

    upload_id: str
    key: str
    name: str
    mime: str
    max_size: int | None = None
    url: str = Field(repr=False)
    created_at: datetime
    expires_at: datetime
    confirmed: bool = False
    artifact: ArtifactVersion | None = None
    """Set once confirmation succeeds, so a second confirm is idempotent."""


class SignedUrlResponse(BaseModel):
    """A freshly minted download URL."""

    url: str = Field(repr=False)
    ref: str
    expires_at: datetime


class SignedUrlService:
    """Coordinates signed URL generation and upload confirmation.

    Not a blob backend — a service layer that uses :class:`BlobService` and
    an :class:`~loom.blobs.artifact.ArtifactService` to manage
    the upload lifecycle.
    """

    def __init__(
        self,
        blobs: BlobService,
        store: Any,
        *,
        default_download_expires: int = DEFAULT_DOWNLOAD_EXPIRES,
        default_upload_expires: int = DEFAULT_UPLOAD_EXPIRES,
    ) -> None:
        self._blobs = blobs
        self._store = store
        self._default_download_expires = default_download_expires
        self._default_upload_expires = default_upload_expires

    def _clamp(self, expires_in: int | None, default: int) -> int:
        value = default if expires_in is None else int(expires_in)
        if value <= 0:
            raise ConfigurationError("expires_in must be a positive number of seconds")
        return min(value, MAX_EXPIRES_IN)

    def _session_key(self, upload_id: str) -> str:
        return f"{_SESSION_PREFIX}{upload_id}"

    async def download_url(
        self,
        ref: str,
        *,
        expires_in: int | None = None,
    ) -> SignedUrlResponse:
        """Mint a GET URL for an existing blob ref."""
        ttl = self._clamp(expires_in, self._default_download_expires)
        url = await self._blobs.signed_url(ref, method="GET", expires_in=ttl)
        return SignedUrlResponse(
            url=url,
            ref=ref,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )

    async def create_upload_session(
        self,
        name: str,
        *,
        mime: str = "application/octet-stream",
        max_size: int | None = None,
        expires_in: int | None = None,
    ) -> UploadSession:
        """Generate a presigned PUT URL and persist the session.

        The upload key is a staging path, not a content-addressed path —
        the content hash is unknown until the client writes the bytes.
        """
        if not self._blobs.supports_signed_urls:
            raise ConfigurationError(
                f"{type(self._blobs.backend).__name__} does not support signed URLs. "
                "Use S3BlobBackend, AzureBlobBackend, GCSBlobBackend, or "
                "LocalBlobBackend with base_url set."
            )
        ttl = self._clamp(expires_in, self._default_upload_expires)
        upload_id = uuid.uuid4().hex
        key = f"staging/{upload_id}"
        now = datetime.now(UTC)
        url = await self._blobs.signed_url(
            key, method="PUT", expires_in=ttl, content_type=mime
        )
        session = UploadSession(
            upload_id=upload_id,
            key=key,
            name=name,
            mime=mime,
            max_size=max_size,
            url=url,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        await self._store.set(
            self._session_key(upload_id),
            session.model_dump(mode="json"),
            ttl + _SESSION_GRACE,
        )
        return session

    async def _load_session(self, upload_id: str) -> UploadSession:
        raw = await self._store.get(self._session_key(upload_id))
        if not raw:
            raise UploadNotFound(f"no upload session '{upload_id}'")
        return UploadSession.model_validate(raw)

    async def confirm_upload(
        self,
        upload_id: str,
        *,
        artifacts: Any | None = None,
        run_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactVersion:
        """Verify the upload landed, hash it, and publish as an artifact.

        Idempotent: a second confirm of the same session returns the same
        :class:`ArtifactVersion`.
        """
        session = await self._load_session(upload_id)
        if session.confirmed and session.artifact is not None:
            return session.artifact
        if datetime.now(UTC) > session.expires_at + timedelta(seconds=_SESSION_GRACE):
            raise UploadNotFound(f"upload session '{upload_id}' has expired")

        backend = self._blobs.backend
        try:
            if self._blobs.supports_head:
                meta = await backend.head(session.key)  # type: ignore[union-attr]
                size = meta.size
            else:
                size = None
            data = await backend.get(session.key)
        except BlobNotFoundError as exc:
            raise UploadNotFound(
                f"upload '{upload_id}' has not landed; PUT to the signed URL first"
            ) from exc

        if size is None:
            size = len(data)
        if session.max_size is not None and size > session.max_size:
            await backend.delete(session.key)
            raise UploadTooLarge(
                f"upload '{upload_id}' is {size} bytes; max_size is {session.max_size}"
            )

        if artifacts is None:
            raise ConfigurationError(
                "confirm_upload needs an ArtifactService. Pass artifacts=..."
            )
        extra = dict(metadata or {})
        version = await artifacts.put(
            session.name,
            data,
            mime=session.mime,
            run_id=run_id,
            **extra,
        )
        await backend.delete(session.key)
        session.confirmed = True
        session.artifact = version
        session.url = ""
        await self._store.set(
            self._session_key(upload_id),
            session.model_dump(mode="json"),
            _SESSION_GRACE,
        )
        logger.info("confirmed upload %s as %s", upload_id, version.qualified_name)
        return version

    async def abort_upload(self, upload_id: str) -> None:
        """Clean up an abandoned upload session. No-op if already gone."""
        try:
            session = await self._load_session(upload_id)
        except UploadNotFound:
            return
        await self._blobs.backend.delete(session.key)
        await self._store.delete(self._session_key(upload_id))
