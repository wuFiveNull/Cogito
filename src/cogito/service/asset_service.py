"""Safe ingestion of image ContentParts into the Core-owned Payload Store."""

from __future__ import annotations

import base64
import binascii
import io
import sqlite3
import uuid
from typing import Any

from cogito.config import MultimodalConfig
from cogito.domain.message import ContentPart
from cogito.domain.multimodal import MultimodalAsset
from cogito.infrastructure.payload_store import PayloadStore
from cogito.store.multimodal_repo import MultimodalRepository, now_ms


class AssetValidationError(ValueError):
    pass


class AssetIngestionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        payload_root: str,
        config: MultimodalConfig,
    ) -> None:
        self._conn = conn
        self._payload_store = PayloadStore(payload_root, conn)
        self._repo = MultimodalRepository(conn)
        self._config = config

    def materialize_part(
        self,
        part: ContentPart,
        *,
        principal_id: str,
    ) -> MultimodalAsset | None:
        """Persist an inline image and replace it with a stable payload reference.

        Remote URLs are deliberately not fetched by Core. A Gateway must upload
        authenticated platform bytes first and provide payload_ref.
        """
        if not self._config.enabled or not self._is_image(part):
            return None

        metadata = dict(part.metadata)
        mime_type = self._resolve_mime(part, metadata)
        if mime_type not in self._config.allowed_mime_types:
            raise AssetValidationError(f"unsupported image MIME type: {mime_type}")

        if part.payload_ref:
            row = self._conn.execute(
                "SELECT * FROM payload_objects WHERE payload_ref=?",
                (part.payload_ref,),
            ).fetchone()
            if row is None:
                raise AssetValidationError("payload_ref does not exist")
            asset = self._repo.find_asset_by_sha256(row["sha256"])
            if asset is None:
                asset = MultimodalAsset(
                    asset_id=uuid.uuid4().hex,
                    payload_ref=row["payload_ref"],
                    sha256=row["sha256"],
                    media_kind="image",
                    mime_type=row["content_type"] or mime_type,
                    size_bytes=row["size"],
                    created_by_principal_id=principal_id,
                    created_at=now_ms(),
                )
                self._repo.insert_asset(asset)
            part.metadata = {**metadata, "asset_id": asset.asset_id, "mime": asset.mime_type}
            return asset

        decoded = self._decode_inline(part.inline_data, metadata)
        if decoded is None:
            return None
        if len(decoded) > self._config.max_file_bytes:
            raise AssetValidationError("image exceeds max_file_bytes")

        sniffed = self._sniff_mime(decoded)
        if sniffed is None:
            raise AssetValidationError("image bytes do not match a supported format")
        if sniffed != mime_type:
            mime_type = sniffed
        if mime_type not in self._config.allowed_mime_types:
            raise AssetValidationError(f"unsupported image MIME type: {mime_type}")

        self._validate_dimensions(decoded)
        payload = self._payload_store.put(decoded, content_type=mime_type)
        self._conn.execute(
            "INSERT OR IGNORE INTO payload_objects "
            "(payload_ref,sha256,content_type,size,storage_path,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                payload.payload_id,
                payload.sha256,
                payload.content_type,
                payload.size_bytes,
                payload.storage_uri,
                now_ms(),
            ),
        )

        asset = self._repo.find_asset_by_sha256(payload.sha256)
        if asset is None:
            asset = MultimodalAsset(
                asset_id=uuid.uuid4().hex,
                payload_ref=payload.payload_id,
                sha256=payload.sha256,
                perceptual_hash=self._compute_phash(decoded),
                media_kind="image",
                mime_type=mime_type,
                size_bytes=payload.size_bytes,
                created_by_principal_id=principal_id,
                created_at=now_ms(),
            )
            self._repo.insert_asset(asset)

        part.inline_data = ""
        part.payload_ref = payload.payload_id
        part.size = payload.size_bytes
        part.sha256 = payload.sha256
        part.metadata = {**metadata, "asset_id": asset.asset_id, "mime": mime_type}
        return asset

    def link_part(self, message_id: str, part: ContentPart) -> None:
        asset_id = str(part.metadata.get("asset_id", ""))
        if not asset_id:
            return
        self._repo.link_message_asset(
            message_id=message_id,
            part_id=part.part_id,
            asset_id=asset_id,
            ordinal=part.ordinal,
            original_filename=str(
                part.metadata.get("name") or part.metadata.get("filename") or ""
            ),
        )

    @staticmethod
    def _is_image(part: ContentPart) -> bool:
        return part.content_type == "image" or part.content_type.startswith("image/")

    @staticmethod
    def _resolve_mime(part: ContentPart, metadata: dict[str, Any]) -> str:
        if part.inline_data.startswith("data:"):
            return part.inline_data[5:].split(";", 1)[0]
        if part.content_type.startswith("image/"):
            return part.content_type
        return str(metadata.get("mime") or metadata.get("content_type") or "image/png")

    @staticmethod
    def _decode_inline(value: str, metadata: dict[str, Any]) -> bytes | None:
        if not value or value.startswith(("http://", "https://")):
            return None
        encoded = value
        if value.startswith("data:"):
            try:
                header, encoded = value.split(",", 1)
            except ValueError as exc:
                raise AssetValidationError("invalid data URI") from exc
            if ";base64" not in header:
                raise AssetValidationError("only base64 data URIs are supported")
        elif metadata.get("encoding") != "base64":
            # Legacy adapters sometimes use opaque IDs/URLs in inline_data.
            return None
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AssetValidationError("invalid base64 image payload") from exc

    def _validate_dimensions(self, data: bytes) -> None:
        try:
            from PIL import Image
        except ImportError:
            # File-size and magic checks still apply; Pillow is an optional runtime extra.
            return
        try:
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise AssetValidationError("invalid image dimensions")
                if width * height > self._config.max_image_pixels:
                    raise AssetValidationError("image exceeds max_image_pixels")
                image.verify()
        except AssetValidationError:
            raise
        except Exception as exc:
            raise AssetValidationError("image decode failed") from exc

    @staticmethod
    def _sniff_mime(data: bytes) -> str | None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return None

    @staticmethod
    def _compute_phash(data: bytes) -> str:
        try:
            import imagehash
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                return str(imagehash.phash(image))
        except (ImportError, Exception):
            return ""
