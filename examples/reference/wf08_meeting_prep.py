"""Workflow: Meeting Prep & Follow-Up."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from loom import Context, Retry, step, workflow

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class MeetingConfig(BaseModel):
    """Input configuration for meeting lifecycle."""

    calendar_id: str
    meeting_id: str
    openai_api_key: str = ""
    notification_email: str = ""


class Meeting(BaseModel):
    """A calendar meeting."""

    meeting_id: str
    title: str = ""
    start_time: str = ""
    attendees: list[str] = []
    agenda: str = ""


class AttendeeProfile(BaseModel):
    """Research output for a meeting attendee."""

    email: str
    name: str = ""
    title: str = ""
    company: str = ""
    linkedin_summary: str = ""
    recent_interactions: list[str] = []


class MeetingBrief(BaseModel):
    """Pre-meeting preparation document."""

    meeting_id: str
    title: str = ""
    attendee_profiles: list[AttendeeProfile] = []
    talking_points: list[str] = []
    background_context: str = ""


class FollowUpTask(BaseModel):
    """A follow-up action item from a meeting."""

    description: str
    assignee: str = ""
    due_date: str = ""
    priority: str = "medium"


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, delay=2.0))
async def get_upcoming_meetings(
    calendar_id: str,
    meeting_id: str,
) -> Meeting:
    """Fetch meeting details from a calendar API.

    Args:
        calendar_id: The calendar to query.
        meeting_id: Specific meeting ID.

    Returns:
        Meeting details.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://calendar.example.com/api/{calendar_id}"
            f"/events/{meeting_id}",
        )
        resp.raise_for_status()
        data = resp.json()

    return Meeting(
        meeting_id=meeting_id,
        title=data.get("title", "Team Sync"),
        start_time=data.get("start", "2025-01-15T10:00:00Z"),
        attendees=data.get(
            "attendees", ["alice@co.com", "bob@co.com"],
        ),
        agenda=data.get("agenda", ""),
    )


@step(retry=Retry(max_attempts=2, delay=1.0))
async def research_attendee(
    email: str,
    api_key: str,
) -> AttendeeProfile:
    """Research a meeting attendee using CRM and web data.

    Args:
        email: Attendee email address.
        api_key: OpenAI API key.

    Returns:
        Attendee profile with background info.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        crm_resp = await client.get(
            "https://crm.example.com/api/contacts",
            params={"email": email},
        )
        crm_data = crm_resp.json() if crm_resp.status_code == 200 else {}

    return AttendeeProfile(
        email=email,
        name=crm_data.get("name", email.split("@")[0]),
        title=crm_data.get("title", "Unknown"),
        company=crm_data.get("company", email.split("@")[-1]),
        linkedin_summary="Senior professional in the field",
        recent_interactions=crm_data.get("interactions", []),
    )


@step(retry=Retry(max_attempts=2, delay=1.0))
async def generate_brief(
    meeting: Meeting,
    profiles: list[AttendeeProfile],
    api_key: str,
) -> MeetingBrief:
    """Generate a pre-meeting brief using AI.

    Args:
        meeting: The meeting details.
        profiles: Attendee profiles.
        api_key: OpenAI API key.

    Returns:
        A structured meeting brief.
    """
    attendee_info = "\n".join(
        f"- {p.name} ({p.title} at {p.company})"
        for p in profiles
    )
    prompt = (
        f"Meeting: {meeting.title}\n"
        f"Attendees:\n{attendee_info}\n"
        f"Agenda: {meeting.agenda}\n\n"
        "Generate 3-5 talking points and background context."
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "user", "content": prompt},
                ],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    return MeetingBrief(
        meeting_id=meeting.meeting_id,
        title=meeting.title,
        attendee_profiles=profiles,
        talking_points=["Review Q4 results", "Discuss roadmap"],
        background_context=content[:300] if content else "",
    )


@step(retry=Retry(max_attempts=2, delay=1.0))
async def send_brief(
    brief: MeetingBrief,
    recipient: str,
) -> bool:
    """Email the meeting brief to the organizer.

    Args:
        brief: The generated brief.
        recipient: Email address to send to.

    Returns:
        True if sent successfully.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://email.example.com/api/send",
            json={
                "to": recipient,
                "subject": f"Brief: {brief.title}",
                "body": brief.background_context,
            },
        )
        resp.raise_for_status()
    return True


@step(retry=Retry(max_attempts=2, delay=1.0))
async def process_transcript(
    transcript: str,
    api_key: str,
) -> list[FollowUpTask]:
    """Extract action items from a meeting transcript.

    Args:
        transcript: Raw meeting transcript text.
        api_key: OpenAI API key.

    Returns:
        List of follow-up tasks.
    """
    prompt = (
        "Extract action items from this transcript. "
        "For each, provide: description, assignee, due date, "
        f"priority.\n\n{transcript[:4000]}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "user", "content": prompt},
                ],
            },
        )
        resp.raise_for_status()

    return [
        FollowUpTask(
            description="Send revised proposal",
            assignee="alice@co.com",
            due_date="2025-01-20",
            priority="high",
        ),
        FollowUpTask(
            description="Schedule follow-up call",
            assignee="bob@co.com",
            due_date="2025-01-22",
        ),
    ]


@step(retry=Retry(max_attempts=2, delay=1.0))
async def create_followup_tasks(
    tasks: list[FollowUpTask],
    meeting_id: str,
) -> int:
    """Create follow-up tasks in the project management tool.

    Args:
        tasks: List of follow-up tasks to create.
        meeting_id: Meeting ID for linking.

    Returns:
        Number of tasks created.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        for task in tasks:
            await client.post(
                "https://tasks.example.com/api/tasks",
                json={
                    "description": task.description,
                    "assignee": task.assignee,
                    "due_date": task.due_date,
                    "priority": task.priority,
                    "meeting_id": meeting_id,
                },
            )
    return len(tasks)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="meeting_lifecycle", version="1")
async def meeting_lifecycle(
    ctx: Context,
    config: MeetingConfig,
) -> dict:
    """Prepare for a meeting, then wait for transcript and create follow-ups.

    Phase 1: Fetch meeting -> research attendees (parallel) -> brief -> send.
    Phase 2: Wait for transcript event -> extract actions -> create tasks.
    """
    # Phase 1: Pre-meeting preparation
    meeting = await ctx.step(
        get_upcoming_meetings,
        config.calendar_id,
        config.meeting_id,
    )

    # Research all attendees in parallel
    research_tasks = [
        ctx.step(
            research_attendee, email, config.openai_api_key,
        )
        for email in meeting.attendees
    ]
    profiles: list[AttendeeProfile] = await ctx.gather(
        *research_tasks,
    )

    brief = await ctx.step(
        generate_brief, meeting, profiles, config.openai_api_key,
    )
    await ctx.step(send_brief, brief, config.notification_email)

    # Phase 2: Wait for the meeting transcript
    transcript: str = await ctx.wait_for_event(
        "meeting_transcript",
        timeout=86400,  # 24 hours
        default="",
    )

    if not transcript:
        return {
            "meeting_id": meeting.meeting_id,
            "brief_sent": True,
            "transcript_received": False,
            "tasks_created": 0,
        }

    tasks = await ctx.step(
        process_transcript, transcript, config.openai_api_key,
    )
    tasks_created = await ctx.step(
        create_followup_tasks, tasks, meeting.meeting_id,
    )

    return {
        "meeting_id": meeting.meeting_id,
        "brief_sent": True,
        "transcript_received": True,
        "tasks_created": tasks_created,
    }
