"""MongoDB store — implements ExecutionStore, CacheStore, LockProvider, TriggerStore.

Requires ``motor`` (async MongoDB driver): ``pip install loomflow[mongo]``

Usage::

    from loom.stores.mongo import MongoStore

    store = MongoStore("mongodb://localhost:27017", database="workflows")
    await store.ensure_indexes()
    rt = Runtime(store=store)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from loom.core.models import (
    Event,
    ExecutionRecord,
    ExecutionStatus,
    TriggerRecord,
)
from loom.runtime.journal import JournalEntry, path_order

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
        self._db = self._client[database]

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
        """Create all indexes. Call once at startup."""
        await self.check_for_legacy_database()
        ex = self._db.executions
        await ex.create_index("workflow")
        await ex.create_index([("status", 1), ("wake_at", 1)])
        await ex.create_index([("status", 1), ("awaiting_event", 1)])
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

        jn = self._db.journal
        await jn.create_index(
            [("run_id", 1), ("path", 1)], unique=True
        )
        await jn.create_index([("run_id", 1), ("sort_key", 1)])

        ev = self._db.events
        await ev.create_index([("run_id", 1), ("name", 1)])

        tr = self._db.triggers
        await tr.create_index("workflow")
        await tr.create_index(
            [("enabled", 1), ("next_fire_at", 1)]
        )

        ca = self._db.cache
        await ca.create_index(
            "expires_at", expireAfterSeconds=0
        )

    # ------------------------------------------------------------------
    # ExecutionStore
    # ------------------------------------------------------------------

    async def create_execution(self, record: ExecutionRecord) -> None:
        doc = _exec_to_doc(record)
        await self._db.executions.replace_one(
            {"_id": record.run_id}, doc, upsert=True
        )

    async def get_execution(
        self, run_id: str
    ) -> ExecutionRecord | None:
        doc = await self._db.executions.find_one({"_id": run_id})
        return _doc_to_exec(doc) if doc else None

    async def update_execution(self, record: ExecutionRecord) -> None:
        doc = _exec_to_doc(record)
        await self._db.executions.replace_one(
            {"_id": record.run_id}, doc, upsert=True
        )

    async def delete_execution(self, run_id: str) -> None:
        await self._db.journal.delete_many({"run_id": run_id})
        await self._db.executions.delete_one({"_id": run_id})

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
            self._db.executions.find(query)
            .sort("_id", -1)
            .skip(offset)
            .limit(limit)
        )
        return [_doc_to_exec(doc) async for doc in cursor]

    async def find_by_idempotency_key(
        self, key: str
    ) -> ExecutionRecord | None:
        doc = await self._db.executions.find_one(
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
            ops.append(
                UpdateOne(
                    {"run_id": run_id, "path": entry.path},
                    {"$set": doc},
                    upsert=True,
                )
            )
        if ops:
            await self._db.journal.bulk_write(ops, ordered=False)

    async def load_journal(
        self, run_id: str
    ) -> list[JournalEntry]:
        cursor = self._db.journal.find(
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
        await self._db.journal.delete_many({
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
        await self._db.events.insert_one(doc)

    async def claim_event_delivery(
        self, key: str, *, ttl_seconds: float = 604800.0
    ) -> bool:
        from pymongo.errors import DuplicateKeyError

        now = time.time()
        await self._db.event_deliveries.delete_many({"expires_at": {"$lte": now}})
        try:
            # insert_one against _id: the unique index *is* the claim. An upsert
            # would succeed for both callers and defeat the whole point.
            await self._db.event_deliveries.insert_one(
                {"_id": key, "expires_at": now + ttl_seconds}
            )
        except DuplicateKeyError:
            return False
        return True

    async def take_event(
        self, run_id: str, name: str
    ) -> Event | None:
        doc = await self._db.events.find_one_and_delete(
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
        cursor = self._db.executions.find(
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
            self._db.executions.find(
                {
                    "status": ExecutionStatus.SUSPENDED.value,
                    "wake_at": {"$lte": now.isoformat()},
                },
                {"_id": 1},
            )
            .sort("wake_at", 1)
            .limit(limit)
        )
        return [doc["_id"] async for doc in cursor]

    # ------------------------------------------------------------------
    # CacheStore
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        doc = await self._db.cache.find_one({"_id": key})
        if doc is None:
            return None
        expires_at = doc.get("expires_at")
        if expires_at is not None and expires_at < time.time():
            await self._db.cache.delete_one({"_id": key})
            return None
        return doc.get("value")

    async def set(
        self, key: str, value: Any, ttl_seconds: float
    ) -> None:
        # A ttl of zero or less means "no expiry", the rule every other store
        # follows. Reading it as "expires immediately" makes set(key, value, 0)
        # a silent no-op, which is never what a caller means.
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else None
        await self._db.cache.replace_one(
            {"_id": key},
            {"_id": key, "expires_at": expires_at, "value": value},
            upsert=True,
        )

    async def delete(self, key: str) -> None:
        await self._db.cache.delete_one({"_id": key})

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
            result = await self._db.locks.find_one_and_update(
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
        result = await self._db.locks.update_one(
            {"_id": key, "owner": owner},
            {"$set": {"expires_at": time.time() + ttl_seconds}},
        )
        return result.modified_count > 0

    async def release(self, key: str, owner: str) -> None:
        await self._db.locks.delete_one(
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
        await self._db.triggers.replace_one(
            {"_id": trigger.trigger_id}, doc, upsert=True
        )

    async def get_trigger(
        self, trigger_id: str
    ) -> TriggerRecord | None:
        doc = await self._db.triggers.find_one(
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
        cursor = self._db.triggers.find(query)
        return [
            TriggerRecord.model_validate(doc["data"])
            async for doc in cursor
        ]

    async def due_triggers(
        self, now: datetime, *, limit: int = 50
    ) -> list[TriggerRecord]:
        cursor = (
            self._db.triggers.find({
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

    async def update_after_fire(
        self,
        trigger_id: str,
        last_fire: datetime,
        next_fire: datetime | None,
    ) -> None:
        await self._db.triggers.update_one(
            {"_id": trigger_id},
            {
                "$set": {
                    "next_fire_at": next_fire,
                    "data.last_fire_at": (
                        last_fire.isoformat() if last_fire else None
                    ),
                    "data.next_fire_at": (
                        next_fire.isoformat() if next_fire else None
                    ),
                },
                "$inc": {"data.run_count": 1},
            },
        )

    async def delete_trigger(self, trigger_id: str) -> None:
        await self._db.triggers.delete_one({"_id": trigger_id})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _exec_to_doc(record: ExecutionRecord) -> dict[str, Any]:
    return {
        "_id": record.run_id,
        "workflow": record.workflow,
        "status": record.status.value,
        "wake_at": (
            record.wake_at.isoformat() if record.wake_at else None
        ),
        "awaiting_event": record.awaiting_event,
        "idempotency_key": record.idempotency_key,
        "created_at": (
            record.created_at.isoformat()
            if record.created_at
            else None
        ),
        "data": record.model_dump(mode="json"),
    }


def _doc_to_exec(doc: dict[str, Any]) -> ExecutionRecord:
    return ExecutionRecord.model_validate(doc["data"])
