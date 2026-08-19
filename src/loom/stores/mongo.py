"""MongoDB store — implements ExecutionStore, CacheStore, LockProvider, TriggerStore.

Requires ``motor`` (async MongoDB driver): ``pip install loomsdk[mongo]``

Usage::

    from loom.stores.mongo import MongoStore

    store = MongoStore("mongodb://localhost:27017", database="workflows")
    await store.ensure_indexes()
    rt = Runtime(store=store)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from loom.core.exceptions import ConcurrentUpdateError, ConfigurationError
from loom.core.models import (
    Event,
    ExecutionRecord,
    ExecutionStatus,
    TriggerRecord,
)
from loom.runtime.journal import JournalEntry, path_order
from loom.stores.base import utc_iso

logger = logging.getLogger(__name__)


class MongoStore:
    """MongoDB-backed store using Motor async driver.

    Implements ``ExecutionStore``, ``CacheStore``, ``LockProvider``,
    and ``TriggerStore``.

    Parameters
    ----------
    uri:
        MongoDB connection string.
    database:
        Database name (default: ``"loom"``).

        **Renamed from ``workflow_builder`` in 0.12.** A default database name
        lives in *your* storage, not in this code, so changing it silently would
        point an existing deployment at an empty database — which reads as "no
        runs" rather than as an error. :meth:`ensure_indexes` therefore checks
        for the old database and says so. To keep the old one:
        ``MongoStore(uri, database="workflow_builder")``.
    """

    def __init__(
        self,
        uri: str = "mongodb://localhost:27017",
        database: str = "loom",
    ) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        self._client = AsyncIOMotorClient(uri)
        self._database = self._client[database]
        self._indexed = False
        self._index_lock = asyncio.Lock()

    @property
    def _db(self) -> Any:
        """The raw database handle, with no index guarantee.

        Kept for callers that already hold a store they have indexed. Store
        methods go through :meth:`_ready` instead.
        """
        return self._database

    async def _ready(self) -> Any:
        """The database handle, with indexes built exactly once first.

        Indexes cannot be created from ``__init__`` because building them is
        async, and :func:`loom.stores.from_url` is not — so
        ``LOOM_STORE=mongodb://…`` with ``Runtime.from_env()``, the documented
        deployment path, produced a store whose ``executions`` collection had
        only ``_id_``. That silently removes the **unique** index on
        ``idempotency_key``, and with it the exactly-once guarantee behind cron
        dedupe, event dedupe and queue ingress: two runs for one occurrence,
        both reported as created. It also leaves every ``load_journal`` a
        collection scan.

        Postgres has the same gap and fails loudly — its pool is ``None`` and
        the first call raises. Silent and degraded is the worse of the two, so
        Mongo closes it instead of matching it.

        ``create_index`` is idempotent server-side, so a store that was already
        indexed explicitly pays one guarded flag check per call thereafter.
        """
        if not self._indexed:
            async with self._index_lock:
                if not self._indexed:
                    await self._build_indexes()
                    self._indexed = True
        return self._database

    #: The name this store used before the package was renamed to ``loom``.
    LEGACY_DATABASE = "workflow_builder"

    async def check_for_legacy_database(self) -> str:
        """Warn when runs exist under the pre-rename database name.

        Returns the warning, or ``""``. Reported rather than migrated: moving
        somebody's data is not a thing a constructor should do quietly, and an
        empty database that used to have runs in it is indistinguishable from a
        fresh install unless something says otherwise.
        """
        if self._db.name == self.LEGACY_DATABASE:
            return ""
        try:
            if await self._db.executions.estimated_document_count():
                return ""
            legacy = self._client[self.LEGACY_DATABASE]
            found = await legacy.executions.estimated_document_count()
        except Exception:
            # A diagnostic must never break startup.
            return ""
        if not found:
            return ""
        message = (
            f"database {self._db.name!r} is empty, but {found} runs exist in "
            f"{self.LEGACY_DATABASE!r} — the name this store used before the "
            "package was renamed to loom. Pass "
            f'MongoStore(uri, database="{self.LEGACY_DATABASE}") to keep using '
            "them, or rename the database."
        )
        logger.warning(message)
        return message

    async def ensure_indexes(self) -> None:
        """Create all indexes.

        Still worth calling at startup so the cost is paid before the first
        request rather than during it, and so an unreachable server fails there
        rather than mid-run. No longer *required* — :meth:`_ready` builds them
        on first use for the callers that cannot await a constructor.
        """
        await self._build_indexes()
        self._indexed = True

    async def _build_indexes(self) -> None:
        # Reads self._database directly, never self._ready(): _ready() is what
        # calls this, and check_for_legacy_database below would recurse through
        # it otherwise.
        await self.check_for_legacy_database()
        ex = self._database.executions
        await ex.create_index("workflow")
        await ex.create_index([("status", 1), ("wake_at", 1)])
        await ex.create_index([("status", 1), ("awaiting_event", 1)])
        await self._unique_idempotency_index(ex)

        jn = self._db.journal
        await jn.create_index(
            [("run_id", 1), ("path", 1)], unique=True
        )
        await jn.create_index([("run_id", 1), ("sort_key", 1)])

        ev = self._db.events
        await ev.create_index([("run_id", 1), ("name", 1)])

        await ex.create_index([("status", 1), ("lease_expires_at", 1)])
        await ex.create_index([("status", 1), ("finished_at", 1)])

        tr = self._db.triggers
        await tr.create_index("workflow")
        await tr.create_index(
            [("enabled", 1), ("next_fire_at", 1)]
        )

        ca = self._db.cache
        await ca.create_index(
            "expires_at", expireAfterSeconds=0
        )

    async def _unique_idempotency_index(self, ex: Any) -> None:
        """Build the unique index, and explain it when the data already violates it.

        A database written while the index was missing can already hold two runs
        under one key, and Mongo then refuses to build the index at all. Raw,
        that surfaces as a ``DuplicateKeyError`` from inside whatever call
        happened to be first — which reads as "this write is a duplicate" when
        what it means is "dedupe was never enforced here, and these are the
        leftovers".

        Raising rather than skipping is the point: continuing without the index
        is the silent state this method exists to end. The message names the key
        so the duplicates can be found.
        """
        from pymongo.errors import DuplicateKeyError, OperationFailure

        try:
            await self._create_idempotency_index(ex)
        except (DuplicateKeyError, OperationFailure) as exc:
            raise ConfigurationError(
                f"cannot build the unique index on executions.idempotency_key in "
                f"{self._database.name!r}: the collection already holds more than one "
                f"run under the same key. That means idempotency was not enforced "
                f"while this database was used without indexes, so a cron occurrence "
                f"or a redelivered event may have started duplicate runs. Resolve the "
                f"duplicates and restart — "
                f"db.executions.aggregate([{{$match:{{idempotency_key:{{$type:'string'}}}}}},"
                f"{{$group:{{_id:'$idempotency_key',n:{{$sum:1}},runs:{{$push:'$_id'}}}}}},"
                f"{{$match:{{n:{{$gt:1}}}}}}]) lists them. Original error: {exc}"
            ) from exc

    async def _create_idempotency_index(self, ex: Any) -> None:
        await ex.create_index(
            "idempotency_key",
            unique=True,
            # partialFilterExpression, not sparse. `sparse` skips documents that
            # *lack* the field; a document carrying `idempotency_key: null`
            # still indexes, so the second keyless run collided with the first
            # and MongoStore could hold exactly one. Found by the conformance
            # matrix the first time Mongo was actually driven through it.
            partialFilterExpression={"idempotency_key": {"$type": "string"}},
        )

    # ------------------------------------------------------------------
    # ExecutionStore
    # ------------------------------------------------------------------

    async def create_execution(self, record: ExecutionRecord) -> None:
        doc = _exec_to_doc(record)
        await (await self._ready()).executions.replace_one(
            {"_id": record.run_id}, doc, upsert=True
        )

    async def get_execution(
        self, run_id: str
    ) -> ExecutionRecord | None:
        doc = await (await self._ready()).executions.find_one({"_id": run_id})
        return _doc_to_exec(doc) if doc else None

    async def update_execution(
        self, record: ExecutionRecord, *, expected_status: ExecutionStatus | None = None
    ) -> None:
        doc = _exec_to_doc(record)
        if expected_status is None:
            await (await self._ready()).executions.replace_one(
                {"_id": record.run_id}, doc, upsert=True
            )
            return
        # find_one_and_update with a status precondition is what makes two
        # instances racing to resume the same SUSPENDED run resolve to
        # exactly one winner: Mongo evaluates the filter and applies the
        # replacement atomically, so the loser's filter simply matches
        # nothing rather than clobbering the winner's in-flight journal.
        fields = {k: v for k, v in doc.items() if k != "_id"}
        result = await (await self._ready()).executions.find_one_and_update(
            {"_id": record.run_id, "status": expected_status.value},
            {"$set": fields},
        )
        if result is None:
            current = await (await self._ready()).executions.find_one({"_id": record.run_id})
            actual = current.get("status") if current else None
            raise ConcurrentUpdateError(
                record.run_id, expected=expected_status.value, actual=actual,
            )

    async def delete_execution(self, run_id: str) -> None:
        await (await self._ready()).journal.delete_many({"run_id": run_id})
        await (await self._ready()).executions.delete_one({"_id": run_id})

    async def list_executions(
        self,
        *,
        workflow: str | None = None,
        status: ExecutionStatus | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExecutionRecord]:
        query: dict[str, Any] = {}
        if workflow:
            query["workflow"] = workflow
        if status:
            query["status"] = status.value
        if tags:
            query["data.tags"] = {"$all": tags}
        if metadata:
            for k, v in metadata.items():
                query[f"data.metadata.{k}"] = v

        cursor = (
            (await self._ready()).executions.find(query)
            .sort("_id", -1)
            .skip(offset)
            .limit(limit)
        )
        return [_doc_to_exec(doc) async for doc in cursor]

    async def find_by_idempotency_key(
        self, key: str
    ) -> ExecutionRecord | None:
        doc = await (await self._ready()).executions.find_one(
            {"idempotency_key": key}
        )
        return _doc_to_exec(doc) if doc else None

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    async def save_journal(
        self, run_id: str, entries: list[JournalEntry]
    ) -> None:
        from pymongo import UpdateOne

        ops = []
        for entry in entries:
            sort_key = ".".join(
                str(s).zfill(9) for s in path_order(entry.path)
            )
            doc = {
                "run_id": run_id,
                "path": entry.path,
                "sort_key": sort_key,
                "data": entry.model_dump(mode="json"),
            }
            _refuse_oversized(doc, run_id, entry.path)
            ops.append(
                UpdateOne(
                    {"run_id": run_id, "path": entry.path},
                    {"$set": doc},
                    upsert=True,
                )
            )
        if ops:
            await (await self._ready()).journal.bulk_write(ops, ordered=False)

    async def load_journal(
        self, run_id: str
    ) -> list[JournalEntry]:
        cursor = (await self._ready()).journal.find(
            {"run_id": run_id}
        ).sort("sort_key", 1)
        return [
            JournalEntry.model_validate(doc["data"])
            async for doc in cursor
        ]

    async def truncate_journal(
        self, run_id: str, from_path: str
    ) -> None:
        from_key = ".".join(
            str(s).zfill(9) for s in path_order(from_path)
        )
        await (await self._ready()).journal.delete_many({
            "run_id": run_id,
            "sort_key": {"$gte": from_key},
        })

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def enqueue_event(self, event: Event) -> None:
        doc = {
            "run_id": event.run_id or "",
            "name": event.name,
            "data": event.model_dump(mode="json"),
        }
        await (await self._ready()).events.insert_one(doc)

    async def claim_event_delivery(
        self, key: str, *, ttl_seconds: float = 604800.0
    ) -> bool:
        from pymongo.errors import DuplicateKeyError

        now = time.time()
        await (await self._ready()).event_deliveries.delete_many({"expires_at": {"$lte": now}})
        try:
            # insert_one against _id: the unique index *is* the claim. An upsert
            # would succeed for both callers and defeat the whole point.
            await (await self._ready()).event_deliveries.insert_one(
                {"_id": key, "expires_at": now + ttl_seconds}
            )
        except DuplicateKeyError:
            return False
        return True

    async def take_event(
        self, run_id: str, name: str
    ) -> Event | None:
        doc = await (await self._ready()).events.find_one_and_delete(
            {
                "name": name,
                "run_id": {"$in": [run_id, ""]},
            },
            sort=[("run_id", -1), ("_id", 1)],
        )
        if doc is None:
            return None
        return Event.model_validate(doc["data"])

    async def runs_awaiting_event(
        self, name: str
    ) -> list[str]:
        cursor = (await self._ready()).executions.find(
            {
                "status": ExecutionStatus.SUSPENDED.value,
                "awaiting_event": name,
            },
            {"_id": 1},
        )
        return [doc["_id"] async for doc in cursor]

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    async def due_runs(
        self, now: datetime, *, limit: int = 100
    ) -> list[str]:
        cursor = (
            (await self._ready()).executions.find(
                {
                    "status": ExecutionStatus.SUSPENDED.value,
                    "wake_at": {"$lte": utc_iso(now)},
                },
                {"_id": 1},
            )
            .sort("wake_at", 1)
            .limit(limit)
        )
        return [doc["_id"] async for doc in cursor]

    async def due_leases(
        self,
        before: datetime,
        statuses: Sequence[ExecutionStatus],
        *,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        if not statuses:
            return []
        cursor = (
            (await self._ready()).executions.find(
                {
                    "status": {"$in": [s.value for s in statuses]},
                    "lease_expires_at": {"$ne": None, "$lte": utc_iso(before)},
                }
            )
            .sort("lease_expires_at", 1)
            .limit(limit)
        )
        return [_doc_to_exec(doc) async for doc in cursor]

    async def terminal_before(
        self,
        cutoff: datetime,
        statuses: Sequence[ExecutionStatus],
        *,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        if not statuses:
            return []
        cursor = (
            (await self._ready()).executions.find(
                {
                    "status": {"$in": [s.value for s in statuses]},
                    "finished_at": {"$ne": None, "$lt": utc_iso(cutoff)},
                }
            )
            .sort("finished_at", 1)
            .limit(limit)
        )
        return [_doc_to_exec(doc) async for doc in cursor]

    # ------------------------------------------------------------------
    # CacheStore
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        doc = await (await self._ready()).cache.find_one({"_id": key})
        if doc is None:
            return None
        expires_at = doc.get("expires_at")
        if expires_at is not None and expires_at < time.time():
            await (await self._ready()).cache.delete_one({"_id": key})
            return None
        return doc.get("value")

    async def set(
        self, key: str, value: Any, ttl_seconds: float
    ) -> None:
        # A ttl of zero or less means "no expiry", the rule every other store
        # follows. Reading it as "expires immediately" makes set(key, value, 0)
        # a silent no-op, which is never what a caller means.
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else None
        await (await self._ready()).cache.replace_one(
            {"_id": key},
            {"_id": key, "expires_at": expires_at, "value": value},
            upsert=True,
        )

    async def delete(self, key: str) -> None:
        await (await self._ready()).cache.delete_one({"_id": key})

    # ------------------------------------------------------------------
    # LockProvider
    # ------------------------------------------------------------------

    async def acquire(
        self, key: str, owner: str, ttl_seconds: float
    ) -> bool:
        from pymongo.errors import DuplicateKeyError

        now = time.time()
        held = {"owner": owner, "expires_at": now + ttl_seconds}
        try:
            result = await (await self._ready()).locks.find_one_and_update(
                {
                    "_id": key,
                    "$or": [{"owner": owner}, {"expires_at": {"$lt": now}}],
                },
                {"$set": held},
                upsert=True,
                return_document=True,
            )
        except DuplicateKeyError:
            # Somebody else holds it and it has not expired. The upsert races
            # the _id index and Mongo reports the collision — which is the
            # correct outcome expressed as an exception. Losing a contended
            # lock is `False`, not a crash: a caller that has to catch a driver
            # error to find out it did not get the lock will not.
            return False
        return result is not None and result.get("owner") == owner

    async def renew(
        self, key: str, owner: str, ttl_seconds: float
    ) -> bool:
        result = await (await self._ready()).locks.update_one(
            {"_id": key, "owner": owner},
            {"$set": {"expires_at": time.time() + ttl_seconds}},
        )
        renewed: bool = result.modified_count > 0
        return renewed

    async def release(self, key: str, owner: str) -> None:
        await (await self._ready()).locks.delete_one(
            {"_id": key, "owner": owner}
        )

    # ------------------------------------------------------------------
    # TriggerStore
    # ------------------------------------------------------------------

    async def save_trigger(self, trigger: TriggerRecord) -> None:
        doc = {
            "_id": trigger.trigger_id,
            "workflow": trigger.workflow,
            "kind": trigger.kind.value,
            "next_fire_at": trigger.next_fire_at,  # native BSON datetime
            "enabled": trigger.enabled,
            "data": trigger.model_dump(mode="json"),
        }
        await (await self._ready()).triggers.replace_one(
            {"_id": trigger.trigger_id}, doc, upsert=True
        )

    async def get_trigger(
        self, trigger_id: str
    ) -> TriggerRecord | None:
        doc = await (await self._ready()).triggers.find_one(
            {"_id": trigger_id}
        )
        if doc is None:
            return None
        return TriggerRecord.model_validate(doc["data"])

    async def list_triggers(
        self, *, workflow: str | None = None
    ) -> list[TriggerRecord]:
        query: dict[str, Any] = {}
        if workflow:
            query["workflow"] = workflow
        cursor = (await self._ready()).triggers.find(query)
        return [
            TriggerRecord.model_validate(doc["data"])
            async for doc in cursor
        ]

    async def due_triggers(
        self, now: datetime, *, limit: int = 50
    ) -> list[TriggerRecord]:
        cursor = (
            (await self._ready()).triggers.find({
                "enabled": True,
                "next_fire_at": {"$lte": now},  # native datetime compare
            })
            .sort("next_fire_at", 1)
            .limit(limit)
        )
        return [
            TriggerRecord.model_validate(doc["data"])
            async for doc in cursor
        ]

    async def claim_due_triggers(
        self,
        now: datetime,
        *,
        owner: str,
        lease_seconds: float = 60.0,
        limit: int = 50,
    ) -> list[TriggerRecord]:
        until = now + timedelta(seconds=lease_seconds)
        won: list[TriggerRecord] = []
        while len(won) < limit:
            # One document at a time: `find_one_and_update` is atomic per
            # document, and `update_many` would not tell us *which* documents
            # this caller won — which is the entire answer being asked for.
            doc = await (await self._ready()).triggers.find_one_and_update(
                {
                    "enabled": True,
                    "next_fire_at": {"$lte": now, "$ne": None},
                    "$or": [
                        {"data.claimed_until": None},
                        {"data.claimed_until": {"$lte": utc_iso(now)}},
                        {"data.claimed_until": {"$exists": False}},
                    ],
                },
                {
                    "$set": {
                        "data.claimed_by": owner,
                        "data.claimed_until": utc_iso(until),
                    }
                },
                sort=[("next_fire_at", 1)],
                return_document=True,
            )
            if doc is None:
                break
            won.append(TriggerRecord.model_validate(doc["data"]))
        return won

    async def update_after_fire(
        self,
        trigger_id: str,
        last_fire: datetime,
        next_fire: datetime | None,
    ) -> None:
        await (await self._ready()).triggers.update_one(
            {"_id": trigger_id},
            {
                "$set": {
                    "next_fire_at": next_fire,
                    # Release the claim; see MemoryStore for why at advance
                    # time rather than at expiry.
                    "data.claimed_by": "",
                    "data.claimed_until": None,
                    "data.last_fire_at": utc_iso(last_fire),
                    "data.next_fire_at": utc_iso(next_fire),
                },
                "$inc": {"data.run_count": 1},
            },
        )

    async def delete_trigger(self, trigger_id: str) -> None:
        await (await self._ready()).triggers.delete_one({"_id": trigger_id})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


#: MongoDB's hard per-document ceiling. Not a tunable — it is the server's.
BSON_DOCUMENT_LIMIT = 16 * 1024 * 1024


def _refuse_oversized(doc: dict[str, Any], run_id: str, where: str) -> None:
    """Refuse a document Mongo will reject, with something an operator can act on.

    The limit is the server's and is not negotiable, so the only question is
    what the caller learns when a payload exceeds it. Left alone, pymongo raises
    `DocumentTooLarge` from inside a `bulk_write` — no run id, no journal path,
    no mention of the fix — and only on this one backend, so the same workflow
    succeeds on Postgres and fails here.

    ``runtime/versions.py`` already reasons about this ceiling for workflow
    source; the lesson simply never reached the journal.
    """
    import bson

    try:
        size = len(bson.BSON.encode(doc))
    except Exception:
        # Encoding failed for some other reason — let the driver report it.
        return
    if size <= BSON_DOCUMENT_LIMIT:
        return
    raise ValueError(
        f"run {run_id!r} at journal path {where!r} produced a {size:,}-byte "
        f"document, over MongoDB's hard {BSON_DOCUMENT_LIMIT:,}-byte limit per "
        "document. Configure Runtime(blobs=BlobService(...)) so payloads this "
        "size are stored by content hash and referenced from the journal, or "
        "return an artifact reference from the step instead of the data."
    )


def _exec_to_doc(record: ExecutionRecord) -> dict[str, Any]:
    return {
        "_id": record.run_id,
        "workflow": record.workflow,
        "status": record.status.value,
        # utc_iso, not isoformat: due_runs compares this field as a string, so
        # a non-UTC offset sorts as a later string than the instant it names
        # and the run is never found. See loom.stores.base.utc_iso.
        "wake_at": utc_iso(record.wake_at),
        "awaiting_event": record.awaiting_event,
        "idempotency_key": record.idempotency_key,
        "created_at": utc_iso(record.created_at),
        # Lifted alongside the SQL stores' columns, and for the same reason:
        # retention and orphan reclamation filter on these, and a predicate
        # buried in `data` cannot use an index.
        "lease_expires_at": utc_iso(record.lease_expires_at),
        "finished_at": utc_iso(record.finished_at),
        "data": record.model_dump(mode="json"),
    }


def _doc_to_exec(doc: dict[str, Any]) -> ExecutionRecord:
    return ExecutionRecord.model_validate(doc["data"])
