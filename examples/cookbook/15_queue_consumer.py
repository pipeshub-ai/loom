"""Example 15 — Consuming a message queue.

Messages become workflow runs. The interesting property is the acknowledgement
contract: a message is acked only after its run is durably recorded, and the
run's idempotency key is derived from the message, so a redelivery resolves to
the run that already exists instead of doing the work twice.

That combination — at-least-once delivery from the broker, exactly-once
execution in LOOM — is what you want from queue ingress.

Run:
    python3 examples/cookbook/15_queue_consumer.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log

from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore
from loom.triggers.queue import InMemoryQueue, QueueConsumer
from loom.triggers.specs import OnEvent

# ---------------------------------------------------------------------------
# The workflow each message starts
# ---------------------------------------------------------------------------


@step
async def validate_order(order: dict) -> dict:
    """Check the order has what we need."""
    if not order.get("sku"):
        raise ValueError(f"order {order.get('order_id')} has no SKU")
    return order


@step
async def charge(order: dict) -> str:
    """Pretend to take payment."""
    return f"charge-{order['order_id']}"


# `idempotency_field` says: two messages carrying the same order_id are the same
# order, however many times the broker delivers them or the producer republishes.
@workflow(
    name="process_order",
    triggers=[OnEvent(topic="orders", idempotency_field="order_id", batch_size=5)],
)
async def process_order(ctx: Context, order: dict) -> str:
    """Validate an order and charge for it."""
    validated = await ctx.step(validate_order, order)
    return await ctx.step(charge, validated)


async def main() -> None:
    header("Queue Consumer")

    # Swap InMemoryQueue for a QueueBackend over Redis Streams, SQS, or Kafka —
    # the consumer only needs poll / ack / nack.
    queue = InMemoryQueue()
    rt = Runtime(store=MemoryStore())

    # Reads batch_size and idempotency_field from the OnEvent trigger above.
    consumer = QueueConsumer.for_workflow(rt, queue, process_order)

    log("producer", "Publishing 4 messages for 3 distinct orders")
    queue.publish({"order_id": "A-1", "sku": "widget"})
    queue.publish({"order_id": "A-2", "sku": "gizmo"})
    queue.publish({"order_id": "A-1", "sku": "widget"})  # duplicate delivery
    queue.publish({"order_id": "A-3"})  # no SKU — will fail in the workflow

    report = await consumer.poll_once()
    log("consumer", f"Handled {report.handled} message(s)")
    log("consumer", f"Started {len(set(report.submitted))} distinct run(s)")

    # Let the background runs settle before reading their outcomes.
    await asyncio.sleep(0.1)

    header("RUNS")
    for record in await rt.list_runs(workflow="process_order"):
        detail = record.error.message if record.error else record.output
        log("run", f"{record.run_id[:12]}… {record.status.value:<10} {detail}")

    header("WHAT HAPPENED")
    log("note", "4 messages, 3 runs — the duplicate A-1 message resolved")
    log("note", "to the run that already existed, via its idempotency key.")
    log("note", "A-3 failed inside the workflow: that is a recorded failure,")
    log("note", "not lost work, so it is not redelivered.")

    await rt.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
