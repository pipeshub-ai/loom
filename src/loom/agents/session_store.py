"""An authoring job that outlives the process that started it.

``CodingSession`` held its transcript in a list and its budget in a dataclass,
and both died with the process. Ctrl+C at minute four of a six-minute
generation discarded the toolset schemas the model had fetched, the entity ids
it had resolved through real API calls, its plan, and every token paid for —
and there was no way to pick it up.

The fix is a **snapshot**, not a durable workflow. Re-entering an authoring job
is not the same problem as re-entering a workflow body: there is no journal to
serve, nothing has been made deterministic, and the thing worth keeping is
precisely the part that is *not* reproducible — a conversation with a model.
So this stores the conversation and what it cost, and resuming means handing
both back and asking again.

**On :class:`CacheStore`**, which every store implements and which the module
docstring already names as "the substrate for anything else keyed and durable —
agent sessions and the artifact index both live here". A separate table would
be a migration on four backends for something a keyed blob does.

Expiry is deliberate. A half-finished authoring job is worth days, not
forever: the code it was writing has moved on, and a resumed conversation about
a file that no longer looks like that is worse than starting again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from loom.agents.messages import Message
from loom.core.ids import new_id
from loom.core.models import Usage

__all__ = [
    "CodingSnapshot",
    "SessionStore",
    "StoreBackedSessionStore",
    "new_session_id",
]

#: How long a snapshot is kept. Long enough to come back after lunch, short
#: enough that a resumed conversation is still about the code on disk.
TTL_SECONDS = 7 * 24 * 60 * 60

#: Key prefix in the cache store. Namespaced so ``list`` cannot collide with a
#: step's memoized result.
PREFIX = "loom:authoring:"

#: Where the index of known ids lives. A list rather than a scan, because
#: ``CacheStore`` has no prefix query — it is a key-value port, and inventing
#: one would be a fifth method every backend has to implement.
INDEX_KEY = f"{PREFIX}index"


def new_session_id() -> str:
    """A sortable id, prefixed so it reads as what it is in a listing."""
    return new_id("auth")


@dataclass
class CodingSnapshot:
    """One authoring job, as of its last completed turn."""

    session_id: str
    #: What was asked for. Kept so ``loom author --resume`` can say what it is
    #: resuming, and so a snapshot is readable without the transcript.
    spec: str
    #: The conversation, oldest first. The expensive part: schemas fetched,
    #: entities resolved against real services, and the model's own plan.
    history: list[Message] = field(default_factory=list)
    #: What the job had spent when this was taken, so a resumed job continues
    #: against the same ceiling rather than being handed a fresh one.
    spent: Usage = field(default_factory=Usage)
    turns_used: int = 0
    #: The last code it produced, if it got that far. A resume that cannot
    #: reach the model at all can still hand this back.
    code: str = ""
    updated_at: float = 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "spec": self.spec,
            "history": [message.model_dump(mode="json") for message in self.history],
            "spent": self.spent.model_dump(mode="json"),
            "turns_used": self.turns_used,
            "code": self.code,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> CodingSnapshot:
        return cls(
            session_id=str(payload.get("session_id", "")),
            spec=str(payload.get("spec", "")),
            history=[
                Message.model_validate(entry)
                for entry in payload.get("history", [])
                if isinstance(entry, dict)
            ],
            spent=Usage.model_validate(payload.get("spent") or {}),
            turns_used=int(payload.get("turns_used", 0) or 0),
            code=str(payload.get("code", "")),
            updated_at=float(payload.get("updated_at", 0.0) or 0.0),
        )

    def describe(self) -> str:
        """One line, for a listing."""
        spec = self.spec.replace("\n", " ")[:52]
        return (
            f"{self.session_id}  {self.turns_used} turns  "
            f"{self.spent.total_tokens} tok  {spec}"
        )


@runtime_checkable
class SessionStore(Protocol):
    """Where authoring snapshots live.

    A port, so a host that keeps them in its own database implements three
    methods instead of being told where they go — the position every other
    storage seam in this repository takes.
    """

    async def save(self, snapshot: CodingSnapshot) -> None: ...

    async def load(self, session_id: str) -> CodingSnapshot | None: ...

    async def recent(self, limit: int = 20) -> list[CodingSnapshot]:
        """Snapshots that can still be resumed, newest first."""
        ...


@dataclass
class StoreBackedSessionStore:
    """The reference implementation, over any :class:`CacheStore`.

    Built only on ``get``/``set``/``delete``, which every LOOM store already
    has, so this works on memory, SQLite, Mongo and Postgres without any of
    them knowing what an authoring session is.
    """

    store: Any

    async def save(self, snapshot: CodingSnapshot) -> None:
        snapshot.updated_at = time.time()
        await self.store.set(
            PREFIX + snapshot.session_id, snapshot.as_json(), TTL_SECONDS
        )
        await self._index(snapshot.session_id)

    async def load(self, session_id: str) -> CodingSnapshot | None:
        payload = await self.store.get(PREFIX + session_id)
        if not isinstance(payload, dict):
            return None
        return CodingSnapshot.from_json(payload)

    async def recent(self, limit: int = 20) -> list[CodingSnapshot]:
        ids = await self.store.get(INDEX_KEY)
        found: list[CodingSnapshot] = []
        for session_id in reversed(ids if isinstance(ids, list) else []):
            snapshot = await self.load(str(session_id))
            # A missing one has expired. Skipped rather than reported: the
            # index is a convenience and its staleness is not a failure.
            if snapshot is not None:
                found.append(snapshot)
            if len(found) >= limit:
                break
        return found

    async def _index(self, session_id: str) -> None:
        """Remember the id, newest last, bounded.

        Read-modify-write on one key, which is racy between two concurrent
        authoring jobs — and harmless, because the index only decides what
        ``loom author --resume`` *lists*. A lost id is still resumable by
        name, which is how anyone who was interrupted has it.
        """
        ids = await self.store.get(INDEX_KEY)
        known = [str(i) for i in ids] if isinstance(ids, list) else []
        if session_id in known:
            known.remove(session_id)
        known.append(session_id)
        await self.store.set(INDEX_KEY, known[-200:], TTL_SECONDS)
