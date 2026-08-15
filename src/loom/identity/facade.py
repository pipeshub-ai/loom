"""``AuthorizedFacade`` — a :class:`~loom.facade.RuntimeFacade` that carries a
:class:`~loom.identity.principal.Principal`.

The structural decision the whole identity layer rests on: this implements
the port with **identical** method signatures, so
``tests/test_surface_parity.py::test_adapter_signatures_match_the_port``
holds it to the same equality check as :class:`~loom.facade.LocalFacade`
and :class:`~loom.facade.RemoteFacade`, and every existing
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

from loom.core.exceptions import ConfigurationError, InsufficientScope
from loom.facade import RuntimeFacade
from loom.identity.principal import Principal
from loom.identity.scopes import Scope

__all__ = ["PRINCIPAL_KEY", "AuthorizedFacade"]

PRINCIPAL_KEY = "loom.principal"
"""The ``record.metadata`` key a run's owning principal's ``subject`` is
pinned under. Read back by every ownership check in this module — a run
with no value under this key predates identity (or was created directly
against a bare :class:`~loom.runtime.engine.Runtime`) and is
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
        env: dict[str, str] | None = None,
        credentials: dict[str, str] | Any | None = None,
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_WRITE.value)
        if credentials is not None:

            raise ConfigurationError(
                "credentials= cannot be sent through an AuthorizedFacade — "
                "connect the credential on the server with 'loom connect', or "
                "start the run in-process."
            )
        pinned = {**(metadata or {}), PRINCIPAL_KEY: self.principal.subject}
        result = await self.inner.start(
            workflow,
            payload,
            idempotency_key=idempotency_key,
            tags=tags,
            metadata=pinned,
            wait=wait,
            env=env,
            credentials=None,
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

    async def pending(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """What is parked on a person, narrowed to runs this principal owns.

        Reading it is ``runs:read``; a request carries the run's own context, so
        listing every one of them would leak across principals exactly as
        ``list_runs`` would.
        """
        self.principal.requires(Scope.RUNS_READ.value)
        if run_id is not None:
            await self._require_owned(run_id)
            return await self.inner.pending(run_id)
        mine = {run["run_id"] for run in await self.list_runs(status="suspended")}
        return [row for row in await self.inner.pending() if row["run_id"] in mine]

    async def respond(
        self, run_id: str, subject: str, answer: dict[str, Any]
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_WRITE.value)
        await self._require_owned(run_id)
        return await self.inner.respond(run_id, subject, answer)

    async def nodes(
        self, query: str = "", *, category: str | None = None
    ) -> list[dict[str, Any]]:
        # The catalog describes what this deployment can run, not anybody's
        # data, so it reads under the same scope as listing workflows.
        self.principal.requires(Scope.WORKFLOWS_READ.value)
        return await self.inner.nodes(query, category=category)

    async def node(self, node_id: str) -> dict[str, Any]:
        self.principal.requires(Scope.WORKFLOWS_READ.value)
        return await self.inner.node(node_id)

    async def list_artifacts(self) -> list[dict[str, Any]]:
        self.principal.requires(Scope.RUNS_READ.value)
        return await self.inner.list_artifacts()

    async def artifact_history(self, name: str) -> list[dict[str, Any]]:
        self.principal.requires(Scope.RUNS_READ.value)
        return await self.inner.artifact_history(name)

    async def artifact_url(
        self, name: str, version: int | None = None, expires_in: int = 3600
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_READ.value)
        return await self.inner.artifact_url(name, version, expires_in)

    async def read_artifact(
        self, name: str, version: int | None = None
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_READ.value)
        return await self.inner.read_artifact(name, version)

    async def put_artifact(
        self,
        name: str,
        content_b64: str,
        *,
        mime: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_WRITE.value)
        return await self.inner.put_artifact(
            name, content_b64, mime=mime, metadata=metadata
        )

    async def upload_url(
        self,
        name: str,
        mime: str = "application/octet-stream",
        max_size: int | None = None,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_WRITE.value)
        return await self.inner.upload_url(
            name, mime=mime, max_size=max_size, expires_in=expires_in
        )

    async def confirm_upload(
        self,
        upload_id: str,
        name: str,
        run_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_WRITE.value)
        return await self.inner.confirm_upload(
            upload_id, name, run_id=run_id, metadata=metadata
        )

    async def read_blob(
        self, ref: str, expires: int, signature: str, method: str = "GET"
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_READ.value)
        return await self.inner.read_blob(ref, expires, signature, method)

    async def write_blob(
        self,
        ref: str,
        expires: int,
        signature: str,
        content_b64: str,
        mime: str = "application/octet-stream",
        method: str = "PUT",
    ) -> dict[str, Any]:
        self.principal.requires(Scope.RUNS_WRITE.value)
        return await self.inner.write_blob(
            ref, expires, signature, content_b64, mime=mime, method=method
        )

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
