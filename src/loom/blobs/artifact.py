"""Named, versioned artifacts.

Blob storage is content-addressed, which makes it immutable by construction:
writing changed bytes produces a different address and nothing links the two.
That is the right substrate and the wrong interface for "here is v2 of the
report" — you need a stable *name* whose value moves.

This module adds that layer. An :class:`ArtifactVersion` binds a name and a
version number to one immutable blob; an :class:`ArtifactStore` keeps the
append-only chain per name. Publishing identical bytes twice is a no-op that
returns the existing version, so a workflow that is replayed or retried does not
accumulate duplicate versions of the same content.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from loom.core.exceptions import ConfigurationError


class ArtifactNotFound(Exception):  # noqa: N818 - reads as a condition, not an error type
    """No artifact exists under the requested name, or not at that version."""


class ArtifactVersion(BaseModel):
    """One immutable version of a named artifact."""

    name: str
    version: int
    """1-based, monotonically increasing per name."""
    ref: str
    """``blob:<sha256>`` reference to the content."""
    mime: str = "application/octet-stream"
    size: int = 0
    sha256: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by_run: str = ""
    """Run that published this version, so an artifact is traceable to its
    producer without a separate audit log."""
    metadata: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    """User-defined key-value labels for filtering and search."""
    content_disposition: str = ""
    """Suggested filename for HTTP Content-Disposition on download.
    Defaults to the artifact name when not set explicitly."""

    @property
    def qualified_name(self) -> str:
        """``name@version`` — how a specific version is addressed."""
        return f"{self.name}@{self.version}"


@runtime_checkable
class ArtifactStore(Protocol):
    """Persistence for the name → versions index.

    Content lives in blob storage; only the index lives here, so an
    implementation over Postgres or Mongo stores small rows regardless of how
    large the artifacts are.
    """

    async def append(self, version: ArtifactVersion) -> None:
        """Record a new version. The caller has already assigned the number."""
        ...

    async def versions(self, name: str) -> list[ArtifactVersion]:
        """Every version of *name*, oldest first. Empty when unknown."""
        ...

    async def names(self) -> list[str]:
        """All artifact names with at least one version."""
        ...


class InMemoryArtifactStore:
    """Process-local index. Fine for tests and single-process use."""

    def __init__(self) -> None:
        self._versions: dict[str, list[ArtifactVersion]] = {}

    async def append(self, version: ArtifactVersion) -> None:
        self._versions.setdefault(version.name, []).append(version)

    async def versions(self, name: str) -> list[ArtifactVersion]:
        return list(self._versions.get(name, []))

    async def names(self) -> list[str]:
        return list(self._versions)


class StoreBackedArtifactStore:
    """Index persisted through any :class:`CacheStore`, so it survives restarts.

    Uses the execution store by default, which means artifacts need no
    infrastructure beyond what the engine already has.
    """

    def __init__(self, store: Any, *, namespace: str = "artifact") -> None:
        self._store = store
        self._namespace = namespace

    def _key(self, name: str) -> str:
        return f"{self._namespace}:{name}"

    def _names_key(self) -> str:
        return f"{self._namespace}:__names__"

    async def append(self, version: ArtifactVersion) -> None:
        existing = await self.versions(version.name)
        payload = [v.model_dump(mode="json") for v in [*existing, version]]
        # No TTL: an artifact index outliving its runs is the point.
        await self._store.set(self._key(version.name), payload, 0)
        known = await self.names()
        if version.name not in known:
            known.append(version.name)
            await self._store.set(self._names_key(), known, 0)

    async def versions(self, name: str) -> list[ArtifactVersion]:
        raw = await self._store.get(self._key(name))
        if not raw:
            return []
        return [ArtifactVersion.model_validate(item) for item in raw]

    async def names(self) -> list[str]:
        raw = await self._store.get(self._names_key())
        if not raw:
            return []
        return list(raw)


@asynccontextmanager
async def _index_lock(index: ArtifactStore, name: str) -> Any:
    """Serialize ``versions() → check digest → append()`` when the index has a lock."""
    store = getattr(index, "_store", None)
    acquire = getattr(store, "acquire", None) if store is not None else None
    # `store is None` implies `acquire is None`, but they are two separate
    # lookups so that implication is invisible; testing both is what lets the
    # release below be checked rather than assumed.
    if acquire is None or store is None:
        yield
        return
    key = f"artifact:lock:{name}"
    owner = "artifact-service"
    await acquire(key, owner, 30.0)
    try:
        yield
    finally:
        await store.release(key, owner)


class ArtifactService:
    """Publishes and resolves named artifacts over a blob backend.

    Parameters
    ----------
    blobs:
        A :class:`~loom.blobs.blob.BlobService` holding content.
    index:
        Where the name → versions chain lives.
    """

    def __init__(self, blobs: Any, index: ArtifactStore | None = None) -> None:
        self._blobs = blobs
        self._index = index or InMemoryArtifactStore()

    async def put(
        self,
        name: str,
        data: bytes,
        *,
        mime: str = "application/octet-stream",
        run_id: str = "",
        labels: dict[str, str] | None = None,
        content_disposition: str = "",
        **metadata: Any,
    ) -> ArtifactVersion:
        """Publish content under *name*, returning the version it became.

        Re-publishing identical bytes returns the existing version rather than
        creating a duplicate. Retries and replays are therefore free, and the
        version number keeps meaning "the content changed".
        """
        digest = hashlib.sha256(data).hexdigest()
        async with _index_lock(self._index, name):
            history = await self._index.versions(name)
            if history and history[-1].sha256 == digest:
                return history[-1]

            ref = await self._blobs.store(data, mime)
            version = ArtifactVersion(
                name=name,
                version=len(history) + 1,
                ref=ref,
                mime=mime,
                size=len(data),
                sha256=digest,
                created_by_run=run_id,
                metadata=metadata,
                labels=labels or {},
                content_disposition=content_disposition,
            )
            await self._index.append(version)
            return version

    async def get(self, name: str, version: int | None = None) -> ArtifactVersion:
        """Resolve a version. ``None`` means latest.

        Raises :class:`ArtifactNotFound` rather than returning ``None``, because
        a workflow reading an artifact that is not there should stop, not carry
        on with a missing value.
        """
        history = await self._index.versions(name)
        if not history:
            raise ArtifactNotFound(f"no artifact named {name!r}")
        if version is None:
            return history[-1]
        for candidate in history:
            if candidate.version == version:
                return candidate
        raise ArtifactNotFound(
            f"artifact {name!r} has no version {version} "
            f"(latest is {history[-1].version})"
        )

    async def read(self, name: str, version: int | None = None) -> bytes:
        """Fetch the content of a version."""
        payload: bytes = await self._blobs.load((await self.get(name, version)).ref)
        return payload

    async def history(self, name: str) -> list[ArtifactVersion]:
        """Every version of *name*, oldest first."""
        return await self._index.versions(name)

    async def names(self) -> list[str]:
        """All artifact names with at least one version."""
        return await self._index.names()

    async def url(
        self,
        name: str,
        version: int | None = None,
        *,
        expires_in: int = 3600,
    ) -> str:
        """Presigned download URL for an artifact version.

        Raises:
            ConfigurationError: If the blob backend cannot sign URLs.
        """
        if not getattr(self._blobs, "supports_signed_urls", False):
            raise ConfigurationError(
                "artifact URLs need a blob backend that can sign. "
                "Use S3, Azure, GCS, or LocalBlobBackend with base_url set."
            )
        resolved = await self.get(name, version)
        url: str = await self._blobs.signed_url(resolved.ref, expires_in=expires_in)
        return url
