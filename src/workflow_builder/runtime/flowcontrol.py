"""Flow control — admission policies that gate workflow execution.

Before a run starts, the ``AdmissionController`` evaluates the flow's
policy and decides: admit, delay, skip, debounce, or batch.
"""

from __future__ import annotations

import time
from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Decision enum
# ---------------------------------------------------------------------------


class AdmissionDecision(StrEnum):
    """Outcome of an admission check."""

    ADMIT = "admit"
    DELAY = "delay"
    SKIP = "skip"
    DEBOUNCE = "debounce"
    BATCH = "batch"


# ---------------------------------------------------------------------------
# Policy models
# ---------------------------------------------------------------------------


class ConcurrencyPolicy(BaseModel):
    """Limit how many runs of a flow (or partition) execute simultaneously."""

    limit: int
    key: str | None = None


class ThrottlePolicy(BaseModel):
    """Steady-rate throttle expressed as a maximum throughput."""

    max_per_second: float


class RateLimitPolicy(BaseModel):
    """Fixed-window rate limit: *requests* per *period_seconds*."""

    requests: int
    period_seconds: float


class DebouncePolicy(BaseModel):
    """Coalesce rapid triggers — only the last one within the window fires."""

    period_seconds: float
    key: str | None = None


class BatchPolicy(BaseModel):
    """Accumulate triggers into a batch before admitting."""

    max_size: int
    window_seconds: float


class SingletonPolicy(BaseModel):
    """Ensure at most one active run per key.

    ``mode`` controls what happens when a second run is requested:

    * ``"skip"`` — silently drop the new request.
    * ``"cancel_previous"`` — cancel the existing run and admit the new one.
    """

    key: str | None = None
    mode: str = "skip"


class FlowControlPolicy(BaseModel):
    """Composite policy evaluated by :class:`AdmissionController`.

    All sub-policies are optional; when omitted the corresponding check is
    skipped.  ``priority`` is advisory — higher values get preference when
    resources are scarce.
    """

    concurrency: ConcurrencyPolicy | None = None
    throttle: ThrottlePolicy | None = None
    rate_limit: RateLimitPolicy | None = None
    debounce: DebouncePolicy | None = None
    batch: BatchPolicy | None = None
    singleton: SingletonPolicy | None = None
    priority: int = 0


# ---------------------------------------------------------------------------
# Admission result
# ---------------------------------------------------------------------------


class AdmissionResult(BaseModel):
    """Outcome returned by :meth:`AdmissionController.evaluate`."""

    decision: AdmissionDecision
    reason: str = ""
    delay_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flow_key(flow_id: str, partition_key: str = "") -> str:
    """Build an internal tracking key, optionally partitioned."""
    return f"{flow_id}:{partition_key}" if partition_key else flow_id


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class AdmissionController:
    """Evaluate :class:`FlowControlPolicy` to decide whether a run may start.

    All state is held in-memory, making the controller easy to test and
    suitable for single-process deployments.  Distributed deployments should
    back these counters with a shared store (Redis, database row locks, etc.).
    """

    def __init__(self) -> None:
        self._in_flight: dict[str, int] = defaultdict(int)

        # Throttle: track the timestamp of the last admitted run per key.
        self._last_admitted: dict[str, float] = {}

        # Rate limit: sliding log of admission timestamps per key.
        self._rate_log: dict[str, list[float]] = defaultdict(list)

        # Debounce: last trigger timestamp per key.
        self._debounce_last: dict[str, float] = {}

        # Batch: accumulated count and first-trigger timestamp per key.
        self._batch_counts: dict[str, int] = defaultdict(int)
        self._batch_first_ts: dict[str, float] = {}

    # -- public bookkeeping --------------------------------------------------

    def record_start(self, flow_id: str, partition_key: str = "") -> None:
        """Mark that a run has started (increment in-flight counter)."""
        self._in_flight[_flow_key(flow_id, partition_key)] += 1

    def record_end(self, flow_id: str, partition_key: str = "") -> None:
        """Mark that a run has finished (decrement in-flight counter)."""
        key = _flow_key(flow_id, partition_key)
        if self._in_flight[key] > 0:
            self._in_flight[key] -= 1

    def in_flight_count(self, flow_id: str, partition_key: str = "") -> int:
        """Return the current in-flight count for a flow/partition."""
        return self._in_flight[_flow_key(flow_id, partition_key)]

    # -- evaluation ----------------------------------------------------------

    async def evaluate(
        self,
        flow_id: str,
        policy: FlowControlPolicy,
        *,
        partition_key: str = "",
    ) -> AdmissionResult:
        """Run all configured checks in priority order.

        Order: concurrency -> singleton -> throttle -> rate limit -> debounce
        -> batch.  The first check that does *not* admit short-circuits.
        """
        key = _flow_key(flow_id, partition_key)
        now = time.monotonic()

        # 1. Concurrency ---------------------------------------------------
        if policy.concurrency is not None:
            ck = _flow_key(
                flow_id,
                policy.concurrency.key or partition_key,
            )
            if self._in_flight[ck] >= policy.concurrency.limit:
                return AdmissionResult(
                    decision=AdmissionDecision.DELAY,
                    reason=(
                        f"concurrency limit ({policy.concurrency.limit}) "
                        f"reached for {ck}"
                    ),
                )

        # 2. Singleton ------------------------------------------------------
        if policy.singleton is not None:
            sk = _flow_key(flow_id, policy.singleton.key or partition_key)
            if self._in_flight[sk] > 0 and policy.singleton.mode == "skip":
                return AdmissionResult(
                    decision=AdmissionDecision.SKIP,
                    reason=f"singleton already running for {sk}",
                )
                # "cancel_previous" — caller is responsible for actually
                # cancelling the prior run; we just admit the new one.

        # 3. Throttle -------------------------------------------------------
        if policy.throttle is not None:
            min_interval = 1.0 / policy.throttle.max_per_second
            last = self._last_admitted.get(key)
            if last is not None:
                elapsed = now - last
                if elapsed < min_interval:
                    wait = min_interval - elapsed
                    return AdmissionResult(
                        decision=AdmissionDecision.DELAY,
                        reason="throttle: too soon since last admission",
                        delay_seconds=wait,
                    )

        # 4. Rate limit -----------------------------------------------------
        if policy.rate_limit is not None:
            log = self._rate_log[key]
            cutoff = now - policy.rate_limit.period_seconds
            # Prune expired entries.
            log[:] = [ts for ts in log if ts > cutoff]
            if len(log) >= policy.rate_limit.requests:
                oldest = log[0]
                wait = oldest + policy.rate_limit.period_seconds - now
                return AdmissionResult(
                    decision=AdmissionDecision.DELAY,
                    reason="rate limit exceeded",
                    delay_seconds=max(wait, 0.0),
                )

        # 5. Debounce -------------------------------------------------------
        if policy.debounce is not None:
            dk = _flow_key(flow_id, policy.debounce.key or partition_key)
            last_trigger = self._debounce_last.get(dk)
            self._debounce_last[dk] = now
            if last_trigger is not None:
                elapsed = now - last_trigger
                if elapsed < policy.debounce.period_seconds:
                    return AdmissionResult(
                        decision=AdmissionDecision.DEBOUNCE,
                        reason="debounce: trigger received within window",
                        delay_seconds=policy.debounce.period_seconds - elapsed,
                    )

        # 6. Batch ----------------------------------------------------------
        if policy.batch is not None:
            self._batch_counts[key] += 1
            if key not in self._batch_first_ts:
                self._batch_first_ts[key] = now

            count = self._batch_counts[key]
            elapsed = now - self._batch_first_ts[key]

            if count < policy.batch.max_size and elapsed < policy.batch.window_seconds:
                return AdmissionResult(
                    decision=AdmissionDecision.BATCH,
                    reason=f"batching: {count}/{policy.batch.max_size} collected",
                    delay_seconds=policy.batch.window_seconds - elapsed,
                )
            # Window expired or batch full — flush.
            self._batch_counts[key] = 0
            self._batch_first_ts.pop(key, None)

        # All checks passed — record and admit.
        self._last_admitted[key] = now
        self._rate_log[key].append(now)
        return AdmissionResult(decision=AdmissionDecision.ADMIT)
