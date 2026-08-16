"""Example 26 — OneDrive and SharePoint: one API, two grants.

A document arrives in someone's OneDrive; the workflow files it into a team's
SharePoint library and records a row about it in a tracking list.

What this shows:

* **A drive is a drive.** ``onedrive_download_file`` and
  ``sharepoint_upload_file`` exchange the same ``DriveItem``/``Attachment``
  types, because a SharePoint document library *is* a Graph ``drive``. The
  hand-off below needs no translation step, and that is the design, not luck.
* **Resolve the vocabulary before writing to it.** A SharePoint list item's
  values are keyed by a column's *internal* name, which is not what the site
  displays. Writing "Due Date" where ``DueDate`` belongs is **accepted and sets
  nothing** — the row appears, the workflow reports success, the value is
  missing. So the workflow resolves columns first and reports what it mapped.
* **Coverage is part of the answer.** The listing step reads ``.complete`` at
  the call site and puts it in its output, because ``Results`` degrades to a
  plain list once journaled.
* **Two toolsets, two grants.** ``GrantSet`` below lets this workflow read
  OneDrive and write SharePoint, and nothing else — the reason these ship as
  separate toolsets rather than one "Microsoft" bundle.

Runs end to end with **no credentials**: without them the steps below report
what they would have done. Set the Microsoft variables to run it for real.

Run:
    python3 examples/cookbook/26_onedrive_sharepoint.py

    # for real:
    export MS_TENANT_ID=... MS_CLIENT_ID=... MS_CLIENT_SECRET=...
    export MS_REFRESH_TOKEN=...          # act as a person, so /me works
    export MS_SHAREPOINT_SITE='contoso.sharepoint.com:/teams/hr'
"""

from __future__ import annotations

import os

from utils import box, header, log

from loom import Context, Runtime, step, workflow
from loom.security.grants import GrantSet
from loom.stores.memory import MemoryStore


def configured() -> bool:
    """Whether real Microsoft credentials are present."""
    return bool(
        os.environ.get("MS_GRAPH_ACCESS_TOKEN")
        or (os.environ.get("MS_TENANT_ID") and os.environ.get("MS_CLIENT_SECRET"))
    )


# ---------------------------------------------------------------------------
# Read from OneDrive
# ---------------------------------------------------------------------------


@step
async def find_recent_documents(folder: str, limit: int) -> dict:
    """List a OneDrive folder, newest first.

    Wrapped in a step of our own rather than called straight from the workflow
    body, so ``.complete`` is read *here* and put into the output. ``Results``
    degrades to a plain list when journaled, so a body-level read would be
    right on the first run and gone on replay.

    Args:
        folder: Path relative to the drive root. "" lists the root.
        limit: How many items to gather across pages.
    """
    from loom.toolsets.microsoft.onedrive.tools import onedrive_list_children

    found = await onedrive_list_children(
        path=folder, limit=limit, order_by="lastModifiedDateTime desc"
    )
    return {
        "files": [
            {"id": item.id, "name": item.name, "size": item.size}
            for item in found
            if not item.is_folder
        ],
        # True means the folder ran out, so a short answer is not ambiguous.
        "complete": found.complete,
    }


# ---------------------------------------------------------------------------
# Resolve the SharePoint vocabulary, then write
# ---------------------------------------------------------------------------


@step
async def resolve_columns(list_id: str, wanted: list[str]) -> dict:
    """Map display names a human would write to the internal keys Graph needs.

    The failure this prevents is the quiet one: SharePoint accepts a create
    whose ``fields`` use display names and simply does not set those columns.
    Nothing errors. The row exists and is blank.

    Args:
        list_id: The tracking list.
        wanted: Column names as a person would say them, e.g. "Due Date".
    """
    from loom.toolsets.microsoft.sharepoint.tools import sharepoint_list_columns

    columns = await sharepoint_list_columns(list_id)
    by_display = {c.display_name.casefold(): c for c in columns}

    mapping: dict[str, str] = {}
    unmatched: list[str] = []
    for label in wanted:
        column = by_display.get(label.casefold())
        # A column that is read-only silently ignores writes too, so it counts
        # as unmatched rather than being mapped to a key that cannot be set.
        if column is None or column.read_only:
            unmatched.append(label)
        else:
            mapping[label] = column.name
    return {"mapping": mapping, "unmatched": unmatched}


@step
async def file_into_library(filename: str, item_id: str, destination: str) -> dict:
    """Move a document from OneDrive into a SharePoint library.

    One ``Attachment`` crosses between the two toolsets untranslated — the
    point of the shared model layer.

    Args:
        filename: Name to store it under in the library.
        item_id: The OneDrive item to copy.
        destination: Folder path inside the library.
    """
    from loom.toolsets.microsoft.onedrive.tools import onedrive_download_file
    from loom.toolsets.microsoft.sharepoint.tools import sharepoint_upload_file

    attachment = await onedrive_download_file(item_id=item_id)
    stored = await sharepoint_upload_file(
        filename, attachment.data, parent_path=destination
    )
    return {"id": stored.id, "name": stored.name, "url": stored.web_url}


@step
async def record_in_tracking_list(list_id: str, fields: dict) -> dict:
    """Add a row to the tracking list.

    Not retried by the toolset: SharePoint has no idempotency key for a list
    item, so a retry after a timeout would add a second row.

    Args:
        list_id: The tracking list.
        fields: Values keyed by INTERNAL column name, from resolve_columns.
    """
    from loom.toolsets.microsoft.sharepoint.tools import sharepoint_create_list_item

    item = await sharepoint_create_list_item(list_id, fields)
    return {"id": item.id, "fields": item.fields}


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------


@workflow(
    name="file_documents",
    # Exactly what this workflow may reach. Two toolsets, two grants — which is
    # why they are not one "Microsoft" bundle.
    grants=GrantSet(toolsets=["onedrive:read", "sharepoint:read", "sharepoint:write"]),
)
async def file_documents(ctx: Context, params: dict) -> dict:
    """File recent OneDrive documents into a team library and log them."""
    folder = params.get("folder", "")
    list_id = params.get("list_id", "Documents Tracker")
    destination = params.get("destination", "Incoming")

    if not configured():
        await ctx.report("no Microsoft credentials — describing the plan instead")
        return {
            "dry_run": True,
            "would_read": f"/me/drive/root:/{folder}" if folder else "/me/drive/root",
            "would_write": f"{destination}/ in the site's default library",
        }

    listing = await ctx.step(find_recent_documents, folder, params.get("limit", 10))
    await ctx.report(f"found {len(listing['files'])} documents")

    # Resolve before writing, once, and carry the mapping forward.
    resolved = await ctx.step(
        resolve_columns, list_id, ["Title", "Source File", "Size"]
    )
    if resolved["unmatched"]:
        # Naming what is missing beats writing a row with holes in it.
        await ctx.report(f"unmapped columns: {', '.join(resolved['unmatched'])}")

    mapping = resolved["mapping"]
    filed = []
    for document in listing["files"]:
        stored = await ctx.step(
            file_into_library, document["name"], document["id"], destination
        )
        if "Title" in mapping:
            await ctx.step(
                record_in_tracking_list,
                list_id,
                {
                    mapping["Title"]: document["name"],
                    **(
                        {mapping["Size"]: document["size"]}
                        if "Size" in mapping
                        else {}
                    ),
                },
            )
        filed.append(stored)

    return {
        "dry_run": False,
        "filed": filed,
        "complete": listing["complete"],
        "columns_resolved": mapping,
    }


async def main() -> None:
    rt = Runtime(store=MemoryStore())
    rt.register(file_documents)

    header("FILING DOCUMENTS INTO A TEAM LIBRARY")
    result = await rt.run(file_documents, {"folder": "Inbox", "limit": 5})
    output = result.output or {}

    if output.get("dry_run"):
        log("mode", "dry run — no Microsoft credentials configured")
        log("would read", output["would_read"])
        log("would write", output["would_write"])
    else:
        for entry in output["filed"]:
            log("filed", f"{entry['name']} -> {entry['url']}")
        log("coverage", f"complete={output['complete']}")

        header("WHAT THE COLUMN NAMES ACTUALLY ARE")
        for label, internal in output["columns_resolved"].items():
            log("resolved", f"{label!r} -> {internal!r}")

    header("THE JOURNAL RECORDS EACH DURABLE CALL")
    for entry in await rt.store.load_journal(result.run_id):
        log("journal", f"{entry.path:<5} {entry.kind.value:<6} {entry.name}")

    box(
        "A SharePoint document library IS a Graph drive, so the download and\n"
        "the upload exchange one Attachment with no translation between them.\n"
        "What does need translating is the column vocabulary: writing a\n"
        "display name where an internal name belongs is accepted, sets\n"
        "nothing, and reports success — so resolve it once, up front.",
        "why this workflow resolves before it writes",
    )


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    raise SystemExit(run_main(main()))
