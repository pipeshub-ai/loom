"""Example 17 — Files moving through a workflow, and versioned artifacts.

Two related things:

* **Attachments** carry a file's bytes *and* its name and content type, so a
  downstream step does not have to be told out of band what it is holding.
* **Artifacts** give a file a stable name whose content can change. Blob storage
  is content-addressed and therefore immutable — ``report.md@2`` is the layer
  that lets "the report" mean one thing while its bytes move.

Run:
    python3 examples/cookbook/17_files_and_artifacts.py
"""

from __future__ import annotations

import asyncio
import csv
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log

from workflow_builder import Context, Runtime, step, workflow
from workflow_builder.state.memory import MemoryStore
from workflow_builder.storage.attachment import Attachment
from workflow_builder.storage.blob import BlobService, LocalBlobBackend

# ---------------------------------------------------------------------------
# Steps that produce and consume files
# ---------------------------------------------------------------------------


@step
async def fetch_export(day: str) -> Attachment:
    """Produce a CSV export. Stands in for a download or a report generator."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["sku", "units"])
    for sku, units in [("widget", 12), ("gizmo", 3), ("doodad", 41)]:
        writer.writerow([sku, units])

    # The name and type travel with the bytes.
    return Attachment.from_text(f"sales-{day}.csv", buffer.getvalue(), source="erp")


@step
async def summarise(export: Attachment) -> str:
    """Read the attachment, using the metadata that came with it."""
    rows = list(csv.DictReader(io.StringIO(export.text())))
    total = sum(int(row["units"]) for row in rows)
    return f"{export.filename}: {len(rows)} SKUs, {total} units"


@step
async def render_report(summary: str) -> bytes:
    """Render a report body. Bytes, because that is what gets published."""
    return f"# Daily Sales\n\n{summary}\n".encode()


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="daily_report")
async def daily_report(ctx: Context, day: str) -> str:
    """Fetch an export, summarise it, and publish the report under a stable name."""
    export = await ctx.step(fetch_export, day)
    summary = await ctx.step(summarise, export)

    body = await ctx.step(render_report, summary)
    # Same name every day; the version moves only when the content actually does.
    version = await ctx.put_artifact("daily-report.md", body, mime="text/markdown")

    return f"{summary} -> {version.qualified_name}"


async def main() -> None:
    header("Files and Artifacts")

    with tempfile.TemporaryDirectory() as tmp:
        # Swap LocalBlobBackend for S3BlobBackend in production; nothing else changes.
        blobs = BlobService(LocalBlobBackend(Path(tmp) / "blobs"))
        rt = Runtime(store=MemoryStore(), blobs=blobs)

        header("RUN 1")
        first = await rt.run(daily_report, "monday")
        log("run", str(first.output))

        header("RUN 2 — same content")
        second = await rt.run(daily_report, "monday")
        log("run", str(second.output))
        log("note", "Identical bytes, so it resolved to v1 rather than making v2.")

        header("RUN 3 — content changed")
        # A different day produces a different summary, so a real new version.
        third = await rt.run(daily_report, "tuesday")
        log("run", str(third.output))

        header("VERSION HISTORY")
        for version in await rt.artifacts.history("daily-report.md"):
            log(
                "artifact",
                f"{version.qualified_name}  {version.size:>4}B  "
                f"by {version.created_by_run[:12]}…",
            )

        header("READING AN OLD VERSION")
        original = await rt.artifacts.read("daily-report.md", 1)
        log("v1", original.decode().strip().replace("\n\n", " | "))

        header("REPLAY PINS WHAT IT ORIGINALLY READ")
        replayed = await rt.replay(first.run_id)
        log("replay", str(replayed.output))
        log("note", "Still v1 — a replay rehearses what happened, not what would.")

        await rt.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
