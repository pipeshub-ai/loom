"""Workflow: Multi-Channel Publisher.

Write several variants about a topic, drop the ones already said, pick the best,
and post it to every configured channel at once — reporting which landed.

**Why not LinkedIn and X.** The original published to both, and LOOM ships
neither: LinkedIn's post API is partner-approved and X's write tier is paid.
Every pattern that version demonstrated survives here — variant generation,
deduplication, bounded parallel publish, partial failure — against channels a
reader can actually run.

What this shows, and why each part is the way it is:

* **Deduplication is against history, not just the batch.** Two runs a week
  apart producing the same post is the failure that matters, and a
  within-batch check cannot see it. ``ctx.state`` is a KV space shared by every
  run of one workflow, which is exactly that memory. ``control.dedupe`` still
  handles the within-batch half — two rules, two places, because they catch
  different things.

* **Reading state and then branching on it is not replay-safe, so the read is
  fed to a step.** ``ctx.state`` is deliberately not journaled, so a replay
  sees whatever state holds *now* rather than what it held then. Passing it
  into a journaled step means the *decision* is recorded even though the read
  is not — and the step's arguments legitimately differ on replay, which is
  precisely why ``VerifyMode`` defaults to ``WARN`` rather than ``STRICT``.

* **Choosing between variants is judgement, so it is an agent.** ``agent.judge``
  rather than "the longest one" or "the first one" — a rule nobody can defend
  is a guess wearing the clothes of logic.

* **Publishing is bounded, and partial failure is a result rather than an
  exception.** A channel that fails is reported; the others still land.

* **History is written only for what actually posted.** Recording the intent
  would make a failed publish look said, so the next run would suppress a
  retry of something nobody ever saw.

Credentials: ``SLACK_BOT_TOKEN``, ``MS_*`` (Teams).
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from loom import Context, OnError, Retry, step, workflow
from loom.nodes.agentic import JudgeIn
from loom.nodes.control import DedupeIn
from loom.nodes.human import ApprovalIn
from loom.security.grants import GrantSet
from loom.toolsets.microsoft.teams.tools import teams_send_channel_message
from loom.toolsets.slack.tools import slack_post_message
from loom.triggers import Manual

#: Where published fingerprints live, in the workflow's shared state.
HISTORY_KEY = "published_hashes"

#: How many fingerprints to keep. Unbounded history is a value that grows for
#: the life of the workflow and is read whole on every run — a slow leak that
#: only shows up in year two.
HISTORY_LIMIT = 500


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Target(BaseModel):
    """One place a post goes."""

    kind: str
    """``slack`` or ``teams``."""

    channel: str = ""
    """A Slack channel id, or a Teams channel id."""

    team_id: str = ""
    """Teams only. A Teams channel id is meaningless without its team."""


class PublisherConfig(BaseModel):
    """What to write about, and where it goes."""

    topic: str
    targets: list[Target] = Field(default_factory=list)
    variants: int = 3
    tone: str = "plain and specific, no hype"
    approve_first: bool = True
    """Ask a person before anything is posted. On by default — this publishes
    under the company's name to everyone in the channel."""

    reviewer: str = ""


class Variant(BaseModel):
    """One candidate post."""

    text: str
    fingerprint: str = ""
    """A digest of the normalised text, so "the same post" survives a changed
    space or a changed capital letter."""

    def fingerprinted(self) -> Variant:
        normalised = " ".join(self.text.lower().split())
        digest = hashlib.sha256(normalised.encode()).hexdigest()[:24]
        return self.model_copy(update={"fingerprint": digest})


class PublishResult(BaseModel):
    """What one channel did with it."""

    kind: str
    channel: str = ""
    reference: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.reference) and not self.error


class PublisherOutcome(BaseModel):
    """Summary returned by the workflow."""

    topic: str
    generated: int = 0
    already_said: int = 0
    """Variants suppressed because a previous run posted them."""

    duplicates_in_batch: int = 0
    approved: bool = True
    chosen_score: float = 0.0
    chosen_reason: str = ""
    """Why the winning variant won. Carried out because a score nobody can
    explain is not reviewable, and this decided what went out."""

    published: list[PublishResult] = Field(default_factory=list)

    @property
    def landed(self) -> int:
        return sum(1 for row in self.published if row.ok)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=2, initial_delay=0.5))
async def unseen(variants: list[Variant], history: list[str]) -> list[Variant]:
    """The variants this workflow has not published before.

    A ``@step`` and not an expression in the body, because *history* comes from
    ``ctx.state``, which is deliberately not journaled. Journaling the decision
    is what makes a replay take the branch it originally took, even though the
    read behind it would answer differently now.

    Its arguments therefore legitimately differ on replay. That is the case
    ``VerifyMode.WARN`` exists for, and the reason the default is not
    ``STRICT``.
    """
    seen = set(history)
    return [v for v in variants if v.fingerprint not in seen]


@step(retry=Retry(max_attempts=1), on_error=OnError.CONTINUE, fallback=None)
async def publish(target: Target, text: str) -> PublishResult | None:
    """Post to one channel.

    **Not retried.** Neither Slack nor Teams has an idempotency key, so a retry
    after a delivery timeout posts it twice — visibly, to everyone in the
    channel. ``on_error=CONTINUE`` with an explicit fallback so one channel
    failing loses that channel rather than the run.
    """
    if target.kind == "slack":
        posted = await slack_post_message(channel=target.channel, text=text)
        return PublishResult(
            kind="slack", channel=target.channel, reference=posted.ts
        )

    sent = await teams_send_channel_message(
        team_id=target.team_id, channel_id=target.channel, content=text
    )
    return PublishResult(kind="teams", channel=target.channel, reference=sent.id)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="social_publisher",
    version="2",
    triggers=[Manual()],
    grants=GrantSet(toolsets=["slack", "teams"]),
)
async def social_publisher(ctx: Context, config: PublisherConfig) -> PublisherOutcome:
    """Generate variants, drop what was already said, publish the best."""
    variants: list[Variant] = []
    for index in range(max(config.variants, 1)):
        written = await ctx.agent(
            f"Write a {config.tone} post about: {config.topic}\n\n"
            f"This is variant {index + 1} of {config.variants} — make it "
            "genuinely different in angle from an obvious first attempt, not "
            "just reworded.",
            name=f"variant:{index}",
        )
        variants.append(Variant(text=str(written.output)).fingerprinted())

    # Within this batch. A rule, so a node rather than an agent.
    deduped = await ctx.node(
        "control.dedupe",
        DedupeIn(items=[v.model_dump() for v in variants], key="fingerprint"),
    )
    batch = [Variant.model_validate(row) for row in deduped.items]

    # Across runs. The read is not journaled, so the *decision* is — see the
    # step's docstring.
    history: list[str] = await ctx.state.get(HISTORY_KEY, default=[])
    fresh = await ctx.step(unseen, batch, history)

    outcome = PublisherOutcome(
        topic=config.topic,
        generated=len(variants),
        duplicates_in_batch=len(variants) - len(batch),
        already_said=len(batch) - len(fresh),
    )
    if not fresh:
        # Every variant has been said before. Not a failure — it is what a
        # dedupe is for, and posting anyway would be the bug.
        return outcome

    # Judgement: there is no rule for "which of these reads better" that is
    # right for every input, and an invented one — longest, first — is a guess
    # wearing the clothes of logic.
    #
    # `agent.judge` scores **one** candidate and says why, so each is judged
    # separately and the best score wins. Asking one call to rank a list would
    # get an answer, but not one with a reason attached to each — and the
    # reason is what makes the choice reviewable.
    criteria = (
        f"Says something specific about {config.topic}, in a {config.tone} "
        "voice, with no hype and no invented facts."
    )
    scored: list[tuple[float, str, Variant]] = []
    for candidate in fresh:
        verdict = await ctx.node(
            "agent.judge",
            JudgeIn(candidate=candidate.text, criteria=criteria),
            name=f"judge:{candidate.fingerprint[:8]}",
        )
        scored.append((float(verdict.score), str(verdict.reason), candidate))

    # Ties broken by fingerprint, so a replay picks the same one. An unstable
    # choice here would publish a different post on a re-drive.
    scored.sort(key=lambda row: (-row[0], row[2].fingerprint))
    best_score, best_reason, chosen = scored[0]
    outcome = outcome.model_copy(
        update={"chosen_score": best_score, "chosen_reason": best_reason}
    )

    if config.approve_first:
        approval = await ctx.node(
            "human.approval",
            ApprovalIn(
                subject=f"publish:{chosen.fingerprint}",
                prompt=(
                    f"About to post this to {len(config.targets)} channel(s):"
                    f"\n\n{chosen.text}"
                ),
                assignees=[config.reviewer] if config.reviewer else [],
            ),
        )
        if not approval.approved:
            return outcome.model_copy(update={"approved": False})

    # Bounded: three channels at a time, not one concurrent call per target.
    published = await ctx.map(
        publish, config.targets, max_concurrency=3, text=chosen.text
    )
    landed = [row for row in published if row is not None]

    # Recorded only if something actually posted. Writing the intent would make
    # a failed publish look said, and the next run would suppress a retry of
    # something nobody ever saw.
    if any(row.ok for row in landed):
        await ctx.state.set(
            HISTORY_KEY, [*history, chosen.fingerprint][-HISTORY_LIMIT:]
        )

    return outcome.model_copy(update={"published": landed})
