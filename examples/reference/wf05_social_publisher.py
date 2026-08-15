"""Workflow: Multi-Platform Social Publisher."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from loom import Context, OnError, Retry, step, workflow

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Platform(StrEnum):
    """Supported social media platforms."""

    LINKEDIN = "linkedin"
    TWITTER = "twitter"
    FACEBOOK = "facebook"


class PublisherConfig(BaseModel):
    """Input configuration for the social publisher."""

    topic: str
    platforms: list[Platform] = [Platform.LINKEDIN, Platform.TWITTER]
    tone: str = "professional"


class SocialPost(BaseModel):
    """A generated post ready for publishing."""

    platform: Platform
    text: str
    hashtags: list[str]
    content_hash: str


class PublishResult(BaseModel):
    """Result of publishing to a single platform."""

    platform: Platform
    post_id: str
    success: bool
    error: str = ""


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=2))
async def generate_posts(
    topic: str,
    platforms: list[Platform],
    tone: str,
) -> list[SocialPost]:
    """Generate tailored posts for each platform via LLM."""
    import hashlib

    import httpx

    posts: list[SocialPost] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for platform in platforms:
            char_limit = {
                Platform.LINKEDIN: 1500,
                Platform.TWITTER: 280,
                Platform.FACEBOOK: 500,
            }.get(platform, 500)

            prompt = (
                f"Write a {tone} {platform.value} post about "
                f"{topic}. Max {char_limit} chars. "
                f"Include 3 relevant hashtags."
            )

            resp = await client.post(
                "https://api.openai.example/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]

            content_hash = hashlib.sha256(
                text.encode(),
            ).hexdigest()[:16]
            posts.append(
                SocialPost(
                    platform=platform,
                    text=text[:char_limit],
                    hashtags=[f"#{topic.split()[0]}"],
                    content_hash=content_hash,
                ),
            )

    return posts


@step(retry=Retry(max_attempts=2))
async def check_duplicates(
    posts: list[SocialPost],
) -> list[SocialPost]:
    """Filter out posts whose content hash was already published."""
    import httpx

    unique: list[SocialPost] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for post in posts:
            resp = await client.get(
                "https://api.dedup.example/v1/check",
                params={
                    "hash": post.content_hash,
                    "platform": post.platform.value,
                },
            )
            if resp.is_success:
                data = resp.json()
                if not data.get("exists", False):
                    unique.append(post)

    return unique


@step(
    retry=Retry(max_attempts=3, initial_delay=2.0),
    on_error=OnError.CONTINUE,
)
async def publish_to_platform(
    post: SocialPost,
) -> PublishResult:
    """Publish a single post to its target platform."""
    import httpx

    endpoints = {
        Platform.LINKEDIN: "https://api.linkedin.example/v2/posts",
        Platform.TWITTER: "https://api.twitter.example/2/tweets",
        Platform.FACEBOOK: "https://api.facebook.example/v1/posts",
    }

    url = endpoints.get(post.platform, endpoints[Platform.TWITTER])

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            json={
                "text": post.text,
                "hashtags": post.hashtags,
            },
        )

        if not resp.is_success:
            return PublishResult(
                platform=post.platform,
                post_id="",
                success=False,
                error=f"HTTP {resp.status_code}",
            )

        post_id = resp.json().get("id", "unknown")

    return PublishResult(
        platform=post.platform,
        post_id=post_id,
        success=True,
    )


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="social_publisher", version="1")
async def social_publisher(
    ctx: Context,
    config: PublisherConfig,
) -> dict[str, object]:
    """Generate, deduplicate, and publish to multiple platforms."""
    # Generate posts for all requested platforms
    all_posts = await ctx.step(
        generate_posts,
        config.topic,
        config.platforms,
        config.tone,
    )

    if not all_posts:
        return {
            "generated": 0,
            "published": 0,
            "skipped_duplicates": 0,
        }

    # Filter out duplicates
    unique_posts = await ctx.step(check_duplicates, all_posts)
    skipped = len(all_posts) - len(unique_posts)

    if not unique_posts:
        return {
            "generated": len(all_posts),
            "published": 0,
            "skipped_duplicates": skipped,
        }

    # Publish to all platforms in parallel
    results: list[PublishResult] = await ctx.gather(
        *[
            ctx.step(publish_to_platform, post)
            for post in unique_posts
        ],
    )

    succeeded = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    return {
        "generated": len(all_posts),
        "published": len(succeeded),
        "skipped_duplicates": skipped,
        "failed": len(failed),
        "post_ids": [r.post_id for r in succeeded],
        "errors": [
            f"{r.platform.value}: {r.error}" for r in failed
        ],
    }
