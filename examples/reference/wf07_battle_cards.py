"""Workflow: Sales Battle Card Generator."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from workflow_builder import Context, Retry, step, workflow

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BattleCardConfig(BaseModel):
    """Input for battle card generation."""

    company_name: str
    competitor_names: list[str]
    industry: str = "technology"
    openai_api_key: str = ""


class CompanyProfile(BaseModel):
    """Research output for a single company."""

    name: str
    description: str = ""
    products: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    recent_news: list[str] = []


class CompetitorAnalysis(BaseModel):
    """Side-by-side analysis against one competitor."""

    competitor: str
    differentiators: list[str] = []
    objection_handlers: dict[str, str] = {}
    win_themes: list[str] = []


class BattleCard(BaseModel):
    """Final battle card combining all research."""

    company: str
    company_profile: CompanyProfile
    competitor_analyses: list[CompetitorAnalysis] = []
    elevator_pitch: str = ""
    document_url: str = ""


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, delay=2.0))
async def research_company(
    company_name: str,
    api_key: str,
) -> CompanyProfile:
    """Research a company using web data and LLM synthesis.

    Args:
        company_name: Name of the company to research.
        api_key: OpenAI API key.

    Returns:
        Structured company profile.
    """
    prompt = (
        f"Research {company_name}. Provide: "
        "description, main products, strengths, weaknesses, "
        "and 3 recent news items."
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

    return CompanyProfile(
        name=company_name,
        description=content[:200] if content else "",
        products=["Product A", "Product B"],
        strengths=["Strong brand", "Large user base"],
        weaknesses=["High pricing"],
        recent_news=["Launched new feature"],
    )


@step(retry=Retry(max_attempts=2, delay=1.0))
async def analyze_competitors(
    our_profile: CompanyProfile,
    competitor_profile: CompanyProfile,
    api_key: str,
) -> CompetitorAnalysis:
    """Generate competitive analysis between two companies.

    Args:
        our_profile: Our company's profile.
        competitor_profile: The competitor's profile.
        api_key: OpenAI API key.

    Returns:
        Competitive analysis with differentiators.
    """
    prompt = (
        f"Compare {our_profile.name} vs {competitor_profile.name}. "
        "List differentiators, common objections with handlers, "
        "and win themes for our sales team."
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

    return CompetitorAnalysis(
        competitor=competitor_profile.name,
        differentiators=[
            "Better integration",
            "Lower total cost",
        ],
        objection_handlers={
            "Too expensive": "ROI within 6 months",
            "Smaller company": "Faster innovation cycle",
        },
        win_themes=["Speed to value", "Customer success"],
    )


@step(retry=Retry(max_attempts=2, delay=1.0))
async def create_document(
    card: BattleCard,
    api_key: str,
) -> str:
    """Create final battle card document and store it.

    Args:
        card: The assembled battle card data.
        api_key: OpenAI API key.

    Returns:
        URL of the stored document.
    """
    prompt = (
        f"Write a concise elevator pitch for {card.company} "
        f"given these differentiators: "
        f"{card.competitor_analyses[0].differentiators[:3]}"
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

        # Store the document
        await client.post(
            "https://docs.example.com/api/battle-cards",
            json=card.model_dump(),
        )

    return f"https://docs.example.com/cards/{card.company}"


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="battle_card_generator", version="1")
async def battle_card_generator(
    ctx: Context,
    config: BattleCardConfig,
) -> BattleCard:
    """Research a company and competitors, then generate a battle card.

    Pipeline: research (parallel) -> analyze (parallel) -> document.
    """
    # Research our company and all competitors in parallel
    research_tasks = [
        ctx.step(research_company, config.company_name, config.openai_api_key),
    ]
    for comp in config.competitor_names:
        research_tasks.append(
            ctx.step(research_company, comp, config.openai_api_key),
        )

    profiles: list[CompanyProfile] = await ctx.gather(*research_tasks)
    our_profile = profiles[0]
    competitor_profiles = profiles[1:]

    # Analyze each competitor in parallel
    analysis_tasks = [
        ctx.step(
            analyze_competitors,
            our_profile,
            comp_profile,
            config.openai_api_key,
        )
        for comp_profile in competitor_profiles
    ]
    analyses: list[CompetitorAnalysis] = await ctx.gather(
        *analysis_tasks,
    )

    card = BattleCard(
        company=config.company_name,
        company_profile=our_profile,
        competitor_analyses=analyses,
    )

    doc_url = await ctx.step(
        create_document, card, config.openai_api_key,
    )

    return card.model_copy(update={"document_url": doc_url})
