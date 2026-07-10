"""Ports exposed by the independent multimodal perception layer."""

from __future__ import annotations

from typing import Any, Protocol


class MultimodalContextReader(Protocol):
    """Read-only projection used by ContextBuilder."""

    def list_for_message(self, message_id: str) -> list[dict[str, Any]]: ...


class VisionToolService(Protocol):
    """Scoped service exposed to the built-in vision tool."""

    async def analyze_for_tool(
        self,
        asset_id: str,
        *,
        principal_id: str,
        session_id: str,
    ) -> dict[str, Any]: ...

