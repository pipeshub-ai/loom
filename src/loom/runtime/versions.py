"""Immutable workflow versions: the source that ran, not just its fingerprint.

The catalog (:mod:`loom.runtime.registry`) answers *what workflows exist*. It
records a ``code_hash`` and a ``source_file``, which is enough to say "this run
was produced by code that has since changed" and not enough to say what that
code *was*. A host that must replay a run against the exact source it ran, or
show a reviewer the diff between two versions, has nowhere to look.

Versions answer *what code was live when*. Separate port, separate lifetime: a
catalog entry is current and mutable, a version is immutable and accumulates.
One store may back both.

**Source and IR live in blobs, not in the record.** A workflow is content, and
inlining content puts a 16MB ceiling on Mongo, a fat row on Postgres, and makes
"which store" a capability question. :class:`BlobService` is already a port with
file/S3/Azure/GCS adapters, so the record references content by hash and the
same code works on every combination.

    versions = StoreBackedVersionStore(store, blobs)
    committed = await versions.commit(
        WorkflowVersion(workflow="onboard", source="...", pins=Pins(toolsets={"jira": "1.2"}))
    )
    await versions.latest("onboard")
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from loom.core.exceptions import WorkflowError

__all__ = [
    "MissingVersionContent",
    "Pins",
    "StoreBackedVersionStore",
    "VersionConflict",
    "VersionStore",
    "WorkflowVersion",
    "content_hash_of",
]


class MissingVersionContent(WorkflowError):  # noqa: N818 - names the absence
    """A version's record exists but its source does not.

    Raised rather than falling back to the file on disk: the point of a version
    is that it is the code that *ran*, and quietly serving whatever is on disk
    now would make a replay rehearse something that never happened.
    """

    def __init__(self, ref: str, *, hint: str = "") -> None:
        self.ref = ref
        detail = f" — {hint}" if hint else ""
        super().__init__(f"version content {ref!r} is not retrievable{detail}")


class VersionConflict(WorkflowError):  # noqa: N818 - names the event
    """A commit raced another one.

    Carries both version numbers because the fix depends on which: an author
    whose base is one behind rebases, an author twenty behind probably wants to
    look at what changed.
    """

    def __init__(self, workflow: str, *, expected: int | None, actual: int | None) -> None:
        self.workflow = workflow
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{workflow} is at version {actual}, not {expected} — somebody else "
            "committed since you started. Re-read the latest version and apply "
            "your change on top of it."
        )


def content_hash_of(source: str) -> str:
    """The identity of a version. Same source, same hash, on every machine."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class Pins(BaseModel):
    """What a version was verified against.

    Without pins a version records *that* it passed the checks and not *what it
    passed them against*, so a toolset upgrade silently invalidates every
    version in the catalog with nothing to compare.
    """

    toolsets: dict[str, str] = Field(default_factory=dict)
    """Toolset id to the version resolved at commit time."""
    nodes: dict[str, str] = Field(default_factory=dict)
    """Node id to version, matching ``NodeSpec.version``."""
    agents: list[str] = Field(default_factory=list)
    """Agent ids the body reaches, from ``derive_grants``."""


class WorkflowVersion(BaseModel):
    """One immutable revision of a workflow's source."""

    workflow: str
    version: int = 0
    """Monotonic per workflow, assigned by the store on commit. Zero means
    unassigned — a caller never picks this."""

    content_hash: str = ""
    """sha256 of the source text. The version's identity: same source, same
    hash, on every machine."""

    code_hash: str = ""
    """The *runtime* fingerprint of the workflow body, matching
    ``ExecutionRecord.code_hash``.

    Two hashes because they answer two questions. ``content_hash`` identifies
    the source a human committed; ``code_hash`` is what a finished run carries,
    and it is derived from the function body rather than the file — so a
    reformatted file has a new content hash and the same code hash. Recording
    only one leaves either "show me this version's source" or "which version
    produced this run" unanswerable."""

    source: str = Field(default="", exclude=True)
    """The code, in memory only. Excluded from serialisation: it goes to blobs
    and comes back through :meth:`VersionStore.source_of`, so a record is small
    on every store and a 2MB workflow is not a Mongo problem."""

    source_ref: str = ""
    """``blob:<sha256>`` — where the source actually lives."""

    ir_ref: str = ""
    """Blob reference to the extracted WGIR, when one was stored."""

    pins: Pins = Field(default_factory=Pins)
    verifier_version: str = ""
    """Which check pipeline passed this. A version verified by an older
    pipeline is not wrong, but it is not the same claim."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = ""
    parent_version: int | None = None
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """``name@version`` — how one revision is addressed."""
        return f"{self.workflow}@{self.version}"


@runtime_checkable
class VersionStore(Protocol):
    """Persistence for immutable workflow versions.

    Four reads and one write. A host wanting versions in its own database —
    Arango, Neo4j, anything — implements these and passes it as
    ``Runtime(versions=…)``; nothing else changes.
    """

    async def commit(
        self, version: WorkflowVersion, *, expected_latest: int | None = None
    ) -> WorkflowVersion:
        """Append a version and return it with its assigned number.

        Committing identical source returns the **existing** version rather
        than creating a duplicate, so a re-publish of unchanged code does not
        inflate the chain — the property artifacts already have.

        ``expected_latest`` is optimistic concurrency. Two people editing one
        workflow is the normal case, and last-write-wins loses work with no
        trace. Raises :class:`VersionConflict` when stale.
        """
        ...

    async def get(self, workflow: str, version: int) -> WorkflowVersion | None: ...

    async def latest(self, workflow: str) -> WorkflowVersion | None: ...

    async def history(
        self, workflow: str, *, limit: int = 50
    ) -> list[WorkflowVersion]:
        """Newest first."""
        ...

    async def resolve(self, workflow: str, digest: str) -> WorkflowVersion | None:
        """The version matching *digest*, by **either** hash it carries.

        One method rather than two because a caller has one hash and wants the
        version: an author holds a ``content_hash`` from the source they are
        about to commit, and a run holds a ``code_hash``. Requiring the caller
        to know which kind they have pushes an internal distinction outward for
        no benefit.
        """
        ...

    async def source_of(self, version: WorkflowVersion) -> str:
        """The source text, fetched from wherever it was stored."""
        ...


class StoreBackedVersionStore:
    """The default :class:`VersionStore`, over any ``ExecutionStore``.

    Records go in the store's cache namespace, which every backend implements;
    content goes to blobs when a :class:`BlobService` is available and inline
    otherwise. That fallback is deliberate: a developer running ``MemoryStore``
    with no blob service should not have to configure object storage to use
    versions, and a 2KB workflow inline costs nothing.
    """

    #: How long a commit may wait for the per-workflow sequence lock, and how
    #: long it holds it. The TTL bounds a crashed committer; the wait bounds a
    #: contended one.
    LOCK_TTL_SECONDS = 30.0
    LOCK_WAIT_SECONDS = 10.0

    #: Cache keys. Namespaced so a store shared with the cache cannot collide.
    _INDEX = "loom:versions:index:{workflow}"
    _ENTRY = "loom:versions:entry:{workflow}:{version}"

    def __init__(self, store: Any, blobs: Any | None = None) -> None:
        self._store = store
        self._blobs = blobs

    # -- writes -------------------------------------------------------------

    async def commit(
        self, version: WorkflowVersion, *, expected_latest: int | None = None
    ) -> WorkflowVersion:
        if not version.workflow:
            raise ValueError("a version must name its workflow")
        # Serialised per workflow. Assigning a number is read-modify-write over
        # the index, and eight concurrent commits each read the same value,
        # each computed the same next number, and seven of them were lost — the
        # conformance test found it on every store, including Memory.
        #
        # LockProvider rather than a store-specific atomic: it is in the same
        # protocol matrix, so this works identically on all four backends
        # instead of needing one implementation per store.
        async with self._sequence_lock(version.workflow):
            return await self._commit_locked(version, expected_latest)

    async def _commit_locked(
        self, version: WorkflowVersion, expected_latest: int | None
    ) -> WorkflowVersion:
        source = version.source
        if not source and not version.source_ref:
            raise ValueError(
                f"{version.workflow}: a version needs source, or a source_ref "
                "pointing at previously stored content"
            )

        digest = version.content_hash or (content_hash_of(source) if source else "")
        existing = await self.resolve(version.workflow, digest) if digest else None
        if existing is not None:
            # Identical content is the same version. Returning it rather than
            # appending keeps a retried publish from inflating the chain.
            return existing

        numbers = await self._numbers(version.workflow)
        current = max(numbers) if numbers else None
        if expected_latest is not None and expected_latest != (current or 0):
            raise VersionConflict(
                version.workflow, expected=expected_latest, actual=current or 0
            )

        source_ref = version.source_ref or await self._put_content(source)
        committed = version.model_copy(
            update={
                "version": (current or 0) + 1,
                "content_hash": digest,
                "source_ref": source_ref,
                "parent_version": current,
            }
        )
        await self._store.set(
            self._ENTRY.format(workflow=committed.workflow, version=committed.version),
            committed.model_dump(mode="json"),
            0,
        )
        await self._store.set(
            self._INDEX.format(workflow=committed.workflow),
            sorted({*numbers, committed.version}),
            0,
        )
        return committed

    @asynccontextmanager
    async def _sequence_lock(self, workflow: str) -> AsyncIterator[None]:
        """Hold the per-workflow commit lock, or say why not.

        A failure to acquire raises rather than proceeding: continuing without
        the lock is exactly the race the lock exists to prevent, and a commit
        that silently loses somebody's work is the worst available outcome.
        """
        key = f"loom:versions:seq:{workflow}"
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + self.LOCK_WAIT_SECONDS
        while True:
            if await self._store.acquire(key, owner, self.LOCK_TTL_SECONDS):
                break
            if time.monotonic() >= deadline:
                raise VersionConflict(
                    workflow, expected=None, actual=None
                ) from TimeoutError(
                    f"could not take the version lock for {workflow!r} within "
                    f"{self.LOCK_WAIT_SECONDS}s"
                )
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            await self._store.release(key, owner)

    # -- reads --------------------------------------------------------------

    async def get(self, workflow: str, version: int) -> WorkflowVersion | None:
        raw = await self._store.get(self._ENTRY.format(workflow=workflow, version=version))
        return WorkflowVersion.model_validate(raw) if raw else None

    async def latest(self, workflow: str) -> WorkflowVersion | None:
        numbers = await self._numbers(workflow)
        return await self.get(workflow, max(numbers)) if numbers else None

    async def history(self, workflow: str, *, limit: int = 50) -> list[WorkflowVersion]:
        found = []
        for number in sorted(await self._numbers(workflow), reverse=True)[:limit]:
            version = await self.get(workflow, number)
            if version is not None:
                found.append(version)
        return found

    async def resolve(self, workflow: str, digest: str) -> WorkflowVersion | None:
        if not digest:
            return None
        for version in await self.history(workflow, limit=1000):
            if digest in {version.content_hash, version.code_hash}:
                return version
        return None

    async def source_of(self, version: WorkflowVersion) -> str:
        if version.source:
            return version.source
        if not version.source_ref:
            raise MissingVersionContent(
                "", hint=f"{version.key} records no source_ref"
            )
        return await self._get_content(version.source_ref)

    # -- content ------------------------------------------------------------

    #: Where content goes when there is no blob service. A *separate* key, not
    #: a field on the record: a first cut put the source in ``source_ref`` and a
    #: 200KB workflow made the record 200KB, which is precisely the coupling
    #: offloading exists to remove. The conformance test that asserts records
    #: stay small caught it, and it would have been invisible until somebody ran
    #: a large workflow on a document store.
    _CONTENT = "loom:versions:content:{digest}"

    async def _put_content(self, source: str) -> str:
        payload = source.encode("utf-8")
        if self._blobs is not None:
            ref: str = await self._blobs.store(payload, "text/x-python")
            return ref
        digest = content_hash_of(source)
        await self._store.set(self._CONTENT.format(digest=digest), source, 0)
        return f"store:{digest}"

    async def _get_content(self, ref: str) -> str:
        if ref.startswith("store:"):
            found = await self._store.get(
                self._CONTENT.format(digest=ref.removeprefix("store:"))
            )
            if found is None:
                raise MissingVersionContent(ref)
            return str(found)
        if self._blobs is None:
            raise MissingVersionContent(
                ref, hint="this Runtime has no blob service — pass Runtime(blobs=...)"
            )
        content: bytes = await self._blobs.load(ref)
        return content.decode("utf-8")

    async def _numbers(self, workflow: str) -> list[int]:
        raw = await self._store.get(self._INDEX.format(workflow=workflow))
        return [int(n) for n in raw] if raw else []
