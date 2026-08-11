"""Workflow: AI Content Generation Pipeline."""

from __future__ import annotations

from pydantic import BaseModel

from workflow_builder import Context, Retry, step, workflow

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ContentConfig(BaseModel):
    """Input configuration for the content pipeline."""

    niche: str = "AI engineering"
    tone: str = "professional"
    publish_linkedin: bool = True
    publish_twitter: bool = True


class TrendingTopic(BaseModel):
    """A trending topic discovered during research."""

    title: str
    summary: str
    source_url: str


class GeneratedPost(BaseModel):
    """Content generated for a specific platform."""

    platform: str
    text: str
    hashtags: list[str]


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def research_trending(niche: str) -> list[TrendingTopic]:
    """Fetch trending topics from a news aggregation API."""
    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            "https://api.trends.example/v1/topics",
            params={"q": niche, "limit": 5},
        )
        resp.raise_for_status()
        items = resp.json().get("topics", [])

    return [
        TrendingTopic(
            title=t["title"],
            summary=t.get("summary", ""),
            source_url=t.get("url", ""),
        )
        for t in items
    ]


@step(retry=Retry(max_attempts=2))
async def generate_post(
    topic: TrendingTopic,
    platform: str,
    tone: str,
) -> GeneratedPost:
    """Use an LLM to produce platform-tailored content."""
    import httpx

    char_limit = 280 if platform == "twitter" else 1500
    prompt = (
        f"Write a {tone} {platform} post about: "
        f"{topic.title}. {topic.summary} "
        f"Max {char_limit} characters. Include hashtags."
    )

    async with httpx.AsyncClient(timeout=30) as client:
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

    return GeneratedPost(
        platform=platform,
        text=text,
        hashtags=[f"#{platform}", f"#{topic.title.split()[0]}"],
    )


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def generate_image(topic_title: str) -> str:
    """Generate a cover image via an image-generation API."""
    import httpx

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.images.example/v1/generate",
            json={"prompt": f"Blog cover for: {topic_title}"},
        )
        resp.raise_for_status()

    return resp.json().get("url", "")


@step(retry=Retry(max_attempts=3, initial_delay=2.0))
async def publish_linkedin(
    post: GeneratedPost,
    image_url: str,
) -> str:
    """Publish a post to LinkedIn via their API."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.linkedin.example/v2/ugcPosts",
            json={
                "text": post.text,
                "image_url": image_url,
            },
        )
        resp.raise_for_status()

    return resp.json().get("id", "unknown")


@step(retry=Retry(max_attempts=3, initial_delay=2.0))
async def publish_twitter(
    post: GeneratedPost,
    image_url: str,
) -> str:
    """Publish a tweet via the Twitter/X API."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.twitter.example/2/tweets",
            json={
                "text": post.text[:280],
                "media_url": image_url,
            },
        )
        resp.raise_for_status()

    return resp.json().get("id", "unknown")


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="content_pipeline", version="1")
async def content_pipeline(
    ctx: Context,
    config: ContentConfig,
) -> dict[str, object]:
    """Research trending topics and publish AI-generated content."""
    topics = await ctx.step(research_trending, config.niche)
    if not topics:
        return {"topic": None, "published": 0}

    # Pick the top trending topic
    topic = topics[0]

    # Generate platform-specific posts and image in parallel
    tasks: list[object] = []
    platforms: list[str] = []

    if config.publish_linkedin:
        tasks.append(
            ctx.step(generate_post, topic, "linkedin", config.tone),
        )
        platforms.append("linkedin")

    if config.publish_twitter:
        tasks.append(
            ctx.step(generate_post, topic, "twitter", config.tone),
        )
        platforms.append("twitter")

    image_task = ctx.step(generate_image, topic.title)
    tasks.append(image_task)

    results = await ctx.gather(*tasks)
    posts = results[:-1]
    image_url: str = results[-1]

    # Publish to each platform in parallel
    publish_tasks = []
    for platform, post in zip(platforms, posts, strict=True):
        if platform == "linkedin":
            publish_tasks.append(
                ctx.step(publish_linkedin, post, image_url),
            )
        elif platform == "twitter":
            publish_tasks.append(
                ctx.step(publish_twitter, post, image_url),
            )

    pub_ids = await ctx.gather(*publish_tasks)

    return {
        "topic": topic.title,
        "platforms": platforms,
        "post_ids": pub_ids,
        "published": len(pub_ids),
    }
