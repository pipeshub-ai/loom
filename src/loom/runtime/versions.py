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

**Committed and live are two different questions.** ``latest`` is the newest
thing anybody committed; ``active`` is the one a host has said to serve.
:class:`VersionActivation` moves that pointer, so a rollback is
``activate("onboard", 3)`` rather than a sixth commit carrying version three's
code — which is what people do when there is no pointer, and it destroys the
only record of which version is actually live.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from loom.core.exceptions import WorkflowError

__all__ = [
    "MissingVersionContent",
    "Pins",
    "StoreBackedVersionStore",
    "UnknownVersion",
    "VersionActivation",
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


class UnknownVersion(WorkflowError):  # noqa: N818 - names the absence
    """Activation named a version that was never committed.

    Its own type rather than ``KeyError`` because a rollback is something a
    person does under pressure, from a number they read off a dashboard, and
    ``KeyError: 3`` tells them nothing about which workflow, what exists, or
    what to try instead. It carries all three.
    """

    def __init__(self, workflow: str, version: int, *, known: Sequence[int] = ()) -> None:
        self.workflow = workflow
        self.version = version
        self.known = list(known)
        existing = (
            ", ".join(str(n) for n in self.known)
            if self.known
            else "nothing has been committed for it"
        )
        super().__init__(
            f"{workflow} has no version {version} — committed: {existing}. "
            "activate() points at code that already exists; it never creates "
            "a version, so commit the source first."
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

    Activation lives in :class:`VersionActivation`, a companion protocol, for
    the reason ``IndexedScans`` is separate from ``ExecutionStore``: a
    ``runtime_checkable`` protocol is all-or-nothing, so adding a method here
    would make every host store that already satisfies this one stop being a
    version store at all. Callers probe with
    ``getattr(versions, "activate", None)``.
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

    async def latest(self, workflow: str) -> WorkflowVersion | None:
        """The highest-numbered version ever committed. **Not** what is live.

        Unchanged by activation, deliberately. ``latest`` answers "what is the
        newest thing anybody committed" and :meth:`VersionActivation.active`
        answers "which one is being served"; a store that answered both with
        this method would un-roll a rollback on the next publish, silently,
        because the next commit is always the new highest number.
        """
        ...

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


@runtime_checkable
class VersionActivation(Protocol):
    """Which committed version a host is actually serving.

    Separate from :class:`VersionStore` on purpose — see that class's note. A
    version store without this is not broken; it simply has no opinion about
    which of its versions is live, and callers that need one probe for the
    methods.
    """

    async def activate(self, workflow: str, version: int) -> None:
        """Point *workflow* at an already-committed *version*.

        **A pointer move, not a copy.** Rolling back to version 3 activates 3;
        it does not create a version 6 carrying 3's content. That distinction
        is the whole reason this exists: without a pointer the only way to roll
        back is to re-commit old source, which appends a duplicate of code that
        already exists *and* destroys the fact of which version is live — the
        chain now says "6 is newest" and nothing says "3 is what runs".

        Idempotent, so a retried rollback is not an error. Raises
        :class:`UnknownVersion` for a version that was never committed.

        This does not touch runs already executing. A run pins
        ``ExecutionRecord.code_hash`` when it starts and every lookup that
        matters — ``version_of``, the sandbox's source resolution — goes
        through that hash, never through this pointer. Activation decides what
        the *next* run gets.
        """
        ...

    async def active(self, workflow: str) -> WorkflowVersion | None:
        """The version being served, or ``None`` if nobody has said.

        ``None`` is a real answer, not a miss: read it as "no one has declared
        one", and it is what a chain committed before activation existed
        reports until someone calls :meth:`activate` once.
        """
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
    _ACTIVE = "loom:versions:active:{workflow}"

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
        if current is None:
            # The *first* version activates itself, and no later one does. A
            # workflow with exactly one version has no other candidate, and
            # leaving the pointer unset there would make every caller write
            # ``active() or latest()`` — the conflation the two methods exist
            # to keep apart, reintroduced in each caller instead of here.
            #
            # Every subsequent commit deliberately leaves the pointer where it
            # is. Advancing it would make a rollback last exactly until the
            # next publish, silently, which is the failure this pointer was
            # added to prevent; a new version not being served is visible in
            # one read, and a rollback that quietly un-rolled itself is not.
            # A host that wants publish-then-serve calls activate() after.
            await self._store.set(
                self._ACTIVE.format(workflow=committed.workflow), committed.version, 0
            )
        return committed

    async def activate(self, workflow: str, version: int) -> None:
        # Same lock as commit, and it must be the same one: activation reads
        # the entry to check it exists and then writes the pointer, so an
        # activation racing the commit of that very number could otherwise
        # read "absent", refuse, and report a version the committer has by then
        # written. Two locks would be two answers to one question.
        async with self._sequence_lock(workflow):
            if await self.get(workflow, version) is None:
                raise UnknownVersion(
                    workflow, version, known=await self._numbers(workflow)
                )
            await self._store.set(self._ACTIVE.format(workflow=workflow), version, 0)

    @asynccontextmanager
    async def _sequence_lock(self, workflow: str) -> AsyncIterator[None]:
        """Hold the per-workflow sequence lock, or say why not.

        A failure to acquire raises rather than proceeding: continuing without
        the lock is exactly the race the lock exists to prevent, and a commit
        that silently loses somebody's work is the worst available outcome.

        Commit and activation share it rather than taking one each, because
        they read and write overlapping state — the number a commit is about to
        assign is the one an activation is about to look up.
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

    async def active(self, workflow: str) -> WorkflowVersion | None:
        number = await self._store.get(self._ACTIVE.format(workflow=workflow))
        return await self.get(workflow, int(number)) if number is not None else None

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
