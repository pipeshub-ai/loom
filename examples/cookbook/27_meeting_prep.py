"""Example 27 — Meeting prep across Outlook, OneNote, and Teams.

Before a meeting: find it, gather the mail thread behind it, write an agenda
page, and post the link to the team's channel.

What this shows:

* **The calendar view, not the events list.** ``outlook_list_calendar_view``
  expands recurring series into occurrences over a window;
  ``outlook_list_events`` would return the *series master* for a weekly
  meeting, so "what is on Tuesday" would silently miss it. This is the same
  distinction ``singleEvents=True`` makes for Google Calendar, and it is the
  single easiest thing to get wrong in this API.
* **Resolve the vocabulary before writing.** A OneNote page's title lives
  inside its HTML ``<title>``; posting a bare fragment creates an untitled
  page and reports success. The step below builds the document properly.
* **Four toolsets, four grants.** ``GrantSet`` lets this workflow read the
  calendar and mail, write a note, and post to Teams — and nothing else. That
  is why Outlook ships as two toolsets rather than one.
* **Acceptance is not delivery.** Nothing here claims a message was received;
  the Teams post returns the message it created, and a mail send would return
  only that Graph accepted it.

Runs end to end with **no credentials**: without them each step reports what it
would have done. Set the Microsoft variables to run it for real.

Run:
    python3 examples/cookbook/27_meeting_prep.py

    # for real:
    export MS_TENANT_ID=... MS_CLIENT_ID=... MS_CLIENT_SECRET=...
    export MS_REFRESH_TOKEN=...          # act as a person, so /me works
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

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
# Read the calendar — the *view*, not the events list
# ---------------------------------------------------------------------------


@step
async def next_meetings(start: str, end: str, limit: int) -> dict:
    """Find what is actually on the calendar between two instants.

    Wrapped in a step of our own so ``.complete`` is read here and put into the
    output: ``Results`` degrades to a plain list once journaled, so a
    body-level read would be right on the first run and gone on replay.

    Args:
        start: ISO-8601 window start, with an offset.
        end: ISO-8601 window end, with an offset.
        limit: How many events to gather.
    """
    from loom.toolsets.microsoft.outlook.calendar.tools import (
        outlook_list_calendar_view,
    )

    found = await outlook_list_calendar_view(start, end, limit=limit)
    return {
        "events": [
            {
                "id": e.id,
                "subject": e.subject,
                "start": e.start,
                "attendees": [a.address for a in e.attendees],
                # Present only because the view expanded a series; the events
                # listing would have returned the master instead.
                "from_series": bool(e.series_master_id),
            }
            for e in found
            if not e.is_cancelled
        ],
        "complete": found.complete,
    }


@step
async def thread_for(subject: str, limit: int) -> list[dict]:
    """Find the mail behind a meeting, by subject.

    Args:
        subject: The meeting subject to search for.
        limit: Maximum messages.
    """
    from loom.toolsets.microsoft.outlook.mail.tools import outlook_search_messages

    found = await outlook_search_messages(subject, limit=limit)
    return [
        {"from": m.from_.address, "subject": m.subject, "preview": m.body_preview[:120]}
        for m in found
    ]


# ---------------------------------------------------------------------------
# Write the agenda
# ---------------------------------------------------------------------------


@step
async def write_agenda(section_id: str, title: str, points: list[str]) -> dict:
    """Create a OneNote page for the meeting.

    ``title`` and the body are passed separately because OneNote reads the page
    title from the HTML document's ``<title>`` — the tool assembles the
    document, so a caller cannot accidentally post a fragment and get an
    untitled page.

    Args:
        section_id: The OneNote section to write into.
        title: Page title.
        points: Agenda items.
    """
    from loom.toolsets.microsoft.onenote.tools import onenote_create_page

    body = "<ul>" + "".join(f"<li>{p}</li>" for p in points) + "</ul>"
    page = await onenote_create_page(section_id, title, body)
    return {"id": page.id, "title": page.title, "url": page.web_url}


@step
async def post_agenda(team_id: str, channel_id: str, title: str, url: str) -> dict:
    """Post the agenda link to a Teams channel.

    Needs delegated credentials — Graph does not support application
    permissions for sending a Teams message.

    Args:
        team_id: The team the channel is in.
        channel_id: The channel to post to.
        title: The agenda page's title.
        url: The agenda page's URL.
    """
    from loom.toolsets.microsoft.teams.tools import teams_send_channel_message

    sent = await teams_send_channel_message(
        team_id,
        channel_id,
        f'Agenda for <b>{title}</b>: <a href="{url}">{url}</a>',
    )
    return {"id": sent.id, "url": sent.web_url}


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------


@workflow(
    name="meeting_prep",
    # Exactly what this workflow may reach. Reading the calendar and mail,
    # writing a note, posting to a channel — and nothing else.
    grants=GrantSet(
        toolsets=[
            "outlook_calendar:read",
            "outlook_mail:read",
            "onenote:write",
            "teams:write",
        ]
    ),
)
async def meeting_prep(ctx: Context, params: dict) -> dict:
    """Prepare for the next meetings on the calendar."""
    hours = params.get("hours_ahead", 24)
    now = ctx.now()
    start = now.isoformat()
    end = (now + timedelta(hours=hours)).isoformat()

    if not configured():
        await ctx.report("no Microsoft credentials — describing the plan instead")
        return {
            "dry_run": True,
            # Naming the endpoint is the point: the *view* is what expands
            # recurring meetings into the occurrences in this window.
            "would_read": f"/me/calendarView from {start} to {end}",
            "would_write": "a OneNote agenda page, then a Teams channel post",
        }

    found = await ctx.step(next_meetings, start, end, params.get("limit", 5))
    await ctx.report(f"{len(found['events'])} meetings in the next {hours}h")

    prepared = []
    for event in found["events"]:
        thread = await ctx.step(thread_for, event["subject"], 5)
        points = [f"From {m['from']}: {m['preview']}" for m in thread] or [
            "No prior thread found."
        ]
        page = await ctx.step(
            write_agenda, params["section_id"], f"Agenda — {event['subject']}", points
        )
        if params.get("team_id") and params.get("channel_id"):
            await ctx.step(
                post_agenda,
                params["team_id"],
                params["channel_id"],
                page["title"],
                page["url"],
            )
        prepared.append({"meeting": event["subject"], "agenda": page["url"]})

    return {"dry_run": False, "prepared": prepared, "complete": found["complete"]}


async def main() -> None:
    rt = Runtime(store=MemoryStore())
    rt.register(meeting_prep)

    header("PREPARING FOR THE NEXT MEETINGS")
    result = await rt.run(
        meeting_prep, {"hours_ahead": 24, "section_id": "SECTION", "limit": 5}
    )
    output = result.output or {}

    if output.get("dry_run"):
        log("mode", "dry run — no Microsoft credentials configured")
        log("would read", output["would_read"])
        log("would write", output["would_write"])
    else:
        for entry in output["prepared"]:
            log("prepared", f"{entry['meeting']} -> {entry['agenda']}")
        log("coverage", f"complete={output['complete']}")

    header("THE JOURNAL RECORDS EACH DURABLE CALL")
    for entry in await rt.store.load_journal(result.run_id):
        log("journal", f"{entry.path:<5} {entry.kind.value:<6} {entry.name}")

    box(
        "calendarView expands a recurring series into the occurrences in a\n"
        "window; the events listing returns the series master instead. So\n"
        "'what is on Tuesday' asked over /events misses every recurring\n"
        "meeting and returns a short list that looks perfectly valid — which\n"
        "is why this workflow reads the view.",
        "the one call worth getting right",
    )
    print(f"\n  (window computed from ctx.now(): {datetime.now(UTC).date()})")


if __name__ == "__main__":
    from loom.runtime.shutdown import run_main

    raise SystemExit(run_main(main()))
