"""In-process rate limiting gateway.

Provides a token-bucket rate limiter for toolset operations.  In embedded
mode this runs in-process with zero network overhead.  In gateway mode
(Phase 5) the same interface is backed by a shared Redis store.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class RateLimitConfig:
    """Configuration for a rate limit group."""

    requests_per_second: float = 10.0
    burst: int = 20
    """Maximum burst size (bucket capacity)."""


@dataclass
class _TokenBucket:
    """Token bucket algorithm."""

    capacity: float
    rate: float  # tokens per second
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def try_acquire(self, tokens: int = 1) -> bool:
        """Try to acquire tokens without blocking.  Returns True on success."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def time_until_available(self, tokens: int = 1) -> float:
        """Seconds until *tokens* tokens will be available."""
        self._refill()
        if self.tokens >= tokens:
            return 0.0
        deficit = tokens - self.tokens
        return deficit / self.rate


class RateLimiter:
    """In-process rate limiter using token buckets.

    Each *group* (e.g. ``salesforce``, ``github.search``) gets its own
    bucket with independent limits.
    """

    def __init__(self) -> None:
        self._configs: dict[str, RateLimitConfig] = {}
        self._buckets: dict[str, _TokenBucket] = {}

    def configure(
        self, group: str, config: RateLimitConfig | None = None
    ) -> None:
        """Set the rate limit for a group."""
        cfg = config or RateLimitConfig()
        self._configs[group] = cfg
        self._buckets[group] = _TokenBucket(
            capacity=cfg.burst, rate=cfg.requests_per_second
        )

    def _get_bucket(self, group: str) -> _TokenBucket:
        if group not in self._buckets:
            self.configure(group)
        return self._buckets[group]

    def try_acquire(self, group: str, tokens: int = 1) -> bool:
        """Try to acquire tokens without blocking."""
        return self._get_bucket(group).try_acquire(tokens)

    async def acquire(
        self, group: str, tokens: int = 1, *, timeout: float = 30.0
    ) -> None:
        """Acquire tokens, waiting if necessary.

        Raises ``TimeoutError`` if tokens are not available within *timeout*.
        """
        bucket = self._get_bucket(group)
        deadline = time.monotonic() + timeout
        while not bucket.try_acquire(tokens):
            wait = min(
                bucket.time_until_available(tokens),
                deadline - time.monotonic(),
            )
            if wait <= 0:
                msg = (
                    f"Rate limit timeout for group '{group}' "
                    f"after {timeout}s"
                )
                raise TimeoutError(msg)
            await asyncio.sleep(wait)

    def reset(self, group: str | None = None) -> None:
        """Reset bucket(s) to full capacity."""
        if group is not None:
            if group in self._buckets:
                cfg = self._configs[group]
                self._buckets[group] = _TokenBucket(
                    capacity=cfg.burst, rate=cfg.requests_per_second
                )
        else:
            for g in list(self._buckets):
                self.reset(g)
