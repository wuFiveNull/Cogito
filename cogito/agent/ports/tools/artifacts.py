# cogito/agent/ports/tools/artifacts.py
#
# Tool Artifact Store Port — persistent storage for large tool outputs.
#
# Design rules (see tool-system-spec §15):
#   - Artifacts are written once and read by reference.
#   - The store manages TTL, deduplication via SHA-256, and cleanup.
#   - Large results (> inline limit) are always materialised as artifacts.

from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator, Protocol

from cogito.agent.domain.tools import ArtifactRef


class ToolArtifactStorePort(Protocol):
    """Persistent storage for tool execution artifacts."""

    async def store(
        self,
        *,
        data: bytes,
        media_type: str,
        name: str | None = None,
        ttl_seconds: int | None = None,
    ) -> ArtifactRef:
        ...

    async def read(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> bytes | None:
        ...

    async def delete(self, artifact_id: str) -> bool:
        ...

    async def cleanup_expired(self) -> int:
        ...
