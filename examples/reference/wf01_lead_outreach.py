"""Workflow: Lead Enrichment & Cold Outreach.

Runs nightly: find companies matching a description, pull a contact out of each
page, skip anyone already in the CRM, draft a personalised mail, **have a person
approve it**, send, and report.

What this shows, and why each part is the way it is:

* **Every external call goes through a toolset.** The version this replaced
  hand-rolled ``httpx`` against invented hostnames, which forfeited error
  classification (a 400 and a 503 retried identically), pagination coverage,
  effect classes — so ``TaintBroker`` and ``GrantSet`` enforced nothing — and
  the fakes that let a workflow be smoke-tested at all.

* **Nobody is cold-emailed without a person seeing it.** ``gmail_create_draft``
  is the safe half of sending; ``human.review_edit`` parks the run while
  somebody reads and edits it; ``gmail_send_draft`` is what finally goes out.
  A workflow that mails fifty strangers on a cron with no gate is not a
  reference for anything.

* **Fan-out is bounded.** ``ctx.map(..., max_concurrency=4)`` rather than
  ``ctx.gather`` over a comprehension, because the latter issues one concurrent
  call per lead against a rate-limited API.

* **Judgement is an agent, rules are steps.** Pulling a name and an email out of
  a page is judgement, so it is ``agent.extract_structured``; deciding whether
  a contact already exists is an exact CRM lookup, so it is a step.

* **No credential is an argument.** Every toolset reads its own environment.
  Step inputs are journaled, and a key passed as one is recorded — see
  ``loom.core.redaction``.

Credentials: ``EXA_API_KEY``, ``HUBSPOT_ACCESS_TOKEN``, ``GOOGLE_*`` (Gmail),
``SLACK_BOT_TOKEN``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from loom import Context, OnError, Retry, step, workflow
from loom.nodes.agentic import ExtractStructuredIn
from loom.nodes.human import ReviewIn
from loom.security.grants import GrantSet
from loom.toolsets.exa.tools import exa_get_contents, exa_search
from loom.toolsets.google.gmail.tools import gmail_create_draft, gmail_send_draft
from loom.toolsets.hubspot.tools import hubspot_create_contact, hubspot_get_contact_by_email
from loom.toolsets.slack.tools import slack_post_message
from loom.triggers import Schedule

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class LeadConfig(BaseModel):
    """Input configuration for the outreach workflow."""

    describe: str = Field(
        description="What kind of company to look for. Exa searches on meaning, "
        "so this is a description rather than keywords."
    )
    max_leads: int = 25
    sender_signature: str = "— the team"
    slack_channel: str = "#outreach"
    reviewer: str = ""
    """Who approves each draft. Empty means whoever is watching ``loom pending``."""


class Lead(BaseModel):
    """A prospect, as read off a page."""

    name: str = ""
    email: str = ""
    company: str = ""
    title: str = ""
    source_url: str = ""

    @property
    def usable(self) -> bool:
        """Whether there is enough here to write to.

        A lead with no email is not a lead. Dropping it here is why the
        summary's counts add up."""
        return bool(self.email and "@" in self.email)


class OutreachResult(BaseModel):
    """Summary returned by the workflow."""

    total_leads: int
    emails_sent: int
    skipped_existing: int = 0
    rejected_by_reviewer: int = 0
    errors: list[str] = Field(default_factory=list)


#: What ``agent.extract_structured`` is asked to pull out of a page.
LEAD_FIELDS = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Full name of a contact person"},
        "email": {"type": "string", "description": "Their email address"},
        "company": {"type": "string"},
        "title": {"type": "string", "description": "Their job title"},
    },
}


# ---------------------------------------------------------------------------
# Steps — the deterministic half
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def find_companies(describe: str, limit: int) -> list[str]:
    """URLs of companies matching *describe*.

    Exa is embeddings-based, so it takes a description of what you want rather
    than keywords. It does not paginate and caps at 100 — the client refuses a
    larger request rather than silently returning a short answer.
    """
    results = await exa_search(query=describe, num_results=min(limit, 100))
    return [result.url for result in results if result.url]


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def read_pages(urls: list[str]) -> list[str]:
    """The text behind each URL, for the extractor to read.

    ``.failed`` carries the URLs Exa could not fetch — it answers 200 for a
    request in which some of them failed, and a short list with nothing saying
    it is short is the same bug as a silent page cap.
    """
    contents = await exa_get_contents(urls=urls, include_text=True)
    return [page.text for page in contents.results if page.text]


@step(retry=Retry(max_attempts=2, initial_delay=0.5), on_error=OnError.CONTINUE, fallback=None)
async def existing_contact(email: str) -> str:
    """The HubSpot contact id for *email*, or ``""``.

    ``on_error=CONTINUE`` with an explicit ``fallback``: a CRM lookup that
    fails should not stop the run, and treating the failure as "not found"
    would create a duplicate contact. ``""`` means *no answer*, and the caller
    skips the lead rather than guessing.
    """
    contact = await hubspot_get_contact_by_email(email=email)
    return contact.id if contact else ""


@step(retry=Retry(max_attempts=2, initial_delay=0.5))
async def record_lead(lead: Lead) -> str:
    """Put the lead in the CRM before anything is sent to them.

    Deliberately before the mail: if the send fails, a recorded contact is
    recoverable, where a sent mail with no CRM record is a person nobody knows
    was contacted.
    """
    contact = await hubspot_create_contact(
        properties={
            "email": lead.email,
            "firstname": lead.name.split(" ")[0] if lead.name else "",
            "lastname": " ".join(lead.name.split(" ")[1:]),
            "company": lead.company,
            "jobtitle": lead.title,
            "hs_lead_status": "NEW",
        }
    )
    return contact.id


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def draft_email(lead: Lead, subject: str, body: str) -> str:
    """Compose the mail without sending it, and return the draft id.

    Drafting is retryable — a duplicate draft is noise a person deletes. Sending
    is not, which is why they are two steps.
    """
    draft = await gmail_create_draft(to=lead.email, subject=subject, body=body)
    return draft.id


@step(retry=Retry(max_attempts=1))
async def send_draft(draft_id: str) -> str:
    """Send an approved draft.

    **No retry, deliberately.** Gmail has no idempotency key, so a timeout
    after delivery is indistinguishable from a failure and a retry mails the
    person twice. Journaling covers replay; this covers the attempt.
    """
    sent = await gmail_send_draft(draft_id=draft_id)
    return sent.id


@step(retry=Retry(max_attempts=1))
async def report_to_slack(channel: str, result: OutreachResult) -> None:
    """Post the summary. Not retried — a retry posts it twice, visibly."""
    await slack_post_message(
        channel=channel,
        text=(
            f"Outreach complete: {result.emails_sent}/{result.total_leads} sent, "
            f"{result.skipped_existing} already in the CRM, "
            f"{result.rejected_by_reviewer} rejected, {len(result.errors)} errors"
        ),
    )


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="lead_outreach",
    version="2",
    triggers=[Schedule(cron="0 7 * * 1-5", timezone="UTC")],
    grants=GrantSet(toolsets=["exa", "hubspot", "gmail", "slack"]),
)
async def lead_outreach(ctx: Context, config: LeadConfig) -> OutreachResult:
    """Find leads, draft outreach, have a person approve it, then send."""
    urls = await ctx.step(find_companies, config.describe, config.max_leads)
    if not urls:
        return OutreachResult(total_leads=0, emails_sent=0)

    pages = await ctx.step(read_pages, urls)

    # Judgement, not a rule: there is no reliable shape to a company page, so
    # this is an agent node rather than a regex nobody can defend.
    leads: list[Lead] = []
    for index, text in enumerate(pages):
        extracted = await ctx.node(
            "agent.extract_structured",
            ExtractStructuredIn(text=text[:8000], fields=LEAD_FIELDS),
        )
        lead = Lead(
            **{k: str(v) for k, v in extracted.values.items() if k in Lead.model_fields},
            source_url=urls[index] if index < len(urls) else "",
        )
        if lead.usable:
            leads.append(lead)

    # Bounded: one CRM lookup at a time per four leads, not one per lead at
    # once. `ctx.map` is what `ctx.gather` over a comprehension should have
    # been.
    contact_ids = await ctx.map(existing_contact, [lead.email for lead in leads], max_concurrency=4)

    errors: list[str] = []
    sent = 0
    skipped = 0
    rejected = 0

    for lead, contact_id in zip(leads, contact_ids, strict=True):
        if contact_id:
            skipped += 1
            continue

        subject = f"Quick note for {lead.name or lead.company}"
        body = await ctx.agent(
            "Write a short, specific cold email. Reference what this company "
            "actually does — no flattery, no invented facts. Two short "
            f"paragraphs, then this signature: {config.sender_signature}\n\n"
            f"Company: {lead.company}\nContact: {lead.name} ({lead.title})\n"
            f"Source: {lead.source_url}",
            name="write_email",
        )

        draft_id = await ctx.step(draft_email, lead, subject, str(body.output))

        # Parks the run at no cost until somebody reads it. `loom pending`
        # lists what is waiting; `loom approve` and the MCP tools resolve it.
        review = await ctx.node(
            "human.review_edit",
            ReviewIn(
                subject=f"outreach:{lead.email}",
                draft=str(body.output),
                prompt=f"About to cold-email {lead.name or lead.email} at {lead.company}.",
                assignees=[config.reviewer] if config.reviewer else [],
            ),
        )
        if not review.approved:
            rejected += 1
            continue

        if review.edited:
            draft_id = await ctx.step(draft_email, lead, subject, str(review.content))

        await ctx.step(record_lead, lead)
        try:
            await ctx.step(send_draft, draft_id)
            sent += 1
        except Exception as exc:
            # Recorded, not swallowed: one failed send must not lose the other
            # leads' results, and the summary reports it.
            errors.append(f"{lead.email}: {exc}")

    result = OutreachResult(
        total_leads=len(leads),
        emails_sent=sent,
        skipped_existing=skipped,
        rejected_by_reviewer=rejected,
        errors=errors,
    )
    await ctx.step(report_to_slack, config.slack_channel, result)
    return result
