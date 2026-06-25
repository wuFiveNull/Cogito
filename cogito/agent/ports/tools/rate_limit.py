# cogito/agent/ports/tools/rate_limit.py
#
# Tool Rate Limiter Port — per-tool and global rate limiting.
#
# Design rules:
#   - Rate limiting is checked BEFORE concurrency locking.
#   - Per-tool rate limits are defined in ToolLimits.rate_limit_key.
#   - Global rate limits are deployment-level configuration.
#   - Must be safe for concurrent access from multiple turns.

from __future__ import annotations

from typing import Protocol

from cogito.agent.domain.tools import ToolDefinition


class ToolRateLimiterPort(Protocol):
    """Rate-limiter for tool execution slots."""

    async def acquire(
        self,
        *,
        definition: ToolDefinition,
        actor_id: str,
        session_id: str,
    ) -> bool:
        """Try to acquire a rate-limit slot. Returns False if rate-limited."""
        ...

    async def release(
        self,
        *,
        definition: ToolDefinition,
        actor_id: str,
        session_id: str,
    ) -> None:
        """Release a previously acquired slot."""
        ...
