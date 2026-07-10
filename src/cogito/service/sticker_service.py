"""Live implementation of the StickerService protocol (save + send stickers).

Reuses the multimodal_assets pipeline (payload, SHA256 dedup, ownership guard).
A sticker is an image asset that has been tagged as reusable by the Agent.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Any

from cogito.config import MultimodalConfig
from cogito.domain.message import ContentPart, Message, MessageDirection, MessageRole
from cogito.infrastructure.payload_store import PayloadStore
from cogito.service.asset_service import AssetIngestionService, AssetValidationError
from cogito.store.multimodal_repo import MultimodalRepository, now_ms

_LOGGER = logging.getLogger("cogito.sticker_service")


class SqliteStickerService:
    """MultimodalRepository-backed sticker save + send.

    ``delivery_service`` is optional: without it ``send_sticker`` gracefully
    reports that outbound delivery is unavailable (e.g. in unit tests or
    contexts without an enqueue path).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        multimodal_repo: MultimodalRepository,
        payload_store: PayloadStore,
        config: MultimodalConfig,
        asset_service: AssetIngestionService,
        delivery_service: Any = None,
        metrics: Any = None,
    ) -> None:
        self._conn = conn
        self._repo = multimodal_repo
        self._payload_store = payload_store
        self._config = config
        self._asset_service = asset_service
        self._delivery_service = delivery_service
        self._metrics = metrics

    # ── save from an existing asset ────────────────────────────────────────

    def save_sticker(
        self,
        asset_id: str,
        *,
        name: str,
        tags: tuple[str, ...] = (),
        principal_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        if not self._repo.is_accessible(
            asset_id, principal_id=principal_id, session_id=session_id,
        ):
            return {"status": "denied", "error": "asset_access_denied"}
        asset = self._repo.get_asset(asset_id)
        if asset is None:
            return {"status": "error", "error": "asset_not_found"}
        if asset.media_kind != "image":
            return {"status": "error", "error": "only image assets can be stickers"}
        self._repo.mark_as_sticker(asset_id, name=name, tags=tags)
        if self._metrics is not None:
            self._metrics.record_sticker_saved()
        return {"status": "saved", "sticker_id": asset_id, "name": name}

    # ── save from a URL (SSRF-safe) ────────────────────────────────────────

    def save_sticker_from_url(
        self,
        url: str,
        *,
        name: str,
        tags: tuple[str, ...] = (),
        principal_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        from cogito.infrastructure.safe_http import SafeHttpError, fetch_url_bytes

        try:
            data = fetch_url_bytes(
                url,
                allowed_hosts=self._config.allowed_sticker_hosts,
                max_bytes=self._config.max_file_bytes,
            )
        except SafeHttpError as exc:
            return {"status": "error", "error": f"fetch_blocked: {exc}"}
        except Exception as exc:
            return {"status": "error", "error": f"fetch_failed: {exc}"}

        # Reuse the asset ingestion validators without going through InboundService.
        try:
            asset = self._ingest_bytes_as_asset(data, principal_id)
        except AssetValidationError as exc:
            return {"status": "error", "error": f"invalid_image: {exc}"}
        if asset is None:
            return {"status": "error", "error": "image_rejected"}
        self._repo.mark_as_sticker(asset.asset_id, name=name, tags=tags)
        if self._metrics is not None:
            self._metrics.record_sticker_saved()
        return {"status": "saved", "sticker_id": asset.asset_id, "name": name}

    def _ingest_bytes_as_asset(self, data: bytes, principal_id: str) -> Any:
        """Validate bytes through the same pipeline the inbound path uses."""

        from cogito.domain.multimodal import MultimodalAsset

        sniffed = AssetIngestionService._sniff_mime(data)
        if sniffed is None:
            raise AssetValidationError("unsupported image format")
        if sniffed not in self._config.allowed_mime_types:
            raise AssetValidationError(f"unsupported MIME: {sniffed}")
        try:
            AssetIngestionService._validate_dimensions(data)
        except AssetValidationError:
            raise

        payload = self._payload_store.put(data, content_type=sniffed)
        existing = self._repo.find_asset_by_sha256(payload.sha256)
        if existing is not None:
            return existing
        asset = MultimodalAsset(
            asset_id=uuid.uuid4().hex,
            payload_ref=payload.payload_id,
            sha256=payload.sha256,
            perceptual_hash=AssetIngestionService._compute_phash(data),
            media_kind="image",
            mime_type=sniffed,
            size_bytes=payload.size_bytes,
            created_by_principal_id=principal_id,
            created_at=now_ms(),
        )
        self._repo.insert_asset(asset)
        self._conn.execute(
            "INSERT OR IGNORE INTO payload_objects "
            "(payload_ref,sha256,content_type,size,storage_path,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (payload.payload_id, payload.sha256, sniffed,
             payload.size_bytes, payload.storage_uri, now_ms()),
        )
        self._conn.commit()
        return asset

    # ── send (outbound image) ──────────────────────────────────────────────

    def send_sticker(
        self,
        sticker_id: str,
        *,
        principal_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        if not self._repo.is_accessible(
            sticker_id, principal_id=principal_id, session_id=session_id,
        ):
            return {"status": "denied", "error": "asset_access_denied"}
        asset = self._repo.get_sticker(sticker_id)
        if asset is None:
            return {"status": "error", "error": "sticker_not_found"}

        if self._delivery_service is None:
            return {"status": "error", "error": "delivery_not_configured"}

        target = self._resolve_target(session_id)
        if target is None:
            return {"status": "error", "error": "no_outbound_target"}

        message = Message(
            message_id=uuid.uuid4().hex,
            conversation_id=target["conversation_id"],
            session_id=session_id,
            sender_principal_id="agent",
            role=MessageRole.assistant,
            direction=MessageDirection.outbound,
            content_parts=[ContentPart(
                content_type="image",
                payload_ref=asset.payload_ref,
                size=asset.size_bytes,
                sha256=asset.sha256,
                metadata={"mime": asset.mime_type, "name": asset.sticker_name or "sticker"},
                trust_label="internal",
            )],
        )
        # Persist the outbound message directly (bypasses InboundService).
        from cogito.store.repositories import MessageRepository
        MessageRepository(self._conn).insert(message)
        for part in message.content_parts:
            MessageRepository(self._conn).insert_content_part(part, message.message_id)
        self._conn.commit()

        from cogito.service.delivery_service import DeliveryRequest
        req = DeliveryRequest(
            target=target["target_snapshot"],
            content_ref=message.message_id,
        )
        import asyncio
        try:
            asyncio.get_running_loop()
            return {"status": "error", "error": "use_async_context_for_delivery"}
        except RuntimeError:
            ref = asyncio.run(self._delivery_service.enqueue(req))
        self._repo.record_sticker_usage(sticker_id)
        if self._metrics is not None:
            self._metrics.record_sticker_sent()
        return {"status": "sent", "delivery_id": ref.delivery_id, "sticker_id": sticker_id}

    def _resolve_target(self, session_id: str) -> dict[str, Any] | None:
        """Derive an outbound target from the session's most recent inbound row."""
        row = self._conn.execute(
            "SELECT m.sender_endpoint_id, m.platform_message_id, c.conversation_id, "
            "       c.conversation_endpoint_ref "
            "FROM messages m JOIN conversations c ON c.conversation_id=m.conversation_id "
            "WHERE m.session_id=? AND m.role='user' "
            "ORDER BY m.receive_sequence DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        target_snapshot = {
            "adapter_id": row["sender_endpoint_id"],
            "conversation_id": row["conversation_id"],
            "target_endpoint_ref": row["conversation_endpoint_ref"],
            "reply_route": {
                "reply_to_platform_message_id": row["platform_message_id"],
            },
        }
        return {
            "conversation_id": row["conversation_id"],
            "target_snapshot": target_snapshot,
        }

    # ── list ───────────────────────────────────────────────────────────────

    def list_stickers(
        self,
        *,
        principal_id: str,
        tag: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._repo.list_stickers(
            principal_id=principal_id, tag=tag, limit=limit,
        )


__all__ = ["SqliteStickerService"]
