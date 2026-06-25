# cogito/agent/ports/prompt_cache.py
#
# PromptCachePort —分层 Prompt 缓存 (Mode 1 from the context-management
# research).
#
# The stable section of the system prompt (identity, behavior rules,
# tool schema descriptions) rarely changes within a session.  This Port
# allows caching the rendered stable section across turns.
#
# Design rules:
#   - Cache entries are immutable strings (fully rendered).
#   - Cache lifetime is scoped to a session_id.
#   - Invalidation is explicit (config/workspace change).
#   - Cache misses are not errors — the caller renders fresh.

from __future__ import annotations

import time
from typing import Protocol


class PromptCachePort(Protocol):
    """Cache for rendered stable prompt sections.

    Each entry is keyed by (session_id, cache_key) where cache_key is
    a hash of the stable inputs (policy text, tool definitions, version).
    """

    async def get(
        self,
        *,
        session_id: str,
        cache_key: str,
    ) -> str | None:
        """Return cached content or None on miss."""
        ...

    async def set(
        self,
        *,
        session_id: str,
        cache_key: str,
        content: str,
    ) -> None:
        """Store rendered content in the cache."""
        ...

    async def invalidate_session(self, session_id: str) -> None:
        """Invalidate all entries for a session (e.g. on config change)."""
        ...

    async def clear(self) -> None:
        """Clear the entire cache (e.g. on global config change)."""
        ...


class InMemoryPromptCache:
    """Thread-safe in-memory implementation of PromptCachePort.

    Entries expire after ``ttl_seconds`` (default 300 = 5 min, matching
    the Anthropic prompt-caching TTL for reference).
    """

    def __init__(self, *, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, str]] = {}  # key → (timestamp, content)

    async def get(
        self,
        *,
        session_id: str,
        cache_key: str,
    ) -> str | None:
        key = self._make_key(session_id, cache_key)
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, content = entry
        if time.monotonic() - ts > self._ttl:
            del self._store[key]
            return None
        return content

    async def set(
        self,
        *,
        session_id: str,
        cache_key: str,
        content: str,
    ) -> None:
        key = self._make_key(session_id, cache_key)
        self._store[key] = (time.monotonic(), content)

    async def invalidate_session(self, session_id: str) -> None:
        prefix = f"{session_id}:"
        self._store = {k: v for k, v in self._store.items() if not k.startswith(prefix)}

    async def clear(self) -> None:
        self._store.clear()

    @staticmethod
    def _make_key(session_id: str, cache_key: str) -> str:
        return f"{session_id}:{cache_key}"
