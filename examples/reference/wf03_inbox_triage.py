"""Workflow: Email Classification & Routing."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from workflow_builder import Context, Retry, step, workflow

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class TriageConfig(BaseModel):
    """Input configuration for inbox triage."""

    mailbox: str = "inbox"
    max_emails: int = 25
    slack_channel: str = "#support-triage"


class EmailCategory(StrEnum):
    """Categories the classifier can assign to an email."""

    URGENT = "urgent"
    MEETING = "meeting"
    SUPPORT = "support"
    NEWSLETTER = "newsletter"
    SPAM = "spam"


class RawEmail(BaseModel):
    """An email fetched from the mailbox."""

    message_id: str
    sender: str
    subject: str
    body: str
    received_at: str


class ClassifiedEmail(BaseModel):
    """An email with an AI-assigned category."""

    email: RawEmail
    category: EmailCategory
    confidence: float
    summary: str


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def fetch_emails(
    mailbox: str,
    max_emails: int,
) -> list[RawEmail]:
    """Fetch unread emails from the mail provider API."""
    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            "https://api.mail.example/v1/messages",
            params={
                "folder": mailbox,
                "unread": True,
                "limit": max_emails,
            },
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])

    return [
        RawEmail(
            message_id=m["id"],
            sender=m["from"],
            subject=m["subject"],
            body=m.get("body", ""),
            received_at=m.get("date", ""),
        )
        for m in messages
    ]


@step(retry=Retry(max_attempts=2))
async def classify_email(email: RawEmail) -> ClassifiedEmail:
    """Use an LLM to classify and summarise an email."""
    import httpx

    prompt = (
        f"Classify this email into one of: "
        f"{', '.join(c.value for c in EmailCategory)}.\n\n"
        f"Subject: {email.subject}\n"
        f"Body: {email.body[:500]}\n\n"
        f"Return JSON with category, confidence, summary."
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.example/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        data = resp.json()["choices"][0]["message"]

    import json

    parsed = json.loads(data["content"])
    return ClassifiedEmail(
        email=email,
        category=EmailCategory(parsed.get("category", "support")),
        confidence=float(parsed.get("confidence", 0.5)),
        summary=parsed.get("summary", email.subject),
    )


@step(retry=Retry(max_attempts=3, initial_delay=2.0))
async def route_to_slack(
    classified: ClassifiedEmail,
    channel: str,
) -> bool:
    """Post the classified email summary to Slack."""
    import httpx

    icon = {
        EmailCategory.URGENT: "rotating_light",
        EmailCategory.MEETING: "calendar",
        EmailCategory.SUPPORT: "ticket",
        EmailCategory.NEWSLETTER: "newspaper",
        EmailCategory.SPAM: "wastebasket",
    }.get(classified.category, "email")

    text = (
        f":{icon}: *{classified.category.value.upper()}* "
        f"({classified.confidence:.0%})\n"
        f"From: {classified.email.sender}\n"
        f"Subject: {classified.email.subject}\n"
        f"Summary: {classified.summary}"
    )

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://hooks.slack.example/services/T00/B00/xxx",
            json={"channel": channel, "text": text},
        )
        resp.raise_for_status()

    return True


@step(retry=Retry(max_attempts=2))
async def create_calendar_events(
    meetings: list[ClassifiedEmail],
) -> int:
    """Create calendar events for emails classified as meetings."""
    import httpx

    created = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for item in meetings:
            resp = await client.post(
                "https://api.calendar.example/v1/events",
                json={
                    "summary": item.email.subject,
                    "description": item.summary,
                    "attendees": [item.email.sender],
                },
            )
            if resp.is_success:
                created += 1

    return created


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="inbox_triage", version="1")
async def inbox_triage(
    ctx: Context,
    config: TriageConfig,
) -> dict[str, object]:
    """Fetch unread emails, classify with AI, and route."""
    emails = await ctx.step(
        fetch_emails, config.mailbox, config.max_emails,
    )
    if not emails:
        return {"total": 0, "classified": 0, "meetings_created": 0}

    # Classify all emails in parallel
    classified: list[ClassifiedEmail] = await ctx.gather(
        *[ctx.step(classify_email, email) for email in emails],
    )

    # Route non-spam emails to Slack in parallel
    actionable = [c for c in classified if c.category != EmailCategory.SPAM]
    if actionable:
        await ctx.gather(
            *[
                ctx.step(route_to_slack, c, config.slack_channel)
                for c in actionable
            ],
        )

    # Create calendar events for meeting emails
    meetings = [
        c for c in classified if c.category == EmailCategory.MEETING
    ]
    meetings_created = 0
    if meetings:
        meetings_created = await ctx.step(
            create_calendar_events, meetings,
        )

    # Build per-category counts
    counts: dict[str, int] = {}
    for c in classified:
        key = c.category.value
        counts[key] = counts.get(key, 0) + 1

    return {
        "total": len(emails),
        "classified": len(classified),
        "meetings_created": meetings_created,
        "category_counts": counts,
    }
