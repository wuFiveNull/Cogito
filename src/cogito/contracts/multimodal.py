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


class StickerService(Protocol):
    """Scoped service exposed to the built-in sticker tools.

    Reuses the multimodal_assets pipeline (payload, dedup, ownership). A
    sticker is simply an image asset that has been tagged as reusable.
    """

    def save_sticker(
        self,
        asset_id: str,
        *,
        name: str,
        tags: tuple[str, ...] = (),
        principal_id: str,
        session_id: str,
    ) -> dict[str, Any]: ...

    def save_sticker_from_url(
        self,
        url: str,
        *,
        name: str,
        tags: tuple[str, ...] = (),
        principal_id: str,
        session_id: str,
    ) -> dict[str, Any]: ...

    def send_sticker(
        self,
        sticker_id: str,
        *,
        principal_id: str,
        session_id: str,
    ) -> dict[str, Any]: ...

    def list_stickers(
        self,
        *,
        principal_id: str,
        tag: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

