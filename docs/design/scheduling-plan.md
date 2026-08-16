# Scheduling — making cron a durability guarantee rather than a loop

<!-- docs-illustrative -->

**Status:** complete — P0–P4 landed, verified against all four stores. **Scope:** `triggers/cron.py`,
`triggers/specs.py`, `runtime/dispatcher.py`, `stores/*`, and one line of
`engine.py`.

---

## 0. Verification first

Every claim below was read out of the code before it was written down. The
three that matter:

**The dispatcher does not claim.** `TriggerStore.due_triggers()` is a plain
`SELECT ... WHERE next_fire_at <= now`. `tick()` then submits, then calls
`update_after_fire`. Read, act, write — with no exclusivity anywhere in the
sequence. Two dispatchers over one store both select the same row and both fire
it. `runtime.submit()` is called **without** an `idempotency_key`, though it
accepts one, so nothing collapses the duplicate downstream either.

The comment in `stores/postgres.py` saying *"Atomic update — no
read-modify-write race"* is true of that one `UPDATE` statement and says nothing
about the claim, which is where the race is.

**Leader election does not cover it.** `Runtime.start_scheduler(elector=…)`
gates `self.tick()` and `self.reclaim_orphans()`. `TriggerDispatcher` has its own
`start()` loop and no elector goes near it. So the mitigation Loom documents is
entirely the host's job — and `Schedule`'s docstring directs the reader to
`loom.worker.leader`, **a module that does not exist**; leader election lives in
`loom/runtime/leader.py`.

**Two declared options do nothing.** `Schedule.catch_up` and `Schedule.jitter`
are fields, are serialised into `describe()`, and are read by nothing.
`catch_up=True` still skips every fire missed during downtime. This is the same
defect class as a sandbox that accepts `max_memory_mb` and cannot apply it: a
policy accepted and quietly not applied is worse than one refused, because the
operator believes they have it.

`tests/test_dispatcher.py` has no concurrency test, so none of this is knowingly
covered.

### What is already good, and stays

`CronSchedule` is not the problem and is not being rewritten. It parses 5- and
6-field expressions with ranges, steps, and lists; it computes in the target
timezone via `ZoneInfo` and returns UTC; it handles leap-year cases like
`0 0 29 2 *`; and it bounds its search to four years and raises `CronError`
rather than spinning. `Interval` reuses `TriggerKind.SCHEDULE`, so one dispatch
path serves both. `TriggerStore` is already a protocol on all four backends and
already in the conformance matrix. Everything below builds on that.

---

## 1. Rules of engagement

This plan closes gaps in a **library**. The constraint that shapes every
decision below: nothing here may encode how any particular host runs.

**Non-goals, stated so they cannot creep in:**

- **No message bus.** No Kafka, no Redis Streams, no queue as the dispatch
  mechanism. A fired trigger becomes `runtime.submit()` and nothing else. A host
  that wants a bus already has one, and `submit()` is the seam it calls.
- **No tenancy.** No `org_id`, no partition-aware scheduling. Loom does not know
  what a tenant is and should not learn.
- **No imported fairness model.** "Fairness" in a platform scheduler means
  fairness *across tenants*, which is a concept this library does not have. What
  is generic is a bounded batch and a deterministic order; that is all this plan
  takes.
- **No new dependency.** Claiming uses each store's own primitives. No
  distributed-lock service, no scheduler library.
- **No behaviour change for a default `Runtime()`.** A single-process
  deployment that works today must behave identically, minus the bugs.

**The positive rule**, the same one the rest of the codebase follows: *Loom
ships the port and a reference adapter that needs no infrastructure; the host
ships the adapter that knows about the host.*

---

## 2. The boundary — which third of "cron" belongs here

"Cron" is three separable things, and conflating them is why this looks like a
bigger question than it is.

| Layer | Belongs to | Why |
|---|---|---|
| **Schedule semantics** — parsing, timezone, DST, next-fire, missed-fire policy | **Loom** | Pure logic with no infrastructure. A host reimplementing it gets DST wrong, and there is no reason for two implementations to exist. |
| **Trigger state and exactly-once firing** | **Loom** | "This runs once" is a *durability guarantee*, and durability guarantees are the product. `TriggerStore` is already a port on four backends; the missing piece is one method, not a subsystem. |
| **Dispatch transport and worker fleet** — topics, consumers, autoscaling | **The host** | This is the host's bus and its operational model. `runtime.submit()` is the seam, and it is already the right shape. |

The second row is the one worth arguing about, so: leaving exactly-once to each
host means every host separately solves the hardest part of scheduling, and the
failure mode — a billing job that runs twice because a deploy briefly had two
replicas — is precisely the class of failure this library exists to prevent.
Loom already owns exactly-once for *events* (`claim_event_delivery`, added for
event ingress). Owning it for *time* is the same guarantee against a different
input.

---

## 3. HLD — the seam map

```
@workflow(triggers=[Schedule("0 9 * * 1-5", timezone="Europe/London")])
                                 │
                    register()   ▼
                          ┌─────────────────┐
                          │  TriggerRecord  │   next_fire_at, claim state
                          └────────┬────────┘
                                   │ persisted through
                          ┌────────▼────────┐
                          │  TriggerStore   │   Memory · SQLite · Postgres · Mongo
                          └────────┬────────┘
                                   │ claim_due_triggers(now, owner, lease)
                          ┌────────▼────────┐
                          │TriggerDispatcher│   occurrences_due() → [Fire]
                          └────────┬────────┘
                                   │ submit(idempotency_key=fire.key)
                          ┌────────▼────────┐
                          │     Runtime     │   ← the only seam a host calls
                          └─────────────────┘
```

Two defences, deliberately layered, because they fail differently:

- **The claim** stops two dispatchers doing the same work. It is an
  optimisation and an operational nicety.
- **The fire key** makes it *not matter* if the claim is lost — to a network
  partition, a clock skew, an expired lease. It is the correctness guarantee.

A design with only the claim is one lease expiry away from a double run. A
design with only the key is correct but wasteful. Both is cheap.

---

## 4. LLD — the gaps, with interfaces

### S1 — A fire has an identity (P0)

**Problem.** A dispatch is currently anonymous: `submit()` with no idempotency
key. Two processes produce two runs; one process retrying after a crash between
`submit` and `update_after_fire` produces two runs.

**Design.** Name the occurrence, not the attempt.

```python
# runtime/dispatcher.py

@dataclass(frozen=True)
class Fire:
    """One scheduled occurrence — the unit of dispatch.

    Identified by the moment the *schedule* called for, never by the wall
    clock that noticed it. Two processes noticing the same occurrence two
    seconds apart must compute the same key, or the key buys nothing.
    """

    trigger_id: str
    workflow: str
    scheduled_for: datetime

    @property
    def key(self) -> str:
        return f"{self.trigger_id}@{self.scheduled_for.isoformat()}"
```

`tick()` passes `idempotency_key=fire.key` to `submit()`. The uniqueness is
already enforced *by the database* — `idempotency_key TEXT UNIQUE` on Postgres
and SQLite, a unique partial index on Mongo — so this is not a hopeful
convention, it is a constraint.

**Cost:** ~10 lines. No new store surface. Works across processes today.

### S2 — Advance from the schedule, not from the clock (P0)

**Problem.** `_next_fire_from_record(trigger, now)` computes the next fire from
**`now`** — the wall clock of whichever process happened to fire it. Two
processes with slightly different clocks compute different next-fire times, so
the schedule drifts by however late the dispatcher was. A cron that fires at
09:00:04 next fires relative to :04.

**Design.** Advance from `fire.scheduled_for`. Deterministic, independent of
who fired it and how late, and idempotent — two processes computing it arrive at
the same answer, so a duplicated `update_after_fire` is harmless.

This is also the prerequisite for S3: missed occurrences are only enumerable if
the last *scheduled* time is known, rather than the last time someone noticed.

### S3 — Missed fires are a policy, not an accident (P1)

**Problem.** `catch_up` exists and is ignored. After an hour of downtime, an
hourly cron silently loses an occurrence and nothing records that it did.

**Design.** With S2, the missed set is computable: every schedule point in
`(last_scheduled, now]`.

```python
@dataclass
class Schedule(TriggerSpec):
    cron: str
    timezone: str = "UTC"
    catch_up: bool = False
    max_catch_up: int = 10
    """Ceiling on backfilled occurrences. Without it, a per-minute cron and a
    week of downtime is ten thousand runs at once — a self-inflicted outage on
    top of the one that just ended. Occurrences beyond the ceiling are dropped
    *loudly*: counted, logged, and recorded on the trigger record."""
    jitter: Duration = 0.0
```

- `catch_up=False` (default, unchanged): skip to the next future occurrence,
  and **record how many were skipped** on the trigger record. Silently skipping
  is what it does today; the fix is that it stops being silent.
- `catch_up=True`: fire each missed occurrence, oldest first, each with its own
  `Fire.key`, so a backfill is replay-safe and interruptible — a dispatcher that
  dies halfway through resumes without duplicating what it already submitted.

### S4 — Jitter applies to dispatch, not to the schedule (P1)

**Problem.** `jitter` exists and is ignored. Its purpose is real: a hundred
triggers at `0 0 * * *` all submit in the same millisecond.

**Design.** Jitter delays *when the submit happens*, sampled per occurrence. It
must **not** enter `Fire.key`, or two processes jitter differently, compute
different keys, and the idempotency defence evaporates — the option meant to
smooth load would have quietly disabled the guarantee. The key is the schedule's
moment; the jitter is when we get around to it.

### S5 — Claiming (P2)

**Problem.** Wasted work, and a trigger record advanced by two writers.

**Design.** One new method on an existing port.

```python
# stores/base.py — TriggerStore

async def claim_due_triggers(
    self,
    now: datetime,
    *,
    owner: str,
    lease_seconds: float,
    limit: int = 50,
) -> list[TriggerRecord]:
    """Atomically take ownership of due triggers, and return only those
    this call actually claimed.

    Three properties, each the fix for a specific failure:

    - **Exclusive.** Two concurrent callers never both receive one record.
    - **Leased, not locked.** A claim expires after ``lease_seconds``, so a
      dispatcher that dies mid-tick does not park a trigger forever. This is
      the difference between a crash costing one late run and costing every
      future run.
    - **Does not advance.** Claiming is not firing. ``update_after_fire``
      remains the only thing that moves ``next_fire_at``, so a claim that is
      never acted on expires and the occurrence is picked up again.
    """
```

Per backend, using what each already has — no new dependency:

| Store | Mechanism |
|---|---|
| Postgres | one `UPDATE … WHERE … RETURNING`, with `FOR UPDATE SKIP LOCKED` on the candidate select |
| Mongo | `find_one_and_update` per candidate, filtered on the claim window |
| SQLite | `BEGIN IMMEDIATE`; the writer lock is the exclusivity |
| Memory | the existing mutex |

`TriggerRecord` gains `claimed_by: str = ""` and `claimed_until: datetime | None`.

**Named cost:** Postgres and SQLite need the column added to an existing table.
`ALTER TABLE … ADD COLUMN IF NOT EXISTS` on Postgres; SQLite needs a
`PRAGMA table_info` check first. This is the one place in the plan that touches
a deployed schema, and it should be reviewed as such rather than buried.

### S6 — One scheduler, one lease (P3)

**Problem.** A host that calls `start_scheduler(elector=…)` reasonably believes
its scheduling is single-leader. Its timers are; its crons are not.

**Design.** `start_scheduler` also drives the dispatcher, under the same lease.
One call covers timers, orphan recovery, and cron. `TriggerDispatcher.start()`
remains for the host that wants to run dispatch separately, and its docstring
says plainly that it does no leader election of its own.

### S8 — Registration is not idempotent (P1) — **found during P1, not planned**

**Problem.** `register()` minted `trigger_id=new_id("trg")` unconditionally, and
registration runs on every process start. Each boot added another record for the
same declared schedule: after three deploys the 10:00 report went out three
times, from three rows whose differing ids no occurrence key could collapse.
Worse than the two-replica race this plan was written for — that one needs a
window, this one is guaranteed on every restart.

**Design.** Identity from what the trigger *is*: a hash of the workflow plus the
fields that decide when it fires, and nothing else.

Two traps found while building it, both now pinned by tests:

- `Schedule.describe()` includes **`next_fire`, a timestamp**. Hashing the whole
  description made the id depend on what time the process booted — reintroducing
  the duplication the id exists to prevent, in a form that only shows up in
  production because a test with a fixed clock cannot see it. Hence an
  *allowlist* of identifying fields, not a denylist.
- Policy (`catch_up`, `max_catch_up`, `jitter`) is excluded from the identity, so
  changing it does not orphan the trigger's `last_fire_at` and `run_count`.
  Registration updates the stored spec in place instead.

Registration also retires triggers the workflow no longer declares, so changing
a cron stops the old one rather than leaving an orphan that appears in no source
file.

### S9 — Two declared fields never reached the store (P2) — **found during P2**

`Schedule.jitter` was not serialised by `describe()` at all, so even a
dispatcher that read it would have found nothing; `max_catch_up` had to be added
alongside. The lesson generalises: a policy field is only real once it is
declared, serialised, read, *and* tested — this plan's original §0 caught the
"read" half and missed the "serialised" half.

### S10 — A lease outliving the fire (P3) — **found during P3**

Claiming and advancing were designed as separate operations, which left the
claim to lapse on its own. That makes the lease duration a **silent lower bound
on the schedule**: a per-minute cron under a sixty-second lease fires once and
then idles until the claim expires. Nothing errors; the schedule is simply
slower than it says it is.

`update_after_fire` now releases the claim in the same operation that advances,
on all four stores. Claim → fire → advance-and-release is the whole cycle.

Also settled here: the claim needed **no schema migration**. Claim state lives
inside `TriggerRecord`, which every store already persists whole, so the one
irreversible-ish step this plan flagged in §4 never had to be taken. Postgres
filters it out of JSONB rather than a column; the due set is already narrowed by
the existing index, so it costs nothing measurable.

### S7 — The corner where the race actually surfaces (P3)

The `idempotency_key` uniqueness is enforced by the database, so a genuine race
resolves as a *constraint violation on insert*, not as two runs. `MongoStore`
catches `DuplicateKeyError` in two places; the other stores need checking. The
required behaviour is uniform and worth stating: **a create that loses the
idempotency race returns the winner's run, it does not raise.** Otherwise the
losing dispatcher logs an exception and leaves the trigger unadvanced, and a
correctness win becomes an operational alarm.

Also in this phase, one line: `Schedule`'s docstring points at
`loom.worker.leader`; the module is `loom.runtime.leader`.

---

## 5. Files that change

| File | Change | Gap |
|---|---|---|
| `runtime/dispatcher.py` | `Fire`, occurrence enumeration, keyed submit, jitter | S1–S4 |
| `triggers/specs.py` | `max_catch_up`; docstring fix | S3, S7 |
| `core/models.py` | `TriggerRecord.claimed_by` / `claimed_until` / `skipped_count` | S3, S5 |
| `stores/base.py` | `claim_due_triggers` on the `TriggerStore` protocol | S5 |
| `stores/{memory,sqlite,postgres,mongo}.py` | four implementations + schema | S5 |
| `runtime/engine.py` | `start_scheduler` drives the dispatcher | S6 |
| `tests/conformance/test_trigger_claim.py` | new — the claim, on every backend | S5 |
| `tests/test_scheduling.py` | new — occurrences, catch-up, jitter, double-fire | S1–S4 |
| `docs/guides/embedding.md` | a scheduling section, executed in CI | all |
| `CLAUDE.md` | the scheduling section | all |

---

## 6. Phases

Ordered so that **correctness lands first and needs no schema change**. If the
plan is stopped after P1, the double-fire bug is fixed and nothing has been
migrated.

### P0 — The failing test (gates everything)

Two `TriggerDispatcher`s over one store, one due trigger, `ManualClock`. Assert
exactly one run. **This test must fail on today's code** — a regression test
that passes before the fix is testing nothing.

*Exit:* red, for the documented reason.

### P1 — Identity and advancement (S1, S2, S8) — **done**

`Fire`, keyed submit, advance from the schedule, stable trigger ids, reconcile
on register. Also closed on the way: `MemoryStore` did not enforce idempotency
uniqueness — it kept the first id in its index and stored the second record
anyway — so the default store held two runs for one key while every persistent
store rejected it. `tests/conformance/test_idempotent_create.py` now states that
property for every backend.

*Exit:* P0 goes green. A dispatcher killed between submit and update, then
restarted, produces one run. A cron fired four seconds late still next fires on
the schedule's grid, not four seconds off it.

### P2 — Missed-fire policy and jitter (S3, S4, S9) — **done**

*Exit:* with `ManualClock` moved forward an hour, an hourly cron with
`catch_up=False` fires once and records three skipped; with `catch_up=True`
fires four; with `max_catch_up=2` fires two and says so. Jitter delays dispatch
and leaves `Fire.key` byte-identical.

### P3 — Claiming (S5) — **done**

*Exit:* `claim_due_triggers` on four backends, in the conformance matrix.
Concurrent claims are disjoint; an expired lease is re-claimable; a claim does
not advance `next_fire_at`. Mongo and Postgres exercised against real servers in
CI, absent means **skipped and named**.

### P4 — Wiring and honesty (S6, S7, S10) — **done**

*Exit:* `start_scheduler(elector=…)` covers cron; a lost idempotency race
returns the existing run on every backend; the docstring points at a module that
exists; `embedding.md` gains a scheduling section that runs in CI.

---

## 7. Testing

| Level | Covers | The failure it exists to catch |
|---|---|---|
| Regression | two dispatchers, one store | the double-fire that is shipping today |
| Conformance | `claim_due_triggers` × 4 backends | a claim that is exclusive on Postgres and advisory on Mongo |
| Clock | catch-up, skip, ceiling, jitter | a policy field that is read by nothing |
| Determinism | same occurrence → same key, across processes | jitter or wall-clock leaking into the key |
| Crash | killed between submit and advance | the retry that becomes a second run |
| Property | `next_after` never returns a non-matching moment; advancement is monotonic | a DST transition that fires twice or not at all |

Two defences carried from the rest of this repo because both caught real
defects: **mutation-verify every new guard** (break the condition, confirm a
named test fails, clearing `__pycache__` first), and **skipped is not passed** —
a backend that cannot be reached is reported by name, never dropped.

---

## 8. Review

**Correctness.** The two defences fail independently: the claim is an
optimisation, the key is the guarantee. A reviewer should check specifically
that jitter cannot reach `Fire.key`, since that is the one change in this plan
that could disable the guarantee while appearing to be about performance.

**Security.** Scheduled runs already carry the service principal so they are not
ownerless. Nothing here widens that. `max_catch_up` is also a safety property:
without it, a downtime window converts directly into a burst of runs, which is a
denial of service a workflow can inflict on its own host.

**Performance.** One claim query per tick, bounded by `limit`, on an index that
already exists. The keyed submit adds one indexed lookup per fire. Neither is on
a workflow's hot path.

**Edge cases.** DST — `next_after` computes in the target zone, so a spring
forward skips and an autumn back does not double-fire; the property test pins
it. A trigger whose workflow was deleted is already disabled rather than
retried. A lease expiring mid-fire is covered by the key.

**Maintainability.** One new store method, one new dataclass. The dispatcher
gets an occurrence-enumeration function that is pure and directly testable,
which is where the policy logic goes rather than inline in the loop.

**Cost.** Roughly the size of the event-idempotency phase, and it reuses that
phase's plumbing. The schema change in P2 is the only irreversible-ish step and
it is additive.

**The main risk.** `catch_up` currently does nothing, so a deployment that set
it to `True` and never noticed will begin backfilling after this lands. That is
the fix working as documented, but it is a behaviour change on upgrade and
belongs in release notes, not only in a docstring. `max_catch_up` defaulting to
10 bounds the blast radius of that surprise.
