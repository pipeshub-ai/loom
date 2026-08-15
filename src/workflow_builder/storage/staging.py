"""Per-run artifact staging.

A staged artifact is bytes already in blob storage, waiting to be committed
as a named version. Staging is scoped to a run so two concurrent workflows
can both stage ``"report.pdf"`` without overwriting each other.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from workflow_builder.storage.artifact import ArtifactVersion
from workflow_builder.storage.attachment import Attachment
from workflow_builder.storage.blob import BlobService


class StagingNotFound(Exception):  # noqa: N818
    """No staged artifact exists under that name for that run."""


class StagedArtifact(BaseModel):
    """A file waiting to be committed as an artifact version."""

    name: str
    ref: str
    """``blob:<sha256>`` reference."""
    mime: str
    size: int
    sha256: str
    staged_at: datetime
    staged_by_run: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class StagingManager:
    """Accumulates files before committing them as versioned artifacts.

    Persistence uses a :class:`~workflow_builder.state.base.CacheStore`, so
    staged artifacts survive a crash. TTL is 24 hours by default — a staged
    artifact that is never committed is cleaned up automatically.
    """

    def __init__(
        self,
        blobs: BlobService,
        artifacts: Any,
        store: Any,
        *,
        ttl: int = 86_400,
    ) -> None:
        self._blobs = blobs
        self._artifacts = artifacts
        self._store = store
        self._ttl = ttl

    def _key(self, run_id: str, name: str) -> str:
        return f"staging:{run_id}:{name}"

    def _manifest_key(self, run_id: str) -> str:
        return f"staging:{run_id}:__manifest__"

    async def _add_to_manifest(self, run_id: str, name: str) -> None:
        names = list(await self._store.get(self._manifest_key(run_id)) or [])
        if name not in names:
            names.append(name)
            await self._store.set(self._manifest_key(run_id), names, self._ttl)

    async def _remove_from_manifest(self, run_id: str, name: str) -> None:
        names = list(await self._store.get(self._manifest_key(run_id)) or [])
        if name in names:
            names = [item for item in names if item != name]
            if names:
                await self._store.set(self._manifest_key(run_id), names, self._ttl)
            else:
                await self._store.delete(self._manifest_key(run_id))

    async def stage(
        self,
        name: str,
        data: bytes | Attachment,
        *,
        mime: str = "application/octet-stream",
        run_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> StagedArtifact:
        """Write bytes to blob storage and record the staging entry.

        If *data* is an already-offloaded :class:`Attachment`, the existing
        ``ref`` is reused rather than re-uploaded.
        """
        extra = dict(metadata or {})
        if isinstance(data, Attachment):
            mime = data.mime or mime
            if data.filename:
                extra.setdefault("filename", data.filename)
            extra.update(data.metadata)
            if data.is_offloaded and data.ref:
                ref = data.ref
                digest = data.sha256 or data.ref.removeprefix("blob:")
                size = data.size
            else:
                raw = data.data or b""
                ref = await self._blobs.store(raw, mime)
                digest = data.sha256 or hashlib.sha256(raw).hexdigest()
                size = data.size or len(raw)
        else:
            raw = data
            ref = await self._blobs.store(raw, mime)
            digest = hashlib.sha256(raw).hexdigest()
            size = len(raw)

        staged = StagedArtifact(
            name=name,
            ref=ref,
            mime=mime,
            size=size,
            sha256=digest,
            staged_at=datetime.now(UTC),
            staged_by_run=run_id,
            metadata=extra,
        )
        await self._store.set(
            self._key(run_id, name), staged.model_dump(mode="json"), self._ttl
        )
        await self._add_to_manifest(run_id, name)
        return staged

    async def get_staged(
        self, name: str, *, run_id: str = ""
    ) -> StagedArtifact | None:
        """Look up a single staging entry."""
        raw = await self._store.get(self._key(run_id, name))
        if not raw:
            return None
        return StagedArtifact.model_validate(raw)

    async def list_staged(self, *, run_id: str) -> list[StagedArtifact]:
        """All staging entries for a run."""
        names = list(await self._store.get(self._manifest_key(run_id)) or [])
        result: list[StagedArtifact] = []
        for name in names:
            item = await self.get_staged(name, run_id=run_id)
            if item is not None:
                result.append(item)
        return result

    async def commit(
        self,
        name: str,
        *,
        run_id: str = "",
        labels: dict[str, str] | None = None,
    ) -> ArtifactVersion:
        """Promote a staged artifact to a versioned artifact.

        Raises:
            StagingNotFound: When nothing is staged under *name* for *run_id*.
        """
        staged = await self.get_staged(name, run_id=run_id)
        if staged is None:
            raise StagingNotFound(
                f"nothing staged as {name!r} for run {run_id!r}"
            )
        raw = await self._blobs.load(staged.ref)
        extra = dict(staged.metadata)
        filename = extra.pop("filename", None)
        disposition = f'attachment; filename="{filename}"' if filename else ""
        version = await self._artifacts.put(
            name,
            raw,
            mime=staged.mime,
            run_id=run_id,
            labels=labels,
            content_disposition=disposition,
            **extra,
        )
        await self.discard(name, run_id=run_id)
        return version

    async def discard(self, name: str, *, run_id: str = "") -> None:
        """Drop a staged artifact. The blob is left in place (it may be shared)."""
        await self._store.delete(self._key(run_id, name))
        await self._remove_from_manifest(run_id, name)

    async def discard_all(self, *, run_id: str) -> None:
        """Drop every staging entry for *run_id*. Used by retention."""
        names = list(await self._store.get(self._manifest_key(run_id)) or [])
        for name in names:
            await self._store.delete(self._key(run_id, name))
        await self._store.delete(self._manifest_key(run_id))
