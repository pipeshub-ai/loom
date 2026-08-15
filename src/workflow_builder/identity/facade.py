"""``AuthorizedFacade`` — a :class:`~workflow_builder.facade.RuntimeFacade` that carries a
:class:`~workflow_builder.identity.principal.Principal`.

The structural decision the whole identity layer rests on: this implements
the port with **identical** method signatures, so
``tests/test_surface_parity.py::test_adapter_signatures_match_the_port``
holds it to the same equality check as :class:`~workflow_builder.facade.LocalFacade`
and :class:`~workflow_builder.facade.RemoteFacade`, and every existing
caller — the CLI, a workflow-manager agent, an MCP tool — keeps working
against it unchanged, because it never sees a parameter the port does not
already declare. Identity lives in this object's own state (``principal``),
never threaded through a call.

Two things this wrapper enforces beyond "does the scope match":

- **Ownership.** ``start()``/``get()``/``list_runs()`` never leak another
  principal's run *content* — an idempotent ``start()`` replayed by a
  second caller against the same key returns the original run's shape
  (status, run_id) with output/input/error redacted, not the first
  caller's data. ``journal()``/``reports()``/``send_event()``/``retry()``/
  ``replay()`` refuse outright for a run this principal did not create —
  retrying or replaying is re-executing under an identity that is not the
  one the run was pinned to, which is exactly the escalation this guards.
- **No escalation via retry/replay.** The principal that created a run is
  pinned into ``record.metadata[PRINCIPAL_KEY]`` at ``start()`` time and
  read back on every later operation against that run, so a second,
  differently-scoped caller cannot force a re-execution and inherit
  whatever the first caller could do.

``cancel()`` is scope-gated only, deliberately not ownership-gated — an
operator holding ``runs:cancel`` can stop *any* run, matching how the
existing role-based ``Permission.FLOW_CANCEL`` already works with no
per-run restriction. It stops a run; it does not read or re-execute it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from workflow_builder.core.exceptions import InsufficientScope
from workflow_builder.facade import RuntimeFacade
from workflow_builder.identity.principal import Principal
from workflow_builder.identity.scopes import Scope

__all__ = ["PRINCIPAL_KEY", "AuthorizedFacade"]

PRINCIPAL_KEY = "loom.principal"
"""The ``record.metadata`` key a run's owning principal's ``subject`` is
pinned under. Read back by every ownership check in this module — a run
with no value under this key predates identity (or was created directly
against a bare :class:`~workflow_builder.runtime.engine.Runtime`) and is
treated as ownerless, visible to any caller who already cleared the scope
check, which is what keeps a pre-identity deployment behaving exactly as
before once this wrapper is introduced.
"""

_REDACTED = frozenset({"output", "input", "error"})


@dataclass
class AuthorizedFacade:
    """Wraps *inner* so every operation is checked against *principal* first."""

    inner: RuntimeFacade
    principal: Principal

    async def workflows(self, *, published: bool = True) -> list[dict[str, Any]]:
        self.principal.requires(Scope.WORKFLOWS_READ.value)
        return await self.inner.workflows(published=published)

    async def start(
        self,
        workflow: str,
        payload: Any,
        *,
        idempotency_key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_WRITE.value)
        pinned = {**(metadata or {}), PRINCIPAL_KEY: self.principal.subject}
        result = await self.inner.start(
            workflow,
            payload,
            idempotency_key=idempotency_key,
            tags=tags,
            metadata=pinned,
            wait=wait,
        )
        # An idempotency-key hit returns whatever run already exists under
        # that key, which may belong to someone else — redact rather than
        # trust that every caller of `start()` also owns what it returns.
        return self._redacted(result)

    async def get(self, run_id: str) -> dict[str, Any] | None:
        self.principal.requires(Scope.RUNS_READ.value)
        result = await self.inner.get(run_id)
        return None if result is None else self._redacted(result)

    async def list_runs(
        self, *, workflow: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.principal.requires(Scope.RUNS_READ.value)
        results = await self.inner.list_runs(workflow=workflow, status=status, limit=limit)
        return [self._redacted(run) for run in results]

    async def journal(self, run_id: str) -> list[dict[str, Any]]:
        self.principal.requires(Scope.RUNS_READ.value)
        await self._require_owned(run_id)
        return await self.inner.journal(run_id)

    async def reports(self, run_id: str, offset: int = 0) -> list[dict[str, Any]]:
        self.principal.requires(Scope.RUNS_READ.value)
        await self._require_owned(run_id)
        return await self.inner.reports(run_id, offset)

    async def send_event(self, run_id: str, name: str, payload: Any) -> None:
        self.principal.requires(Scope.RUNS_WRITE.value)
        await self._require_owned(run_id)
        await self.inner.send_event(run_id, name, payload)

    async def cancel(self, run_id: str) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_CANCEL.value)
        return await self.inner.cancel(run_id)

    async def retry(self, run_id: str) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_WRITE.value)
        await self._require_owned(run_id)
        return await self.inner.retry(run_id)

    async def replay(self, run_id: str) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_WRITE.value)
        await self._require_owned(run_id)
        return await self.inner.replay(run_id)

    async def publish(self, workflow: str) -> dict[str, Any]:
        self.principal.requires(Scope.WORKFLOWS_PUBLISH.value)
        return await self.inner.publish(workflow)

    async def schedules(self, workflow: str | None = None) -> list[dict[str, Any]]:
        self.principal.requires(Scope.RUNS_READ.value)
        return await self.inner.schedules(workflow)

    async def schedule(
        self, workflow: str, cron: str, *, timezone: str = "UTC"
    ) -> dict[str, Any]:
        self.principal.requires(Scope.SCHEDULES_WRITE.value)
        return await self.inner.schedule(workflow, cron, timezone=timezone)

    async def unschedule(self, trigger_id: str) -> bool:
        self.principal.requires(Scope.SCHEDULES_WRITE.value)
        return await self.inner.unschedule(trigger_id)

    async def close(self) -> None:
        await self.inner.close()

    # -- ownership -------------------------------------------------------

    def _is_privileged(self) -> bool:
        return self.principal.has(Scope.ADMIN.value)

    def _owner_of(self, run: dict[str, Any]) -> str | None:
        metadata = run.get("metadata") or {}
        owner = metadata.get(PRINCIPAL_KEY)
        return owner if isinstance(owner, str) else None

    def _redacted(self, run: dict[str, Any]) -> dict[str, Any]:
        """*run*, with content nulled out if it is not this principal's.

        A run with no pinned owner is treated as everyone's — see
        ``PRINCIPAL_KEY``'s docstring for why that has to be the rule
        rather than "no owner means no one".
        """
        owner = self._owner_of(run)
        if owner is None or owner == self.principal.subject or self._is_privileged():
            return run
        return {key: (None if key in _REDACTED else value) for key, value in run.items()}

    async def _require_owned(self, run_id: str) -> None:
        """Raise unless this run is ownerless, this principal's own, or the
        principal is privileged. The gate behind journal/reports/send_event/
        retry/replay — every operation that either exposes full content a
        redaction would not cover, or re-executes the run under whichever
        identity calls it.
        """
        if self._is_privileged():
            return
        run = await self.inner.get(run_id)
        if run is None:
            return  # let the wrapped operation raise its own not-found
        owner = self._owner_of(run)
        if owner is not None and owner != self.principal.subject:
            raise InsufficientScope(
                f"run '{run_id}' belongs to a different principal",
                required=Scope.ADMIN.value,
                held=sorted(self.principal.scopes),
            )
