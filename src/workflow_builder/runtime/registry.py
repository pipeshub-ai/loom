"""Persisted workflow catalog.

Workflow *definitions* live in code — the file on disk is the source of truth,
and a second copy in a database would only drift. But that leaves three things
awkward:

* A run records ``workflow`` and ``code_hash``; nothing says where that code was
  or what it looked like.
* The HTTP and MCP surfaces can only list what the serving process happened to
  import at startup.
* The coding agent generates a file and hands it back. Nothing records that it
  exists.

This module stores the *catalog entry* — name, version, code hash, description,
triggers, source path — without storing the code. Publishing is explicit
(``await runtime.publish(flow)``) rather than a side effect of ``@workflow``, so
importing a module never writes to a database.

An entry is a claim that a workflow existed, not a promise that this process can
run it: ``executable`` distinguishes the two.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from workflow_builder.runtime.workflow import WorkflowDefinition


class WorkflowRecord(BaseModel):
    """A published workflow's catalog entry."""

    name: str
    version: str = "1"
    description: str = ""
    code_hash: str = ""
    """Fingerprint of the workflow body, matching ``ExecutionRecord.code_hash``.
    This is what ties a historical run to the code that produced it."""
    source_file: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    triggers: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_by: str = ""
    """Node that published it, for tracing an unexpected entry to its source."""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """``name@version`` — how one entry is addressed."""
        return f"{self.name}@{self.version}"


@runtime_checkable
class WorkflowRegistry(Protocol):
    """Persistence for the workflow catalog."""

    async def put(self, record: WorkflowRecord) -> None:
        """Publish or update an entry, keyed by ``name@version``."""
        ...

    async def get(self, name: str, version: str | None = None) -> WorkflowRecord | None:
        """Fetch one entry. ``version=None`` means the most recently published."""
        ...

    async def list(self) -> list[WorkflowRecord]:
        """Every published entry."""
        ...

    async def delete(self, name: str, version: str | None = None) -> None:
        """Remove one version, or every version when ``version`` is None."""
        ...


class InMemoryWorkflowRegistry:
    """Process-local catalog. Fine for tests and single-process use."""

    def __init__(self) -> None:
        self._records: dict[str, WorkflowRecord] = {}

    async def put(self, record: WorkflowRecord) -> None:
        self._records[record.key] = record

    async def get(self, name: str, version: str | None = None) -> WorkflowRecord | None:
        if version is not None:
            return self._records.get(f"{name}@{version}")
        matches = [r for r in self._records.values() if r.name == name]
        return max(matches, key=lambda r: r.published_at) if matches else None

    async def list(self) -> list[WorkflowRecord]:
        return sorted(self._records.values(), key=lambda r: (r.name, r.version))

    async def delete(self, name: str, version: str | None = None) -> None:
        for key in [
            r.key
            for r in list(self._records.values())
            if r.name == name and (version is None or r.version == version)
        ]:
            self._records.pop(key, None)


class StoreBackedWorkflowRegistry:
    """Catalog persisted through any :class:`CacheStore`.

    Keeps a key index alongside the entries, because the cache protocol offers no
    way to enumerate keys — and a catalog you cannot list is not a catalog.
    """

    def __init__(self, store: Any, *, namespace: str = "workflow") -> None:
        self._store = store
        self._namespace = namespace

    def _key(self, entry_key: str) -> str:
        return f"{self._namespace}:def:{entry_key}"

    @property
    def _index_key(self) -> str:
        return f"{self._namespace}:index"

    async def _index(self) -> list[str]:
        return list(await self._store.get(self._index_key) or [])

    async def put(self, record: WorkflowRecord) -> None:
        # No TTL: a catalog that expires is worse than no catalog.
        await self._store.set(self._key(record.key), record.model_dump(mode="json"), 0)
        index = await self._index()
        if record.key not in index:
            await self._store.set(self._index_key, [*index, record.key], 0)

    async def get(self, name: str, version: str | None = None) -> WorkflowRecord | None:
        if version is not None:
            raw = await self._store.get(self._key(f"{name}@{version}"))
            return WorkflowRecord.model_validate(raw) if raw else None
        matches = [r for r in await self.list() if r.name == name]
        return max(matches, key=lambda r: r.published_at) if matches else None

    async def list(self) -> list[WorkflowRecord]:
        records: list[WorkflowRecord] = []
        for entry_key in await self._index():
            raw = await self._store.get(self._key(entry_key))
            if raw:
                records.append(WorkflowRecord.model_validate(raw))
        return sorted(records, key=lambda r: (r.name, r.version))

    async def delete(self, name: str, version: str | None = None) -> None:
        index = await self._index()
        doomed = [
            key
            for key in index
            if key.split("@")[0] == name
            and (version is None or key == f"{name}@{version}")
        ]
        for key in doomed:
            await self._store.delete(self._key(key))
        if doomed:
            await self._store.set(
                self._index_key, [k for k in index if k not in doomed], 0
            )


def record_for(
    definition: WorkflowDefinition[Any, Any, Any], *, published_by: str = ""
) -> WorkflowRecord:
    """Build a catalog entry from a live workflow definition."""
    from workflow_builder.core.serde import json_schema_for

    schema: dict[str, Any] = {}
    if definition.input_type is not None:
        try:
            schema = json_schema_for(definition.input_type)
        except Exception:
            schema = {}

    try:
        source_file = inspect.getfile(definition.fn)
    except (TypeError, OSError):
        source_file = ""

    return WorkflowRecord(
        name=definition.name,
        version=definition.version,
        description=definition.description,
        code_hash=definition.code_hash,
        source_file=source_file,
        input_schema=schema,
        triggers=[spec.name for spec in definition.triggers],
        tags=list(definition.tags),
        published_by=published_by,
    )
