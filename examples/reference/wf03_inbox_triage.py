"""Workflow: Inbox Triage & Smart Routing.

Reads unread mail, classifies each message, routes what matters to Slack,
labels it in Gmail, and puts any meeting it finds on the calendar.

What this shows, and why each part is the way it is:

* **Classification is judgement, so it is an agent node.** The version this
  replaced decided "urgent" by POSTing a prompt and string-matching the reply.
  ``agent.classify`` returns a typed label and a ``confident`` flag, and a
  low-confidence message is routed for a person to look at rather than filed by
  a guess. An invented keyword list — ``if "urgent" in subject.lower()`` — is a
  guess wearing the clothes of logic.

* **Threads, not messages.** Gmail groups by conversation, so labelling one
  message of a thread looks to a person like nothing happened. Routing and
  labelling here work on ``thread_id``.

* **A label is an id, not a name.** ``gmail_modify_labels`` takes ``Label_7``;
  passing "Triaged" applies nothing and reports success. ``gmail_find_label``
  is marked ``resolves="label"`` and is why the resolution happens once, up
  front, instead of per message.

* **Coverage is reported.** ``gmail_search_messages`` returns ``Results``, so
  ``.complete`` says whether the inbox was fully read. A count taken from one
  page and reported as a total is the failure ``Results`` exists to prevent.

* **Read and route only — no human gate.** Nothing here writes outside the
  organisation: a Slack post, a Gmail label, a calendar entry. The one place a
  person is involved is a message the classifier was *not* confident about,
  which is routed to a review channel instead of being filed silently.

Credentials: ``GOOGLE_*`` (Gmail + Calendar), ``SLACK_BOT_TOKEN``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from loom import Context, OnError, Retry, step, workflow
from loom.nodes.agentic import ClassifyIn, ExtractStructuredIn
from loom.security.grants import GrantSet
from loom.toolsets.google.calendar.tools import calendar_create_event
from loom.toolsets.google.gmail.tools import (
    gmail_find_label,
    gmail_modify_thread_labels,
    gmail_search_messages,
)
from loom.toolsets.slack.tools import slack_post_message
from loom.triggers import Poll

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class EmailCategory(StrEnum):
    """Categories the classifier can assign to an email."""

    URGENT = "urgent"
    MEETING = "meeting"
    SUPPORT = "support"
    NEWSLETTER = "newsletter"
    SPAM = "spam"


class TriageConfig(BaseModel):
    """Input configuration for inbox triage."""

    query: str = "is:unread in:inbox"
    """Gmail search syntax. The default is the whole point of the workflow."""

    max_emails: int = 25
    channels: dict[str, str] = Field(
        default_factory=lambda: {
            EmailCategory.URGENT.value: "#ops-urgent",
            EmailCategory.SUPPORT.value: "#support-triage",
            EmailCategory.MEETING.value: "#calendar",
        }
    )
    review_channel: str = "#triage-review"
    """Where a message the classifier was unsure about goes.

    Not the same as a low-priority channel: this is "a person should look",
    which is a different request from "this is a newsletter"."""

    triaged_label: str = "Triaged"
    calendar_id: str = "primary"


class TriagedMessage(BaseModel):
    """One message, after the classifier has read it."""

    message_id: str
    thread_id: str
    subject: str
    sender: str
    category: EmailCategory
    confident: bool


class TriageResult(BaseModel):
    """Summary returned by the workflow."""

    total: int
    complete: bool
    """Whether the whole inbox was read, or the page cap cut it short."""

    routed: int = 0
    needs_review: int = 0
    meetings_created: int = 0
    category_counts: dict[str, int] = Field(default_factory=dict)


#: What ``agent.extract_structured`` is asked to find in a meeting mail.
MEETING_FIELDS = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "What the meeting is about"},
        "start": {"type": "string", "description": "RFC3339 start, e.g. 2026-03-01T15:00:00Z"},
        "end": {"type": "string", "description": "RFC3339 end"},
    },
}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def fetch_unread(query: str, limit: int) -> dict[str, object]:
    """Unread mail, plus whether that is all of it.

    Returns the coverage alongside the rows rather than only the rows: a caller
    that reports ``len(rows)`` as the inbox size is wrong with nothing to
    notice. ``Results.complete`` is what makes the difference visible.
    """
    found = await gmail_search_messages(query=query, max_results=limit)
    return {
        "messages": [
            {
                "id": message.id,
                "thread_id": message.thread_id,
                "subject": message.subject,
                "sender": message.sender,
                "body": (message.body or message.snippet or "")[:4000],
            }
            for message in found
        ],
        "complete": found.complete,
    }


@step(retry=Retry(max_attempts=2, initial_delay=0.5))
async def resolve_label(label_name: str) -> str:
    """The label *id* for a human-readable name, or ``""``.

    Resolved once, before the loop. Gmail accepts an id and silently applies
    nothing for a name, so a workflow that skipped this would report every
    message as labelled and label none of them.
    """
    label = await gmail_find_label(label_name=label_name)
    return label.id if label else ""


@step(retry=Retry(max_attempts=2, initial_delay=0.5), on_error=OnError.CONTINUE, fallback=False)
async def label_thread(thread_id: str, label_id: str) -> bool:
    """Mark a whole conversation triaged.

    The *thread*, because Gmail's UI groups by conversation: labelling one
    message of a thread looks to a person like nothing happened.
    """
    if not label_id:
        return False
    await gmail_modify_thread_labels(thread_id=thread_id, add=[label_id])
    return True


@step(retry=Retry(max_attempts=1))
async def route(channel: str, message: TriagedMessage, note: str = "") -> None:
    """Post one message to its channel. Not retried — a retry posts it twice."""
    await slack_post_message(
        channel=channel,
        text=(
            f"*{message.category.value}*{note} — {message.subject}\n"
            f"from {message.sender}"
        ),
    )


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def create_meeting(
    calendar_id: str, title: str, start: str, end: str, attendee: str
) -> str:
    """Put a meeting on the calendar.

    ``send_updates="none"`` on purpose: a triage run that mailed every attendee
    as a side effect of reading the inbox would be a surprise, and bulk work
    should never notify by default.
    """
    event = await calendar_create_event(
        summary=title,
        start=start,
        end=end,
        calendar_id=calendar_id,
        attendees=[attendee] if attendee else None,
        send_updates="none",
    )
    return event.id


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="inbox_triage",
    version="2",
    triggers=[Poll(every=300)],
    grants=GrantSet(toolsets=["gmail", "google_calendar", "slack"]),
)
async def inbox_triage(ctx: Context, config: TriageConfig) -> TriageResult:
    """Read unread mail, classify it, route it, and file any meetings."""
    fetched = await ctx.step(fetch_unread, config.query, config.max_emails)
    messages = list(fetched["messages"])  # type: ignore[arg-type]
    if not messages:
        return TriageResult(total=0, complete=bool(fetched["complete"]))

    label_id = await ctx.step(resolve_label, config.triaged_label)

    triaged: list[TriagedMessage] = []
    for message in messages:
        verdict = await ctx.node(
            "agent.classify",
            ClassifyIn(
                text=f"Subject: {message['subject']}\nFrom: {message['sender']}\n\n"
                f"{message['body']}",
                labels=[category.value for category in EmailCategory],
                instructions=(
                    "urgent = needs action today. meeting = proposes or confirms "
                    "a time. support = a customer problem. newsletter = bulk "
                    "mail nobody replies to. spam = unsolicited."
                ),
            ),
        )
        triaged.append(
            TriagedMessage(
                message_id=str(message["id"]),
                thread_id=str(message["thread_id"]),
                subject=str(message["subject"]),
                sender=str(message["sender"]),
                category=EmailCategory(verdict.label),
                confident=bool(verdict.confident),
            )
        )

    routed = 0
    needs_review = 0
    meetings = 0

    for message in triaged:
        # An unconfident verdict is not a category — it is a request for a
        # person. Filing it by the guess is how triage quietly loses things.
        if not message.confident:
            await ctx.step(route, config.review_channel, message, " (unsure)")
            needs_review += 1
            continue

        if message.category is EmailCategory.SPAM:
            continue

        channel = config.channels.get(message.category.value)
        if channel:
            await ctx.step(route, channel, message)
            routed += 1

        if message.category is EmailCategory.MEETING:
            details = await ctx.node(
                "agent.extract_structured",
                ExtractStructuredIn(
                    text=f"{message.subject}\n\nfrom {message.sender}",
                    fields=MEETING_FIELDS,
                ),
            )
            start = str(details.values.get("start", ""))
            end = str(details.values.get("end", ""))
            if start and end:
                await ctx.step(
                    create_meeting,
                    config.calendar_id,
                    str(details.values.get("title") or message.subject),
                    start,
                    end,
                    message.sender,
                )
                meetings += 1

        await ctx.step(label_thread, message.thread_id, label_id)

    counts: dict[str, int] = {}
    for message in triaged:
        counts[message.category.value] = counts.get(message.category.value, 0) + 1

    return TriageResult(
        total=len(triaged),
        complete=bool(fetched["complete"]),
        routed=routed,
        needs_review=needs_review,
        meetings_created=meetings,
        category_counts=counts,
    )
