"""Workflow: Meeting Prep & Follow-Up.

Before a meeting: find it, research who is coming, write a brief, post it.
After: wait for the transcript, pull out what was agreed, and file the tasks —
once somebody has confirmed they are the right tasks.

What this shows, and why each part is the way it is:

* **The transcript is real now.** The version this replaced parked on a
  ``meeting_transcript`` event that nothing in the world would ever send. Meet
  reports its own artifacts, and a transcript is a **Google Doc** — so it has
  no bytes to download and ``drive_export_file`` is the call, not
  ``drive_download_file``. That distinction is a 403 that reads as a
  permissions problem if you get it wrong.

* **``MeetRecording.is_ready`` is why the wait is still needed.** Meet reports a
  transcript the moment the meeting ends and the Drive file appears later, so
  the workflow sleeps and re-checks rather than assuming. The wait is durable:
  the run costs nothing while it holds.

* **The calendar *view*, not the events list.** ``calendar_list_events`` sends
  ``singleEvents=True``, so a weekly meeting comes back as the occurrence on
  Tuesday rather than the series master. Asked the other way, "what is on
  Tuesday" silently misses every recurring meeting and returns a plausible
  short list.

* **Fan-out is capped.** ``ctx.map(..., max_concurrency=3)`` over attendees.

* **Tasks are confirmed before they are filed.** An action item a model
  invented, filed against a named person with a due date, is worse than no
  action item — it is a commitment somebody now has to decline.

Credentials: ``GOOGLE_*`` (Calendar + Meet + Drive), ``EXA_API_KEY``,
``SLACK_BOT_TOKEN``, ``ASANA_ACCESS_TOKEN``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from loom import Context, OnError, Retry, step, workflow
from loom.nodes.agentic import ExtractStructuredIn, SummarizeIn
from loom.nodes.human import ReviewIn
from loom.security.grants import GrantSet
from loom.toolsets.asana.tools import asana_create_task
from loom.toolsets.exa.tools import exa_answer
from loom.toolsets.google.calendar.tools import calendar_list_events
from loom.toolsets.google.drive.tools import drive_export_file
from loom.toolsets.google.meet.tools import meet_list_transcripts
from loom.toolsets.slack.tools import slack_post_message
from loom.triggers import Schedule

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class MeetingConfig(BaseModel):
    """Which meeting, and where the brief and the tasks go."""

    calendar_id: str = "primary"
    window_start: str = Field(default="", description="RFC3339. Empty means now.")
    window_end: str = Field(default="", description="RFC3339 end of the window.")
    brief_channel: str = "#meetings"
    asana_workspace: str = ""
    transcript_wait_hours: int = 6
    """How long to keep re-checking Meet for the transcript.

    Meet reports a transcript when the meeting ends and the Drive file appears
    later, so this is a poll with a ceiling rather than a single look."""

    reviewer: str = ""


class Attendee(BaseModel):
    """One person, as the web describes them."""

    email: str
    background: str = ""


class Meeting(BaseModel):
    """The occurrence, not the series."""

    event_id: str
    title: str = ""
    start: str = ""
    attendees: list[str] = Field(default_factory=list)
    conference_id: str = ""
    """Meet's own id for the call. Empty when the event has no Meet link, which
    is why the follow-up half is skipped rather than failing."""


class FollowUp(BaseModel):
    """One thing somebody agreed to do."""

    description: str
    assignee_email: str = ""
    due_on: str = ""


class MeetingOutcome(BaseModel):
    """What the workflow returns."""

    event_id: str = ""
    title: str = ""
    attendees_researched: int = 0
    brief_posted: bool = False
    transcript_found: bool = False
    tasks_filed: int = 0
    tasks_declined: int = 0


#: What ``agent.extract_structured`` is asked to find in a transcript.
FOLLOWUP_FIELDS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "assignee_email": {"type": "string"},
                    "due_on": {"type": "string", "description": "YYYY-MM-DD"},
                },
            },
        }
    },
}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=2.0))
async def next_meeting(calendar_id: str, time_min: str, time_max: str) -> Meeting | None:
    """The next occurrence in the window, or ``None``.

    ``calendar_list_events`` expands recurring series into occurrences. Reading
    the events list instead would return the series *master* for a weekly
    meeting — an event with the wrong date, which nothing downstream would
    flag.
    """
    events = await calendar_list_events(
        calendar_id=calendar_id, time_min=time_min, time_max=time_max, max_results=1
    )
    if not events:
        return None
    event = events[0]
    return Meeting(
        event_id=event.id,
        title=event.summary,
        start=event.start,
        attendees=[a for a in event.attendees if a],
        conference_id=event.conference_id or "",
    )


@step(retry=Retry(max_attempts=2, initial_delay=1.0), on_error=OnError.CONTINUE, fallback=None)
async def research_attendee(email: str) -> Attendee | None:
    """What the web says about one attendee.

    Continue-on-error with an explicit fallback: a brief missing one person's
    background is still a useful brief, where a failed run before a meeting is
    just a meeting nobody prepared for.
    """
    answer = await exa_answer(query=f"Who is {email}? What company and role?")
    return Attendee(email=email, background=answer.answer)


@step(retry=Retry(max_attempts=1))
async def post_brief(channel: str, title: str, brief: str) -> str:
    """Post the brief. Not retried — a retry posts it twice."""
    posted = await slack_post_message(channel=channel, text=f"*{title}*\n\n{brief}")
    return posted.ts


@step(retry=Retry(max_attempts=2, initial_delay=1.0), on_error=OnError.CONTINUE, fallback="")
async def transcript_text(conference_id: str) -> str:
    """The meeting transcript, or ``""`` if it is not ready yet.

    Two facts about Meet are encoded here. A transcript is reported before its
    Drive file exists, so an empty answer means "not yet" rather than "never".
    And a transcript **is a Google Doc** — it has no bytes, so it is *exported*
    rather than downloaded; ``drive_download_file`` on one is a 403 that reads
    as a permissions problem.
    """
    transcripts = await meet_list_transcripts(conference_record=conference_id)
    if not transcripts:
        return ""
    document_id = transcripts[0].document_id
    if not document_id:
        return ""
    exported = await drive_export_file(file_id=document_id, export_mime="text/plain")
    return (exported.data or b"").decode("utf-8", errors="replace")


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def file_task(workspace: str, item: FollowUp) -> str:
    """File one follow-up.

    Asana has no idempotency key, so creating a task is deliberately the kind
    of write the toolset does not retry on its own — this retries because the
    surrounding approval means a duplicate is visible and cheap to delete,
    which is a different bargain from a mail send.
    """
    task = await asana_create_task(
        title=item.description,
        workspace_gid=workspace,
        due_on=item.due_on,
        notes=f"From the meeting follow-up. Owner: {item.assignee_email or 'unassigned'}",
    )
    return task.gid


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="meeting_lifecycle",
    version="2",
    triggers=[Schedule(cron="*/30 8-18 * * 1-5", timezone="UTC")],
    grants=GrantSet(
        toolsets=["google_calendar", "google_meet", "google_drive", "exa", "slack", "asana"]
    ),
)
async def meeting_lifecycle(ctx: Context, config: MeetingConfig) -> MeetingOutcome:
    """Prepare for the next meeting, then follow it up once it has happened."""
    meeting = await ctx.step(
        next_meeting, config.calendar_id, config.window_start, config.window_end
    )
    if meeting is None:
        return MeetingOutcome()

    # Capped: a twelve-person meeting is twelve searches, three at a time.
    researched = await ctx.map(research_attendee, meeting.attendees, max_concurrency=3)
    found = [person for person in researched if person is not None]

    brief = await ctx.node(
        "agent.summarize",
        SummarizeIn(
            text="\n\n".join(f"{p.email}: {p.background}" for p in found),
            style="a short brief, one line per person",
            focus="who these people are and what they will care about",
        ),
    )
    await ctx.step(post_brief, config.brief_channel, meeting.title, brief.summary)

    outcome = MeetingOutcome(
        event_id=meeting.event_id,
        title=meeting.title,
        attendees_researched=len(found),
        brief_posted=True,
    )

    # No Meet link means no transcript is coming. Reporting that is better than
    # polling for six hours against a conference that does not exist.
    if not meeting.conference_id:
        return outcome

    # Durable poll: the run parks between checks and costs nothing while it
    # holds. Meet reports a transcript before its Drive file exists, so an
    # empty answer means "not yet".
    text = ""
    for _ in range(config.transcript_wait_hours):
        text = await ctx.step(transcript_text, meeting.conference_id)
        if text:
            break
        await ctx.sleep(3600)

    if not text:
        return outcome

    extracted = await ctx.node(
        "agent.extract_structured",
        ExtractStructuredIn(
            text=text[:40_000],
            fields=FOLLOWUP_FIELDS,
            instructions=(
                "Only things somebody actually committed to. Not topics "
                "discussed, not ideas raised — commitments."
            ),
        ),
    )
    items = [
        FollowUp(**{k: str(v) for k, v in raw.items() if k in FollowUp.model_fields})
        for raw in extracted.values.get("items", [])
        if isinstance(raw, dict) and raw.get("description")
    ]
    if not items:
        return outcome.model_copy(update={"transcript_found": True})

    # A task a model invented, filed against a named person with a due date, is
    # a commitment somebody now has to decline. Confirm first.
    review = await ctx.node(
        "human.review_edit",
        ReviewIn(
            subject=f"follow-ups:{meeting.event_id}",
            draft="\n".join(
                f"- {i.description} ({i.assignee_email or 'unassigned'}, {i.due_on or 'no date'})"
                for i in items
            ),
            prompt="File these? Remove anything nobody actually committed to.",
            assignees=[config.reviewer] if config.reviewer else [],
        ),
    )
    if not review.approved:
        return outcome.model_copy(
            update={"transcript_found": True, "tasks_declined": len(items)}
        )

    filed = 0
    for item in items:
        await ctx.step(file_task, config.asana_workspace, item)
        filed += 1

    return outcome.model_copy(
        update={"transcript_found": True, "tasks_filed": filed}
    )
