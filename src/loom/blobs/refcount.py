"""Which runs still need a blob, so retention can tell if it is safe to delete.

Blobs are content-addressed, so two runs that produce byte-identical output
share one — and for a deterministic step that is the *normal* case, not a
coincidence. Retention deleted by scanning one run's journal and removing every
ref it named, which meant compacting run A destroyed a payload run B was still
going to replay. The failure surfaced as a bare 64-character hex string from
inside ``BlobService.load``, and it was the one data-loss path that reached a
*correctly configured* deployment.

The obvious answer is a refcount table, and it was avoided for the usual reason:
it would need a schema change on four backends. It does not. Every store already
implements ``CacheStore`` — the artifact index, agent sessions and the event log
all live there — so the index is a key per blob holding the run ids that
reference it, written at the one point every journal flush passes through.

Two deliberate limits, both stated because a guarantee nobody can characterise
is not one:

* **It costs nothing for runs with no offloaded payloads.** Entries are tested
  for the blob marker in memory first, so a store round trip only happens when a
  payload was actually offloaded — which is the rare case the threshold exists
  to make rare.
* **It is authoritative only for runs written after it shipped.** A journal
  recorded earlier has no index entry, and "no entry" is indistinguishable from
  "no referents". Refusing to delete on that basis would leak every pre-existing
  blob forever, so an unknown ref falls back to the previous behaviour — the
  replay-clone check — rather than to either extreme.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from loom.runtime.journal import JournalEntry

#: Cache key prefix. Namespaced so a compaction sweep can recognise its own keys.
BLOB_REF_PREFIX = "blobref:"

__all__ = ["BLOB_REF_PREFIX", "record_refs", "referents", "refs_in", "release_ref"]


def refs_in(entries: list[JournalEntry]) -> set[str]:
    """Every blob reference these entries name, in memory and without I/O."""
    from loom.blobs.retention import _blob_ref

    found: set[str] = set()
    for entry in entries:
        ref = _blob_ref(entry.output)
        if ref is not None:
            found.add(ref)
    return found


async def record_refs(store: Any, run_id: str, entries: list[JournalEntry]) -> None:
    """Note that *run_id* references the blobs named in *entries*.

    Best-effort by construction: a store that cannot index is a store whose
    blobs are collected slightly too eagerly, which is the behaviour that
    shipped before this existed. Failing a journal flush over bookkeeping would
    trade a rare deletion bug for a common durability one.
    """
    refs = refs_in(entries)
    if not refs:
        return
    for ref in refs:
        with suppress(Exception):
            key = BLOB_REF_PREFIX + ref
            holders = list(await store.get(key) or [])
            if run_id not in holders:
                holders.append(run_id)
                # ttl=0 means never expires — a reference outlives any cache
                # horizon, and an entry that quietly aged out would read as
                # "nobody needs this" and licence the delete.
                await store.set(key, holders, 0)


async def referents(store: Any, ref: str) -> list[str] | None:
    """Runs known to reference *ref*, or ``None`` when nothing is recorded.

    ``None`` and ``[]`` are different answers and the caller must treat them
    differently: the first means "written before the index existed", the second
    means "genuinely nobody".
    """
    try:
        found = await store.get(BLOB_REF_PREFIX + ref)
    except Exception:
        return None
    return None if found is None else list(found)


async def release_ref(store: Any, ref: str, run_id: str) -> bool | None:
    """Drop *run_id* from *ref*'s referents. ``True`` when none remain.

    ``None`` when the ref predates the index, so the caller can fall back rather
    than read an absent record as permission.
    """
    holders = await referents(store, ref)
    if holders is None:
        return None
    remaining = [held for held in holders if held != run_id]
    key = BLOB_REF_PREFIX + ref
    with suppress(Exception):
        if remaining:
            await store.set(key, remaining, 0)
        else:
            await store.delete(key)
    return not remaining
