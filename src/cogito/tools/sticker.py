"""Sticker tools: save an image as a reusable sticker and send one.

Three tools, all in toolset ("core", "multimodal") — gated by the multimodal
layer being enabled, mirroring the vision tool.

- ``save_sticker``: tag an existing in-conversation image as a sticker.
- ``save_sticker_from_url``: SSRF-safe download + ingest as sticker.
- ``send_sticker``: emit an image content_part through the Delivery path.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from cogito.capability.models import ToolContext, ToolDef
from cogito.contracts.multimodal import StickerService


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def create_save_sticker_def(
    make_service: Callable[[], StickerService] | None = None,
) -> ToolDef:
    async def handler(args: dict, context: ToolContext) -> str:
        if make_service is None:
            return _dumps({"status": "unavailable", "error": "sticker_service_not_configured"})
        svc = make_service()
        return _dumps(
            svc.save_sticker(
                str(args.get("asset_id", "")),
                name=str(args.get("name", "sticker")),
                tags=tuple(str(t) for t in args.get("tags", ())),
                principal_id=context.principal_id,
                session_id=context.session_id,
            )
        )

    return ToolDef(
        name="save_sticker",
        description=(
            "Save an image the user shared as a reusable sticker (表情包). Call when the "
            "user explicitly asks to keep an image as a sticker, or when an image would "
            "make a good sticker. After saving, the sticker can be resent with "
            "send_sticker. Only images the current principal can access are eligible."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "asset_id": {
                    "type": "string",
                    "description": "asset_id of an image in the current conversation.",
                },
                "name": {
                    "type": "string",
                    "description": "Short display name for the sticker (max 200 chars).",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for later retrieval.",
                },
            },
            "required": ["asset_id", "name"],
            "additionalProperties": False,
        },
        toolset=("core", "multimodal"),
        handler=handler,
        permissions=("sticker.write",),
        risk_level="low",
        side_effect_class="idempotent",
        resource_requirements={"network_requests": 0, "model_calls": 0},
        output_schema={"type": "object"},
    )


def create_save_sticker_from_url_def(
    make_service: Callable[[], StickerService] | None = None,
) -> ToolDef:
    async def handler(args: dict, context: ToolContext) -> str:
        if make_service is None:
            return _dumps({"status": "unavailable", "error": "sticker_service_not_configured"})
        svc = make_service()
        return _dumps(
            svc.save_sticker_from_url(
                str(args.get("url", "")),
                name=str(args.get("name", "sticker")),
                tags=tuple(str(t) for t in args.get("tags", ())),
                principal_id=context.principal_id,
                session_id=context.session_id,
            )
        )

    return ToolDef(
        name="save_sticker_from_url",
        description=(
            "Download an image from a URL and save it as a reusable sticker. The fetch "
            "is restricted to public addresses and the configured allowed_sticker_hosts "
            "whitelist (private networks are always blocked). Only use when the user "
            "explicitly provides a URL for a sticker."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public image URL to fetch."},
                "name": {"type": "string", "description": "Short display name for the sticker."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags.",
                },
            },
            "required": ["url", "name"],
            "additionalProperties": False,
        },
        toolset=("core", "multimodal"),
        handler=handler,
        permissions=("network.http", "sticker.write"),
        risk_level="medium",
        side_effect_class="idempotent",
        resource_requirements={"network_requests": 1},
        output_schema={"type": "object"},
    )


def create_send_sticker_def(
    make_service: Callable[[], StickerService] | None = None,
) -> ToolDef:
    async def handler(args: dict, context: ToolContext) -> str:
        if make_service is None:
            return _dumps({"status": "unavailable", "error": "sticker_service_not_configured"})
        svc = make_service()
        return _dumps(
            svc.send_sticker(
                str(args.get("sticker_id", "")),
                principal_id=context.principal_id,
                session_id=context.session_id,
            )
        )

    return ToolDef(
        name="send_sticker",
        description=(
            "Send a previously saved sticker (表情包) to the current conversation. "
            "Use in casual chat when an emoji-like reaction fits — the user seems "
            "amused, you are playfully teasing, or a sticker captures the mood better "
            "than words. Keep usage occasional so it does not feel spammy."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sticker_id": {
                    "type": "string",
                    "description": "sticker_id returned by save_sticker.",
                },
            },
            "required": ["sticker_id"],
            "additionalProperties": False,
        },
        toolset=("core", "multimodal"),
        handler=handler,
        permissions=("message.send",),
        risk_level="medium",
        # Delivery can succeed even when the response is lost, so never retry it
        # automatically without an application-specific reconciliation handler.
        side_effect_class="non_retriable",
        resource_requirements={"network_requests": 1, "model_calls": 0},
        output_schema={"type": "object"},
    )


__all__ = [
    "create_save_sticker_def",
    "create_save_sticker_from_url_def",
    "create_send_sticker_def",
]
