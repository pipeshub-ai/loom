"""Example 16 — Serving a Runtime over HTTP.

Workflow *authoring* is Python: durability comes from re-entering a Python
function body, and that does not survive a language boundary. Workflow
*operation* does not need to be. Behind HTTP, a Go service can start a run and a
TypeScript UI can approve one, without either embedding a Python interpreter.

This example runs both sides in-process — no port is opened — so you can see the
whole round trip in one file. In production you would serve the app with
uvicorn and point real clients at it.

Requires:
    pip install workflow-builder[api]

Run:
    python3 examples/cookbook/16_http_server.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.server import LoomClient
from workflow_builder.server.app import create_app
from workflow_builder.state.memory import MemoryStore

# ---------------------------------------------------------------------------
# Workflows to expose
# ---------------------------------------------------------------------------


@step
async def score_lead(email: str) -> int:
    """Score a lead from its domain."""
    return 90 if email.endswith((".gov", ".edu")) else 50


@step
async def send_welcome(email: str) -> str:
    """Pretend to send a welcome message."""
    return f"welcomed {email}"


@workflow(name="onboard_lead", description="Score a lead and welcome it")
async def onboard_lead(ctx: Context, email: str) -> dict:
    """Score, then welcome."""
    score = await ctx.step(score_lead, email)
    greeting = await ctx.step(send_welcome, email)
    return {"score": score, "greeting": greeting}


@workflow(name="refund_request", description="Refund, pending human approval")
async def refund_request(ctx: Context, amount: float) -> str:
    """Park until a human decides, however long that takes."""
    if await ctx.wait_for_approval("refund"):
        return f"refunded ${amount:.2f}"
    return "refund denied"


async def main() -> None:
    import httpx

    header("HTTP Server")

    rt = Runtime(store=MemoryStore())
    rt.register_all([onboard_lead, refund_request])
    app = create_app(rt, title="LOOM Cookbook")

    # ASGITransport speaks to the app directly, so this is a real request/response
    # cycle without binding a socket. Swap for base_url="http://host:8000" to
    # talk to a uvicorn process instead.
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://loom.local")

    async with LoomClient(http=http) as loom:
        header("DISCOVERY")
        for definition in await loom.workflows():
            log("GET /workflows", f"{definition['name']}: {definition['description']}")

        header("START A RUN")
        run = await loom.start("onboard_lead", "ada@mit.edu", wait=True)
        log("POST /runs", f"{run['run_id'][:12]}… {run['status']}")
        log("output", str(run["output"]))

        header("READ ITS JOURNAL")
        for entry in await loom.journal(run["run_id"]):
            log("GET /journal", f"{entry['seq']}. {entry['name']} → {entry['status']}")

        header("HUMAN IN THE LOOP, FROM OUTSIDE PYTHON")
        pending = await loom.start("refund_request", 42.5, wait=True)
        log("POST /runs", f"{pending['run_id'][:12]}… {pending['status']}")

        # Any HTTP client can resolve the approval — a Slack bot, a web UI, curl.
        await loom.approve(pending["run_id"], "refund")
        log("POST /events", "approval:refund delivered")

        final = await loom.wait(pending["run_id"], timeout=5, poll_interval=0.05)
        log("GET /runs/{id}", f"{final['status']}: {final['output']}")

        header("IDEMPOTENCY")
        first = await loom.start("onboard_lead", "grace@navy.gov", idempotency_key="lead-7")
        second = await loom.start("onboard_lead", "grace@navy.gov", idempotency_key="lead-7")
        same = first["run_id"] == second["run_id"]
        log("POST /runs", f"Retried request reused the original run: {same}")

    await rt.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
