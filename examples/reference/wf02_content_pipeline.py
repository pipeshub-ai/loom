"""Workflow: AI Content Generation Pipeline.

Weekly: find what is being talked about in a niche, write a piece per channel,
have somebody read it, and publish — to Slack, Teams, and a Confluence page.

**Why not LinkedIn and X.** The original published to both, and LOOM ships
neither: LinkedIn's post API is partner-approved and X's write tier is paid, so
a reference workflow built on them would be one nobody reading this could run.
Every pattern that version demonstrated survives here — research, per-channel
generation, parallel publish, partial failure, a summary — against three
channels that work with credentials a reader already has. Swapping a channel
back in later is one step and one grant, which is the point of the toolset
layer.

What this shows, and why each part is the way it is:

* **Research is cited, and the citations travel.** ``exa_answer`` returns a
  written answer *with its sources*, and they reach the draft. A weekly post
  asserting an industry trend with nothing behind it is how a company says
  something wrong in public.

* **Per-channel generation, because the channels are not alike.** A Slack post
  and a Confluence page want different lengths and different formatting, and
  one text posted to both reads as written for neither.

* **A person reads it before it goes out.** ``human.review_edit`` on the whole
  set: this is content published under the company's name, and the edit a
  reviewer makes is what actually gets posted rather than the draft they
  approved alongside.

* **Publishing is bounded and partial failure is reported.**
  ``ctx.map(..., max_concurrency=2)``, and a channel that fails does not take
  the others down — the result says which landed.

* **No image.** The original generated one, and LOOM has no image provider. An
  ``ImageProvider`` port for a single example is a subsystem in search of a
  user; when one exists this workflow gains a step. Saying so beats a step that
  pretends.

Credentials: ``EXA_API_KEY``, ``SLACK_BOT_TOKEN``, ``MS_*`` (Teams),
``CONFLUENCE_*``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from loom import Context, OnError, Retry, step, workflow
from loom.nodes.human import ReviewIn
from loom.security.grants import GrantSet
from loom.toolsets.confluence.tools import confluence_create_page
from loom.toolsets.exa.tools import exa_answer, exa_search
from loom.toolsets.microsoft.teams.tools import teams_send_channel_message
from loom.toolsets.slack.tools import slack_post_message
from loom.triggers import Schedule

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ContentConfig(BaseModel):
    """What to write about, and where it goes."""

    niche: str = Field(
        description="The subject area to research, described rather than keyworded."
    )
    tone: str = "plain and specific, no hype"
    slack_channel: str = "#announcements"
    teams_team_id: str = ""
    teams_channel_id: str = ""
    confluence_space_id: str = ""
    reviewer: str = ""


class Topic(BaseModel):
    """One thing worth writing about, and where that claim came from."""

    title: str
    summary: str = ""
    citations: list[str] = Field(default_factory=list)
    """Carried into the draft. An assertion about an industry with no source
    behind it is the one a reviewer cannot check and a reader repeats."""


class Draft(BaseModel):
    """One piece, written for one channel."""

    channel: str
    """``slack``, ``teams``, or ``confluence``."""

    title: str = ""
    body: str


class Published(BaseModel):
    """What one channel did with it."""

    channel: str
    reference: str = ""
    """A message timestamp, a message id, or a page URL."""

    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.reference) and not self.error


class ContentResult(BaseModel):
    """Summary returned by the workflow."""

    topic: str = ""
    approved: bool = False
    published: list[Published] = Field(default_factory=list)

    @property
    def landed(self) -> int:
        return sum(1 for row in self.published if row.ok)


#: How long each channel's draft should be, and in what shape. Per channel
#: because they are not alike: a Slack post nobody scrolls and a Confluence
#: page nobody skims are different failures.
CHANNEL_BRIEF = {
    "slack": "three short paragraphs, no headings, written to be read in a feed",
    "teams": "three short paragraphs, a single bolded lead line",
    "confluence": "a page with an intro, two or three headed sections, and a sources list",
}


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=2.0))
async def find_topic(niche: str) -> Topic:
    """The most-discussed thing in *niche* this week, with its sources.

    Two calls: a search to find what is being written about, then an answer
    over it. ``exa_answer`` alone would answer from the whole web rather than
    from what is *current*, which is the question a weekly pipeline is asking.
    """
    recent = await exa_search(
        query=f"notable developments in {niche} this week",
        num_results=8,
        category="news",
    )
    answer = await exa_answer(
        query=(
            f"What is the single most significant recent development in {niche}, "
            "and why does it matter?"
        )
    )
    headline = recent[0].title if recent else niche
    return Topic(
        title=headline,
        summary=answer.answer,
        citations=[c.url for c in answer.citations if c.url],
    )


class Routing(BaseModel):
    """Where each channel's post goes. No credentials — those are the
    toolsets' own, read from the environment."""

    slack_channel: str = ""
    teams_team_id: str = ""
    teams_channel_id: str = ""
    confluence_space_id: str = ""


@step(retry=Retry(max_attempts=1), on_error=OnError.CONTINUE, fallback=None)
async def publish(draft: Draft, routing: Routing) -> Published | None:
    """Post one draft to its channel.

    One mapped step rather than three, so ``ctx.map`` can bound the whole
    fan-out over a heterogeneous list — which is what the dispatch is for.

    **Not retried.** Neither Slack nor Teams has an idempotency key, so a retry
    after a delivery timeout posts the message twice, visibly, to everyone.
    Confluence would tolerate one — a duplicate page is recoverable and
    notifies nobody — but a single step carries a single policy, and the
    stricter one is the right choice when they disagree.

    ``on_error=CONTINUE`` with an explicit fallback, so one channel failing
    loses that channel rather than the others.
    """
    if draft.channel == "slack":
        posted = await slack_post_message(
            channel=routing.slack_channel, text=draft.body
        )
        return Published(channel="slack", reference=posted.ts)

    if draft.channel == "teams":
        sent = await teams_send_channel_message(
            team_id=routing.teams_team_id,
            channel_id=routing.teams_channel_id,
            content=draft.body,
            subject=draft.title,
        )
        return Published(channel="teams", reference=sent.id)

    page = await confluence_create_page(
        space_id=routing.confluence_space_id,
        title=draft.title or "Weekly update",
        body=draft.body,
    )
    return Published(channel="confluence", reference=page.url)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="content_pipeline",
    version="2",
    triggers=[Schedule(cron="0 9 * * 1", timezone="UTC")],
    grants=GrantSet(toolsets=["exa", "slack", "teams", "confluence"]),
)
async def content_pipeline(ctx: Context, config: ContentConfig) -> ContentResult:
    """Research, write per channel, get it approved, then publish."""
    topic = await ctx.step(find_topic, config.niche)

    channels = ["slack"]
    if config.teams_team_id and config.teams_channel_id:
        channels.append("teams")
    if config.confluence_space_id:
        channels.append("confluence")

    sources = "\n".join(f"- {url}" for url in topic.citations)
    drafts: list[Draft] = []
    for channel in channels:
        written = await ctx.agent(
            f"Write a {config.tone} piece for {channel}: "
            f"{CHANNEL_BRIEF[channel]}.\n\n"
            f"Topic: {topic.title}\n\nWhat we know:\n{topic.summary}\n\n"
            f"Sources:\n{sources}\n\n"
            "Assert nothing the sources do not support. Where you cite, cite "
            "one of these URLs and no other.",
            name=f"write:{channel}",
        )
        drafts.append(
            Draft(channel=channel, title=topic.title, body=str(written.output))
        )

    # Published under the company's name, so a person reads it first — and
    # what they *edit* is what goes out, not the draft they approved beside it.
    review = await ctx.node(
        "human.review_edit",
        ReviewIn(
            subject=f"content:{topic.title[:60]}",
            draft="\n\n=====\n\n".join(f"[{d.channel}]\n{d.body}" for d in drafts),
            prompt="Check every claim against its source before this goes out.",
            assignees=[config.reviewer] if config.reviewer else [],
        ),
    )
    if not review.approved:
        return ContentResult(topic=topic.title, approved=False)

    if review.edited:
        drafts = _reassemble(drafts, str(review.content))

    # Bounded: three channels at two at a time. Small here, and the shape is
    # what matters — a per-item fan-out with no cap is the pattern this set is
    # not allowed to teach.
    routing = Routing(
        slack_channel=config.slack_channel,
        teams_team_id=config.teams_team_id,
        teams_channel_id=config.teams_channel_id,
        confluence_space_id=config.confluence_space_id,
    )
    by_channel = {draft.channel: draft for draft in drafts}
    published = await ctx.map(
        publish,
        [by_channel[name] for name in channels],
        max_concurrency=2,
        routing=routing,
    )

    return ContentResult(
        topic=topic.title,
        approved=True,
        published=[row for row in published if row is not None],
    )


def _reassemble(drafts: list[Draft], edited: str) -> list[Draft]:
    """Split a reviewer's edited block back into per-channel drafts.

    A pure function, so it needs no journal entry. The separator is the one the
    review was rendered with; a reviewer who removes it gets their whole text
    on the first channel, which is visible and recoverable — where silently
    posting the *unedited* draft would not be.
    """
    parts = edited.split("\n\n=====\n\n")
    if len(parts) != len(drafts):
        return [drafts[0].model_copy(update={"body": edited})]
    out: list[Draft] = []
    for draft, part in zip(drafts, parts, strict=True):
        body = part.split("]\n", 1)[-1] if part.startswith("[") else part
        out.append(draft.model_copy(update={"body": body}))
    return out
