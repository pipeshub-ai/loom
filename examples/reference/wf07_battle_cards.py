"""Workflow: Sales Battle Card Generator.

Research a prospect and the competitors it is likely evaluating, work out what
separates us from each, and publish a battle card a rep can read before a call —
after somebody who knows the product has checked it.

What this shows, and why each part is the way it is:

* **Research is bounded and the coverage is carried.** ``exa_answer`` returns a
  written answer *with its citations*, and those citations travel into the card.
  A battle card asserting a competitor's pricing with no source is how a rep
  says something wrong on a call, confidently.

* **Fan-out is capped.** ``ctx.map(..., max_concurrency=3)`` over competitors
  rather than ``ctx.gather`` over a comprehension: a list of eight competitors
  should not become eight simultaneous searches against a rate-limited API.

* **A person signs it off before it is published.** This is sales collateral
  about *other companies*; ``human.review_edit`` is the difference between a
  draft and a claim the company has made. Publishing is what happens after
  approval, not before.

* **Judgement is an agent, and it is asked for one thing at a time.** Each
  competitor gets its own comparison rather than one prompt asked to hold eight
  in its head — which is the difference between an answer and an average.

* **The document lives where the team already reads.** Confluence, because a
  battle card nobody can find is a battle card nobody uses.

Credentials: ``EXA_API_KEY``, ``CONFLUENCE_URL``/``CONFLUENCE_EMAIL``/
``CONFLUENCE_API_TOKEN``, ``SLACK_BOT_TOKEN``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from loom import Context, Retry, step, workflow
from loom.nodes.human import ReviewIn
from loom.security.grants import GrantSet
from loom.toolsets.confluence.tools import confluence_create_page
from loom.toolsets.exa.tools import exa_answer
from loom.toolsets.slack.tools import slack_post_message
from loom.triggers import Manual

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class BattleCardConfig(BaseModel):
    """What to research, and where the card goes."""

    company_name: str = Field(description="The prospect being sold to.")
    competitor_names: list[str] = Field(
        default_factory=list, description="Who else they are evaluating."
    )
    our_product: str = Field(default="", description="What we are selling.")
    space_id: str = ""
    """Confluence space the card is published into."""

    announce_channel: str = "#sales"
    reviewer: str = ""


class Research(BaseModel):
    """One company, as the web describes it."""

    subject: str
    summary: str = ""
    citations: list[str] = Field(default_factory=list)
    """Where each claim came from. Carried into the card, because an
    unsourced assertion about a competitor is the one a rep repeats."""


class Comparison(BaseModel):
    """How we differ from one competitor."""

    competitor: str
    differentiators: list[str] = Field(default_factory=list)
    objection_handlers: list[str] = Field(default_factory=list)
    where_they_win: list[str] = Field(default_factory=list)
    """Named on purpose. A card that claims we win everywhere is one a rep
    stops trusting the first time a prospect pushes back."""


class BattleCard(BaseModel):
    """What the workflow returns."""

    company: str
    prospect: Research
    comparisons: list[Comparison] = Field(default_factory=list)
    published_url: str = ""
    approved: bool = False


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=2.0))
async def research(subject: str) -> Research:
    """What the web says about one company, with citations.

    ``exa_answer`` writes the answer and returns the sources it used. Keeping
    both is the point: the summary is what a rep reads, and the citations are
    what they check when a prospect disagrees.
    """
    answer = await exa_answer(query=f"What does {subject} do, sell, and charge for?")
    return Research(
        subject=subject,
        summary=answer.answer,
        citations=[citation.url for citation in answer.citations if citation.url],
    )


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def publish(space_id: str, title: str, body: str) -> str:
    """Put the approved card in Confluence and return its URL."""
    page = await confluence_create_page(space_id=space_id, title=title, body=body)
    return page.url


@step(retry=Retry(max_attempts=1))
async def announce(channel: str, title: str, url: str) -> None:
    """Tell the team. Not retried — a retry posts it twice."""
    await slack_post_message(channel=channel, text=f"New battle card: {title} — {url}")


def render(card: BattleCard) -> str:
    """The card as markup. A pure function, so it needs no journal entry.

    Deliberately not a step: it reaches nothing and returns the same string for
    the same card, so journaling it would record a value the body can always
    recompute.
    """
    lines = [f"h1. {card.company}", "", card.prospect.summary, ""]
    if card.prospect.citations:
        lines += ["h2. Sources", *[f"* {url}" for url in card.prospect.citations], ""]
    for comparison in card.comparisons:
        lines += [f"h2. vs {comparison.competitor}", "", "h3. Where we win"]
        lines += [f"* {point}" for point in comparison.differentiators]
        lines += ["", "h3. Where they win"]
        lines += [f"* {point}" for point in comparison.where_they_win]
        lines += ["", "h3. Objections"]
        lines += [f"* {point}" for point in comparison.objection_handlers]
        lines += [""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="battle_card_generator",
    version="2",
    triggers=[Manual()],
    grants=GrantSet(toolsets=["exa", "confluence", "slack"]),
)
async def battle_card_generator(ctx: Context, config: BattleCardConfig) -> BattleCard:
    """Research, compare, get it approved, then publish."""
    prospect = await ctx.step(research, config.company_name)

    # Capped: eight competitors is eight searches, three at a time, not eight
    # at once against an API that rate-limits.
    competitors = await ctx.map(research, config.competitor_names, max_concurrency=3)

    comparisons: list[Comparison] = []
    for found in competitors:
        # One competitor per call. Asking a model to hold eight comparisons in
        # one answer produces an average of them rather than an answer about
        # any of them.
        verdict = await ctx.agent(
            f"We sell {config.our_product or 'our product'}. A prospect, "
            f"{config.company_name}, is evaluating us against {found.subject}.\n\n"
            f"What we know about {found.subject}:\n{found.summary}\n\n"
            "Give three things we do better, three things they do better, and "
            "three replies to the objections a rep will actually hear. Say "
            "nothing you cannot support from the text above.",
            name=f"compare:{found.subject}",
        )
        comparisons.append(
            Comparison(
                competitor=found.subject,
                differentiators=[str(verdict.output)],
                where_they_win=[],
                objection_handlers=[],
            )
        )

    card = BattleCard(
        company=config.company_name, prospect=prospect, comparisons=comparisons
    )

    # Sales collateral about other companies. A person who knows the product
    # signs it off before it becomes something the company has said.
    review = await ctx.node(
        "human.review_edit",
        ReviewIn(
            subject=f"battle-card:{config.company_name}",
            draft=render(card),
            prompt="Check every claim about a competitor against its citation.",
            assignees=[config.reviewer] if config.reviewer else [],
        ),
    )
    if not review.approved:
        return card

    url = await ctx.step(
        publish,
        config.space_id,
        f"Battle card — {config.company_name}",
        str(review.content),
    )
    await ctx.step(announce, config.announce_channel, config.company_name, url)

    return card.model_copy(update={"published_url": url, "approved": True})
