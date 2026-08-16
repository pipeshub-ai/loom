"""A host embedding Loom, written against nothing but its public seams.

This is the acid test for the whole phase, and it is deliberately adversarial:
if this file needs a private attribute, an internal import, or a monkey-patch,
then a real host needs one too, and the port it reached past is not finished.

The host below is not any particular product. It is the shape every embedding
host has — its own identity, its own notification transport, its own idea of
where code lives — expressed as fakes:

===================  =========================================================
`Switchboard`        a ``HumanChannel``: the host's notification bus
`the workflow`       ordinary code, no host imports, no store choice
`the Runtime`        composed by the host: store, sandbox, broker, channel
===================  =========================================================

What it drives, in one run: start → **sandboxed** execution → park on a human →
answer over the host's own channel → resume → read back *which version of the
code* ran. Each of those was a separate gap; this asserts they compose, which is
a different claim from each one working alone.
"""

from __future__ import annotations

import os

from loom import Context, Runtime, step, workflow
from loom.nodes.human.channel import DeliveryReceipt, HumanRequest
from loom.runtime.sandbox import SandboxPolicy
from loom.runtime.sandboxes import SubprocessSandbox
from loom.stores.sqlite import SQLiteStore


class Switchboard:
    """The host's notification transport, behind ``HumanChannel``.

    A real one posts to Slack, or writes a row its frontend polls. This one
    keeps the requests in a list, which is all the protocol asks of it — and the
    point of the protocol is that Loom never learns which of those it is.
    """

    name = "switchboard"

    def __init__(self) -> None:
        self.delivered: list[HumanRequest] = []
        self.withdrawn: list[str] = []

    async def deliver(self, request: HumanRequest) -> DeliveryReceipt:
        self.delivered.append(request)
        return DeliveryReceipt(delivered=True, channel=self.name)

    async def withdraw(self, request_id: str, reason: str) -> None:
        self.withdrawn.append(request_id)

    # -- what the host's own API would call ---------------------------------

    def pending_for(self, run_id: str) -> HumanRequest | None:
        return next((r for r in self.delivered if r.run_id == run_id), None)


@step
async def fetch_invoice(invoice_id: str) -> dict:
    """Stands in for a call the host's credentials would authorise."""
    return {"id": invoice_id, "amount": 4200}


@step
async def pay_invoice(invoice_id: str, amount: int) -> str:
    return f"paid {amount} for {invoice_id}"


@workflow(name="settle_invoice")
async def settle_invoice(ctx: Context, payload: dict) -> str:
    """Ordinary workflow code: no host imports, and no choice of store.

    Where the journal lives and where this body runs are both the host's
    decisions, made at the Runtime. That is what lets the same file run in a
    test, on a laptop, and inside a sandbox in production.
    """
    invoice = await ctx.step(fetch_invoice, invoice_id=payload["invoice_id"])
    # The node, not ctx.wait_for_approval: LOOM parks either way, but only the
    # node delivers the request to the host's channel. A run parked with nobody
    # notified is indistinguishable from patience.
    await ctx.node("human.approval", {"subject": "payment"})
    return await ctx.step(
        pay_invoice, invoice_id=invoice["id"], amount=invoice["amount"]
    )


def a_host(tmp_path, *, sandboxed: bool) -> tuple[Runtime, Switchboard]:
    """Compose Loom the way a host would: every seam supplied, none reached past."""
    switchboard = Switchboard()
    runtime = Runtime(
        store=SQLiteStore(str(tmp_path / "host.db")),
        human=switchboard,
        **(
            {
                "sandbox": SubprocessSandbox(),
                "sandbox_policy": SandboxPolicy(max_wall_seconds=30),
            }
            if sandboxed
            else {}
        ),
    )
    runtime.register(settle_invoice)
    return runtime, switchboard


class TestAHostCanDriveTheWholeLifecycle:
    async def test_start_park_answer_resume_and_trace(self, tmp_path) -> None:
        """The exit criterion, end to end and in one run."""
        rt, switchboard = a_host(tmp_path, sandboxed=True)

        # The host records which code it is about to run, and gets a version back.
        await rt.publish(settle_invoice, source=_source_of_this_module())

        parked = await rt.run(settle_invoice, {"invoice_id": "INV-1"})
        assert parked.status.value == "suspended", (
            f"expected a park on the human, got {parked.status}: {parked.error}"
        )

        # The host's own transport was told, exactly once, with enough to build a UI.
        request = switchboard.pending_for(parked.run_id)
        assert request is not None, "nobody was notified; the run is parked in silence"
        assert request.subject == "payment"

        # The host's API answers on behalf of whoever it authenticated.
        await rt.approve(parked.run_id, "payment")
        resumed = await rt.resume(parked.run_id)

        assert resumed.status.value == "completed", f"resume failed: {resumed.error}"
        assert resumed.output == "paid 4200 for INV-1"

        # And afterwards: which code did that, exactly?
        version = await rt.version_of(resumed.run_id)
        assert version is not None, "the run cannot be traced to the code that ran"
        assert "settle_invoice" in await rt.versions.source_of(version)

    async def test_the_body_really_ran_in_the_sandbox(self, tmp_path) -> None:
        """Otherwise the test above proves the lifecycle and not the isolation."""
        rt, _ = a_host(tmp_path, sandboxed=True)
        rt.register(report_pid)

        result = await rt.run(report_pid, {})

        assert result.output != os.getpid()

    async def test_the_same_host_code_works_unsandboxed(self, tmp_path) -> None:
        """A sandbox is a deployment decision, so switching it off changes only
        that. If the workflow needed editing to run inline, the seam leaked."""
        rt, switchboard = a_host(tmp_path, sandboxed=False)

        parked = await rt.run(settle_invoice, {"invoice_id": "INV-2"})
        await rt.approve(parked.run_id, "payment")
        resumed = await rt.resume(parked.run_id)

        assert resumed.output == "paid 4200 for INV-2"
        assert switchboard.pending_for(parked.run_id) is not None


@workflow(name="report_pid")
async def report_pid(ctx: Context, payload: dict) -> int:
    return os.getpid()


class TestTheHostIsToldWhenItIsMisconfigured:
    async def test_no_channel_configured_is_refused_before_the_run_parks(
        self, tmp_path
    ) -> None:
        """A run parked with nobody listening is indistinguishable from patience,
        so it is found a day late. Failing at the park is the whole point."""
        rt = Runtime(store=SQLiteStore(str(tmp_path / "silent.db")))
        rt.register(needs_a_person)

        result = await rt.run(needs_a_person, {})

        assert result.status.value == "failed"

    async def test_delivery_happens_once_across_a_resume(self, tmp_path) -> None:
        """Delivery is journaled, so a restart does not re-notify. Without that
        every redeploy re-pings the team, and they learn to ignore the channel."""
        rt, switchboard = a_host(tmp_path, sandboxed=False)

        parked = await rt.run(settle_invoice, {"invoice_id": "INV-3"})
        again = await rt.resume(parked.run_id)  # still waiting: nobody answered
        assert again.status.value == "suspended"

        for_this_run = [r for r in switchboard.delivered if r.run_id == parked.run_id]
        assert len(for_this_run) == 1, f"notified {len(for_this_run)} times"


@workflow(name="needs_a_person")
async def needs_a_person(ctx: Context, payload: dict) -> str:
    await ctx.node("human.approval", {"subject": "anything"})
    return "approved"


def _source_of_this_module() -> str:
    """What the host would have read out of its own repository."""
    import inspect
    import sys

    return inspect.getsource(sys.modules[__name__])


def test_the_host_uses_no_private_api() -> None:
    """The claim this whole file exists to make, checked mechanically.

    A host that has to reach past a seam has found a seam that is not finished,
    and the cheapest way for that to creep back in is one underscore at a time.
    """
    import re

    source = _source_of_this_module()
    private = re.findall(r"\b(?:rt|runtime|switchboard)\.(_\w+)", source)

    assert not private, f"the reference host reached past a seam: {sorted(set(private))}"
