"""Remembering which control an intent meant, across runs.

Tier 1 costs a model call to turn "the button that confirms the booking" into a
role and a name. The answer is almost always the same tomorrow, so paying for it
on every run is the saving Stagehand's action cache and Skyvern's code cache
both exist to capture.

**Why this is a cache and not state.** ``NodeContext`` deliberately withholds
``ctx.state`` — "shared across every run of the workflow… a node that could
reach them can change a run its author never saw" — and that reasoning holds:
state is *semantic*, a workflow branches on it, and a node writing to it can
change a run nobody reviewed. A plan cache is none of those things. Deleting it
changes no outcome, only a bill; nothing branches on it; and, critically, **a
hit is verified against the live page before it is used**, so a stale entry is
discarded rather than acted on. That last property is what makes it safe to
share across runs at all.

**A cached plan is not a journaled one.** The journal already serves a settled
``browser.observe`` back without resolving anything, so a *replay* never reaches
this. The cache is for the second and third *run* — and because it is not
journaled, an act whose target came from here legitimately replays with
different arguments. That is precisely the case ``VerifyMode.WARN`` exists to
tolerate, and the reason ``STRICT`` is not the default.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from loom.browser.base import Target
from loom.core.ids import stable_hash

logger = logging.getLogger(__name__)

__all__ = ["PlanCache", "page_shape"]

#: A day. Long enough that a daily workflow pays for resolution once, short
#: enough that a site's redesign costs at most one stale lookup per intent —
#: and a stale one is *verified and discarded*, never acted on, so the ceiling
#: on being wrong is a wasted model call rather than a wrong click.
DEFAULT_TTL_SECONDS = 86_400.0


def page_shape(url: str) -> str:
    """The part of a URL that decides which controls a page has.

    Scheme, host and path; **never** the query or fragment. A booking page for
    two different restaurants is the same *shape* — same form, same labels —
    and keying on the full URL would make the cache miss on every record while
    filling up with one entry per row. Fragments and query strings are where
    the per-record identity lives, which is exactly what must not be in the key.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


class PlanCache:
    """Intent to target, keyed by page shape, over any ``CacheStore``.

    Every method is best-effort. A cache that raises would turn a saving into
    an outage, and there is nothing here a caller cannot simply do again.
    """

    PREFIX = "loom:browser:plan"

    def __init__(self, store: Any, *, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 workflow: str = "") -> None:
        self._store = store
        self._ttl = ttl_seconds
        # Scoped per workflow, deliberately. Two workflows can mean different
        # controls by the same words on the same page, and a shared entry would
        # hand one of them the other's answer with no way to tell.
        self._workflow = workflow

    def key(self, url: str, intent: str, role_hint: str = "") -> str:
        return f"{self.PREFIX}:" + stable_hash({
            "workflow": self._workflow,
            "page": page_shape(url),
            "intent": " ".join(intent.split()).lower(),
            "role": role_hint,
        })

    async def get(self, url: str, intent: str, role_hint: str = "") -> Target | None:
        """A previously resolved target, or ``None``. Never raises.

        The caller **must** verify what comes back against the live page before
        acting on it. This returns what was true once, not what is true now.
        """
        try:
            raw = await self._store.get(self.key(url, intent, role_hint))
        except Exception:
            logger.debug("plan cache read failed", exc_info=True)  # the same to a caller
            return None
        if not isinstance(raw, dict) or not raw.get("role"):
            return None
        return Target(
            role=str(raw["role"]), name=str(raw.get("name", "")),
            ordinal=int(raw.get("ordinal", 0)), exact=bool(raw.get("exact", True)),
        )

    async def put(self, url: str, intent: str, target: Target,
                  role_hint: str = "") -> None:
        try:
            await self._store.set(
                self.key(url, intent, role_hint),
                {"role": target.role, "name": target.name,
                 "ordinal": target.ordinal, "exact": target.exact},
                self._ttl,
            )
        except Exception:
            logger.debug("plan cache write failed", exc_info=True)

    async def forget(self, url: str, intent: str, role_hint: str = "") -> None:
        """Drop a stale entry, so the next run does not re-verify it to fail."""
        try:
            await self._store.delete(self.key(url, intent, role_hint))
        except Exception:
            logger.debug("plan cache delete failed", exc_info=True)
