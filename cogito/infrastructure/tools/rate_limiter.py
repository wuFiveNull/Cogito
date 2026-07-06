# cogito/infrastructure/tools/rate_limiter.py
#
# TokenBucketRateLimiter — concrete rate limiter implementation.
#
# Implements ToolRateLimiterPort using a token-bucket algorithm
# per tool and per session.

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Mapping

logger = logging.getLogger(__name__)


@dataclass
class TokenBucket:
    """Token bucket for rate limiting."""
    capacity: float
    refill_rate: float        # tokens per second
    tokens: float
    last_refill: float

    def try_consume(self, tokens: float = 1.0) -> bool:
        """Try to consume *tokens* from the bucket. Returns True if allowed."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def wait_time(self, tokens: float = 1.0) -> float:
        """Return estimated wait time in seconds before *tokens* are available."""
        if self.tokens >= tokens:
            return 0.0
        needed = tokens - self.tokens
        return needed / self.refill_rate if self.refill_rate > 0 else float("inf")


@dataclass
class RateLimiterConfig:
    """Rate limiter configuration.

    Attributes:
        default_capacity:     Default max burst (token bucket capacity)
        default_refill_rate:  Default tokens per second
        max_calls_per_minute: Global max calls per minute (0 = unlimited)
        tool_limits:          Per-tool overrides {tool_name: (capacity, refill_rate)}
    """
    default_capacity: float = 10.0
    default_refill_rate: float = 1.0    # 1 call/second = 60 calls/minute
    max_calls_per_minute: int = 0       # 0 = unlimited
    tool_limits: Mapping[str, tuple[float, float]] = field(default_factory=dict)


class TokenBucketRateLimiter:
    """Rate limiter using token-bucket algorithm per tool.

    Implements a simplified ToolRateLimiterPort interface.
    """

    def __init__(self, config: RateLimiterConfig | None = None) -> None:
        self._config = config or RateLimiterConfig()
        self._buckets: dict[str, TokenBucket] = {}
        self._session_buckets: dict[str, TokenBucket] = {}
        self._global_calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        tool_name: str,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Acquire permission to call *tool_name*.

        Returns True if allowed, False if rate limited.
        Blocks up to *timeout* seconds waiting for tokens.
        """
        start = time.monotonic()
        while True:
            allowed = await self._try_acquire(tool_name, session_id)
            if allowed:
                return True
            if timeout is not None and (time.monotonic() - start) >= timeout:
                return False
            await asyncio.sleep(0.1)

    async def release(self, tool_name: str, *, session_id: str | None = None) -> None:
        """Release rate limit (no-op for token bucket)."""
        pass  # Token bucket auto-refills; no explicit release needed.

    async def _try_acquire(self, tool_name: str, session_id: str | None) -> bool:
        async with self._lock:
            # Global rate limit check
            if self._config.max_calls_per_minute > 0:
                now = time.monotonic()
                self._global_calls = [t for t in self._global_calls if now - t < 60.0]
                if len(self._global_calls) >= self._config.max_calls_per_minute:
                    return False
                self._global_calls.append(now)

            # Per-tool token bucket
            bucket = self._get_or_create_bucket(tool_name)
            if not bucket.try_consume():
                return False

            # Per-session token bucket
            if session_id:
                session_bucket = self._session_buckets.get(session_id)
                if session_bucket is None:
                    session_bucket = TokenBucket(
                        capacity=self._config.default_capacity * 2,
                        refill_rate=self._config.default_refill_rate * 2,
                        tokens=self._config.default_capacity * 2,
                        last_refill=time.monotonic(),
                    )
                    self._session_buckets[session_id] = session_bucket
                if not session_bucket.try_consume():
                    return False

            return True

    def _get_or_create_bucket(self, tool_name: str) -> TokenBucket:
        if tool_name not in self._buckets:
            # Check for tool-specific limits
            limits = self._config.tool_limits.get(tool_name)
            if limits:
                capacity, refill_rate = limits
            else:
                capacity = self._config.default_capacity
                refill_rate = self._config.default_refill_rate

            self._buckets[tool_name] = TokenBucket(
                capacity=capacity,
                refill_rate=refill_rate,
                tokens=capacity,
                last_refill=time.monotonic(),
            )
        return self._buckets[tool_name]

    def get_stats(self, tool_name: str) -> dict:
        """Get current rate limit stats for a tool."""
        bucket = self._buckets.get(tool_name)
        if bucket is None:
            return {"tool": tool_name, "available": "unlimited"}
        return {
            "tool": tool_name,
            "available_tokens": round(bucket.tokens, 1),
            "capacity": bucket.capacity,
            "refill_rate": bucket.refill_rate,
        }
