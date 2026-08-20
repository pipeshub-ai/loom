"""Flow control — admission policies that gate workflow execution.

Before a run starts, the ``AdmissionController`` evaluates the flow's
policy and decides: admit, delay, skip, debounce, or batch.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from loom.runtime.admission_state import AdmissionState, InMemoryAdmissionState
from loom.runtime.clock import Clock, SystemClock

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

    Where the counters live is a :class:`AdmissionState`, not a dict on this
    object. It *was* a dict, and the consequence was that
    ``Runtime(admission=...)`` provided no concurrency limit, no rate limit and
    no singleton guarantee in any multi-worker deployment — the only kind that
    has these problems. Pass
    :class:`~loom.runtime.admission_state.StoreBackedAdmissionState` there and
    the same policies hold across processes.

    Every window this evaluates — throttle, rate limit, debounce, batch — is
    measured against the :class:`~loom.runtime.clock.Clock`, so pass the
    Runtime's own when there is one::

        rt = Runtime(store=store, clock=clock,
                     admission=AdmissionController(clock=clock))

    It reads ``clock.now()`` rather than ``time.monotonic()``, and the trade is
    deliberate. Monotonic time is immune to an NTP step; it is also unreachable
    from a test, which is why nothing anywhere proved that a debounce window
    ever *expires* — only that entering one debounces. A clock step widens or
    narrows one window once and self-corrects; an untestable expiry is a
    permanent hole, since a debounce that never releases looks exactly like a
    trigger that never fired.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        state: AdmissionState | None = None,
    ) -> None:
        self._clock: Clock = clock or SystemClock()
        self._state: AdmissionState = state or InMemoryAdmissionState()
        """Where the counters live.

        Defaults to process-local, which is exactly what shipped and is correct
        for one process. Pass
        :class:`~loom.runtime.admission_state.StoreBackedAdmissionState` for a
        deployment with more than one worker — which is the only kind that has
        the problems these policies solve."""

    # -- public bookkeeping --------------------------------------------------

    async def record_start(self, flow_id: str, partition_key: str = "") -> None:
        """Mark that a run has started (increment in-flight counter)."""
        await self._state.enter(_flow_key(flow_id, partition_key))

    async def record_end(self, flow_id: str, partition_key: str = "") -> None:
        """Mark that a run has finished (decrement in-flight counter)."""
        await self._state.leave(_flow_key(flow_id, partition_key))

    async def in_flight_count(self, flow_id: str, partition_key: str = "") -> int:
        """Return the current in-flight count for a flow/partition."""
        return await self._state.in_flight(_flow_key(flow_id, partition_key))

    # -- evaluation ----------------------------------------------------------

    async def evaluate(
        self,
        flow_id: str,
        policy: FlowControlPolicy,
        *,
        partition_key: str = "",
        now: float | None = None,
    ) -> AdmissionResult:
        """Run all configured checks in priority order.

        Order: concurrency -> singleton -> throttle -> rate limit -> debounce
        -> batch.  The first check that does *not* admit short-circuits.

        *now* overrides the clock reading for this one call, in seconds on the
        same scale the windows are expressed in. For a caller that already has
        a moment in hand and must not re-read the clock between two decisions —
        two policies evaluated for one trigger have to agree about when they
        were evaluated, or one window closes a microsecond after the other.
        """
        key = _flow_key(flow_id, partition_key)
        moment = self._clock.now().timestamp() if now is None else now

        # 1. Concurrency ---------------------------------------------------
        if policy.concurrency is not None:
            ck = _flow_key(
                flow_id,
                policy.concurrency.key or partition_key,
            )
            if await self._state.in_flight(ck) >= policy.concurrency.limit:
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
            if await self._state.in_flight(sk) > 0:
                if policy.singleton.mode == "skip":
                    return AdmissionResult(
                        decision=AdmissionDecision.SKIP,
                        reason=f"singleton already running for {sk}",
                    )
                # `cancel_previous` admitted the new run and cancelled nothing —
                # the comment said "caller is responsible", and no caller was.
                # Refused rather than silently ignored, the rule a sandbox
                # already applies to a limit it cannot honour.
                raise NotImplementedError(
                    f"SingletonPolicy(mode={policy.singleton.mode!r}) is not "
                    "implemented — it admitted the new run and cancelled "
                    "nothing. Use mode='skip', or cancel the prior run yourself "
                    "before submitting."
                )

        # 3. Throttle -------------------------------------------------------
        if policy.throttle is not None:
            min_interval = 1.0 / policy.throttle.max_per_second
            last = await self._state.read(f"{key}:last")
            if last is not None:
                elapsed = moment - last
                if elapsed < min_interval:
                    wait = min_interval - elapsed
                    return AdmissionResult(
                        decision=AdmissionDecision.DELAY,
                        reason="throttle: too soon since last admission",
                        delay_seconds=wait,
                    )

        # 4. Rate limit -----------------------------------------------------
        if policy.rate_limit is not None:
            log = list(await self._state.read(f"{key}:rate", []) or [])
            cutoff = moment - policy.rate_limit.period_seconds
            # Prune expired entries.
            log = [ts for ts in log if ts > cutoff]
            await self._state.write(f"{key}:rate", log)
            if len(log) >= policy.rate_limit.requests:
                oldest = log[0]
                wait = oldest + policy.rate_limit.period_seconds - moment
                return AdmissionResult(
                    decision=AdmissionDecision.DELAY,
                    reason="rate limit exceeded",
                    delay_seconds=max(wait, 0.0),
                )

        # 5. Debounce -------------------------------------------------------
        if policy.debounce is not None:
            dk = _flow_key(flow_id, policy.debounce.key or partition_key)
            last_trigger = await self._state.read(f"{dk}:debounce")
            await self._state.write(f"{dk}:debounce", moment)
            if last_trigger is not None:
                elapsed = moment - last_trigger
                if elapsed < policy.debounce.period_seconds:
                    return AdmissionResult(
                        decision=AdmissionDecision.DEBOUNCE,
                        reason="debounce: trigger received within window",
                        delay_seconds=policy.debounce.period_seconds - elapsed,
                    )

        # 6. Batch ----------------------------------------------------------
        if policy.batch is not None:
            count = int(await self._state.read(f"{key}:batch", 0) or 0) + 1
            first = await self._state.read(f"{key}:batch_first")
            if first is None:
                first = moment
                await self._state.write(f"{key}:batch_first", first)
            await self._state.write(f"{key}:batch", count)
            elapsed = moment - float(first)

            if count < policy.batch.max_size and elapsed < policy.batch.window_seconds:
                return AdmissionResult(
                    decision=AdmissionDecision.BATCH,
                    reason=f"batching: {count}/{policy.batch.max_size} collected",
                    delay_seconds=policy.batch.window_seconds - elapsed,
                )
            # Window expired or batch full — flush.
            await self._state.write(f"{key}:batch", 0)
            await self._state.write(f"{key}:batch_first", None)

        # All checks passed — record and admit.
        await self._state.write(f"{key}:last", moment)
        admitted = list(await self._state.read(f"{key}:rate", []) or [])
        admitted.append(moment)
        await self._state.write(f"{key}:rate", admitted)
        return AdmissionResult(decision=AdmissionDecision.ADMIT)
