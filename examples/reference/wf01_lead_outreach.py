"""Workflow: Lead Enrichment & Cold Outreach."""

from __future__ import annotations

from pydantic import BaseModel

from loom import Context, OnError, Retry, step, workflow

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class LeadConfig(BaseModel):
    """Input configuration for the outreach workflow."""

    source_url: str
    max_leads: int = 50
    sender_email: str = "outreach@example.com"


class Lead(BaseModel):
    """A raw lead scraped from a directory or list."""

    name: str
    email: str
    company: str
    title: str = ""


class EnrichedLead(BaseModel):
    """A lead with company context added."""

    lead: Lead
    company_size: str = ""
    industry: str = ""
    recent_news: str = ""


class OutreachResult(BaseModel):
    """Summary returned by the workflow."""

    total_leads: int
    emails_sent: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def scrape_leads(
    source_url: str,
    max_leads: int,
) -> list[Lead]:
    """Scrape leads from the configured source URL."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            source_url,
            params={"limit": max_leads},
        )
        resp.raise_for_status()
        rows = resp.json().get("leads", [])

    return [
        Lead(
            name=r["name"],
            email=r["email"],
            company=r["company"],
            title=r.get("title", ""),
        )
        for r in rows[:max_leads]
    ]


@step(retry=Retry(max_attempts=2, initial_delay=0.5))
async def enrich_lead(lead: Lead) -> EnrichedLead:
    """Call an enrichment API to add company context."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.enrichment.example/v1/enrich",
            json={"company": lead.company},
        )
        resp.raise_for_status()
        data = resp.json()

    return EnrichedLead(
        lead=lead,
        company_size=data.get("size", "unknown"),
        industry=data.get("industry", "unknown"),
        recent_news=data.get("news", ""),
    )


@step(retry=Retry(max_attempts=2))
async def generate_email(enriched: EnrichedLead) -> str:
    """Use an LLM to draft a personalised cold email."""
    import httpx

    prompt = (
        f"Write a short cold email to {enriched.lead.name} "
        f"at {enriched.lead.company} ({enriched.industry}). "
        f"Reference: {enriched.recent_news or 'their work'}."
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.example/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        body = resp.json()

    return body["choices"][0]["message"]["content"]


@step(
    retry=Retry(max_attempts=3, initial_delay=2.0),
    on_error=OnError.CONTINUE,
)
async def send_email(
    to_email: str,
    subject: str,
    body: str,
    sender: str,
) -> bool:
    """Send the email via a transactional mail API."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.mail.example/v1/send",
            json={
                "from": sender,
                "to": to_email,
                "subject": subject,
                "html": body,
            },
        )
        resp.raise_for_status()

    return True


@step
async def log_results(result: OutreachResult) -> None:
    """Post a summary to the team Slack channel."""
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            "https://hooks.slack.example/services/T00/B00/xxx",
            json={
                "text": (
                    f"Outreach complete: {result.emails_sent}"
                    f"/{result.total_leads} sent, "
                    f"{len(result.errors)} errors"
                ),
            },
        )


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="lead_outreach", version="1")
async def lead_outreach(
    ctx: Context,
    config: LeadConfig,
) -> OutreachResult:
    """Scrape leads, enrich, personalise, and send cold email."""
    leads = await ctx.step(scrape_leads, config.source_url, config.max_leads)

    # Enrich all leads in parallel
    enriched_leads: list[EnrichedLead] = await ctx.gather(
        *[ctx.step(enrich_lead, lead) for lead in leads],
    )

    # Generate personalised emails in parallel
    emails: list[str] = await ctx.gather(
        *[ctx.step(generate_email, el) for el in enriched_leads],
    )

    # Send emails and track results
    errors: list[str] = []
    sent = 0
    for enriched, email_body in zip(enriched_leads, emails, strict=True):
        subject = f"Quick note for {enriched.lead.name}"
        ok = await ctx.step(
            send_email,
            enriched.lead.email,
            subject,
            email_body,
            config.sender_email,
        )
        if ok:
            sent += 1
        else:
            errors.append(f"Failed: {enriched.lead.email}")

    result = OutreachResult(
        total_leads=len(leads),
        emails_sent=sent,
        errors=errors,
    )

    await ctx.step(log_results, result)
    return result
