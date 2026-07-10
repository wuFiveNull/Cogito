"""Persistence for multimodal assets and versioned vision analyses."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from cogito.domain.multimodal import (
    AssetStatus,
    MultimodalAsset,
    VisionAnalysis,
    VisionAnalysisStatus,
)


def now_ms() -> int:
    return int(time.time() * 1000)


class MultimodalRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── Asset / message link ──────────────────────────────────────────────

    def find_asset_by_sha256(self, sha256: str) -> MultimodalAsset | None:
        row = self._conn.execute(
            "SELECT * FROM multimodal_assets WHERE sha256=? AND status<>'deleted'",
            (sha256,),
        ).fetchone()
        return self._asset_from_row(row) if row else None

    def get_asset(self, asset_id: str) -> MultimodalAsset | None:
        row = self._conn.execute(
            "SELECT * FROM multimodal_assets WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        return self._asset_from_row(row) if row else None

    def insert_asset(self, asset: MultimodalAsset) -> None:
        self._conn.execute(
            "INSERT INTO multimodal_assets (asset_id,payload_ref,sha256,"
            "perceptual_hash,media_kind,mime_type,size_bytes,"
            "created_by_principal_id,status,retention_class,version,created_at,deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                asset.asset_id,
                asset.payload_ref,
                asset.sha256,
                asset.perceptual_hash,
                asset.media_kind,
                asset.mime_type,
                asset.size_bytes,
                asset.created_by_principal_id,
                asset.status.value,
                asset.retention_class,
                asset.version,
                asset.created_at,
                asset.deleted_at,
            ),
        )

    def link_message_asset(
        self,
        *,
        message_id: str,
        part_id: str,
        asset_id: str,
        ordinal: int,
        original_filename: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO message_asset_links "
            "(message_id,part_id,asset_id,ordinal,original_filename,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (message_id, part_id, asset_id, ordinal, original_filename, now_ms()),
        )

    # ── Sticker semantics (migration 1005) ─────────────────────────────────

    def mark_as_sticker(
        self,
        asset_id: str,
        *,
        name: str,
        tags: tuple[str, ...] = (),
    ) -> bool:
        """Tag an asset as a reusable sticker. Caller must verify accessibility."""
        self._conn.execute(
            "UPDATE multimodal_assets SET is_sticker=1, sticker_name=?, tags_json=? "
            "WHERE asset_id=? AND is_sticker=0",
            (name[:200], json.dumps(list(tags), ensure_ascii=False)[:2000], asset_id),
        )
        self._conn.commit()
        return True

    def record_sticker_usage(self, asset_id: str) -> None:
        self._conn.execute(
            "UPDATE multimodal_assets SET usage_count = usage_count + 1 "
            "WHERE asset_id=?",
            (asset_id,),
        )
        self._conn.commit()

    def list_stickers(
        self,
        *,
        principal_id: str,
        tag: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if tag:
            # LIKE on the JSON array; tags are short tokens so substring is adequate.
            pattern = f'%"{tag}"%'
            rows = self._conn.execute(
                "SELECT * FROM multimodal_assets "
                "WHERE is_sticker=1 AND created_by_principal_id=? "
                "AND status='available' AND tags_json LIKE ? "
                "ORDER BY usage_count DESC, created_at DESC LIMIT ?",
                (principal_id, pattern, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM multimodal_assets "
                "WHERE is_sticker=1 AND created_by_principal_id=? "
                "AND status='available' "
                "ORDER BY usage_count DESC, created_at DESC LIMIT ?",
                (principal_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_sticker(self, sticker_id: str) -> MultimodalAsset | None:
        row = self._conn.execute(
            "SELECT * FROM multimodal_assets WHERE asset_id=? AND is_sticker=1",
            (sticker_id,),
        ).fetchone()
        return self._asset_from_row(row) if row else None

    def list_message_assets(self, message_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT a.*, l.ordinal, l.original_filename "
            "FROM message_asset_links l "
            "JOIN multimodal_assets a ON a.asset_id=l.asset_id "
            "WHERE l.message_id=? AND a.status='available' "
            "ORDER BY l.ordinal ASC, l.part_id ASC",
            (message_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def is_accessible(self, asset_id: str, *, principal_id: str, session_id: str) -> bool:
        if session_id:
            row = self._conn.execute(
                "SELECT 1 FROM message_asset_links l "
                "JOIN messages m ON m.message_id=l.message_id "
                "WHERE l.asset_id=? AND m.session_id=? LIMIT 1",
                (asset_id, session_id),
            ).fetchone()
            return row is not None
        if principal_id:
            row = self._conn.execute(
                "SELECT 1 FROM multimodal_assets "
                "WHERE asset_id=? AND created_by_principal_id=? AND status='available'",
                (asset_id, principal_id),
            ).fetchone()
            return row is not None
        return False

    # ── Analysis cache / claim ────────────────────────────────────────────

    def get_or_create_analysis(
        self,
        *,
        asset_id: str,
        analysis_kind: str,
        model_id: str,
        prompt_version: str,
        result_schema_version: str,
        options_hash: str,
    ) -> VisionAnalysis:
        analysis_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT OR IGNORE INTO vision_analyses "
            "(analysis_id,asset_id,analysis_kind,model_id,prompt_version,"
            "result_schema_version,options_hash,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,'queued',?)",
            (
                analysis_id,
                asset_id,
                analysis_kind,
                model_id,
                prompt_version,
                result_schema_version,
                options_hash,
                now_ms(),
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM vision_analyses WHERE asset_id=? AND analysis_kind=? "
            "AND model_id=? AND prompt_version=? AND result_schema_version=? "
            "AND options_hash=?",
            (
                asset_id,
                analysis_kind,
                model_id,
                prompt_version,
                result_schema_version,
                options_hash,
            ),
        ).fetchone()
        if row is None:  # pragma: no cover - protected by INSERT/UNIQUE
            raise RuntimeError("vision analysis cache row was not created")
        return self._analysis_from_row(row)

    def get_analysis(self, analysis_id: str) -> VisionAnalysis | None:
        row = self._conn.execute(
            "SELECT * FROM vision_analyses WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        return self._analysis_from_row(row) if row else None

    def find_analysis(
        self,
        *,
        asset_id: str,
        analysis_kind: str,
        model_id: str,
        prompt_version: str,
        result_schema_version: str,
        options_hash: str,
    ) -> VisionAnalysis | None:
        row = self._conn.execute(
            "SELECT * FROM vision_analyses WHERE asset_id=? AND analysis_kind=? "
            "AND model_id=? AND prompt_version=? AND result_schema_version=? "
            "AND options_hash=?",
            (
                asset_id,
                analysis_kind,
                model_id,
                prompt_version,
                result_schema_version,
                options_hash,
            ),
        ).fetchone()
        return self._analysis_from_row(row) if row else None

    def claim_analysis(self, analysis_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE vision_analyses SET status='running',started_at=?,"
            "error_category='',retryable=0 "
            "WHERE analysis_id=? AND status='queued'",
            (now_ms(), analysis_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def requeue_analysis(self, analysis_id: str) -> None:
        self._conn.execute(
            "UPDATE vision_analyses SET status='queued',started_at=NULL,"
            "completed_at=NULL,retryable=1 WHERE analysis_id=? AND status='running'",
            (analysis_id,),
        )
        self._conn.commit()

    def retry_failed_analysis(self, analysis_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE vision_analyses SET status='queued',started_at=NULL,completed_at=NULL "
            "WHERE analysis_id=? AND status='failed' AND retryable=1",
            (analysis_id,),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def complete_analysis(
        self,
        analysis_id: str,
        *,
        short_description: str,
        detailed_description: str,
        extracted_text: str,
        objects: list[str],
        document_type: str,
        metadata: dict[str, Any],
    ) -> None:
        self._conn.execute(
            "UPDATE vision_analyses SET status='succeeded',short_description=?,"
            "detailed_description=?,extracted_text=?,objects_json=?,document_type=?,"
            "metadata_json=?,error_category='',retryable=0,completed_at=? "
            "WHERE analysis_id=? AND status='running'",
            (
                short_description,
                detailed_description,
                extracted_text,
                json.dumps(objects, ensure_ascii=False),
                document_type,
                json.dumps(metadata, ensure_ascii=False),
                now_ms(),
                analysis_id,
            ),
        )
        self._conn.commit()

    def fail_analysis(self, analysis_id: str, *, category: str, retryable: bool) -> None:
        self._conn.execute(
            "UPDATE vision_analyses SET status='failed',error_category=?,retryable=?,"
            "completed_at=? WHERE analysis_id=?",
            (category[:100], int(retryable), now_ms(), analysis_id),
        )
        self._conn.commit()

    def enqueue_analysis_task(self, analysis_id: str) -> str:
        task_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT OR IGNORE INTO tasks "
            "(task_id,task_type,payload_ref,status,priority,retry_policy,"
            "idempotency_key,origin,created_at) "
            "VALUES (?, 'vision.analyze', ?, 'queued', 60, ?, ?, 'multimodal', ?)",
            (
                task_id,
                json.dumps({"analysis_id": analysis_id}),
                json.dumps({"max_attempts": 3, "backoff_seconds": [5, 30, 120]}),
                f"vision.analyze:{analysis_id}",
                now_ms(),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT task_id FROM tasks WHERE task_type='vision.analyze' AND idempotency_key=?",
            (f"vision.analyze:{analysis_id}",),
        ).fetchone()
        if row is not None:
            self._conn.execute(
                "UPDATE tasks SET status='queued',scheduled_at=NULL "
                "WHERE task_id=? AND status IN ('failed','cancelled')",
                (row[0],),
            )
            self._conn.commit()
        return str(row[0]) if row else task_id

    @staticmethod
    def _asset_from_row(row: sqlite3.Row) -> MultimodalAsset:
        # Migration 1005 adds sticker columns; tolerate older schemas by reading
        # with defaults when the columns are not present in the result set.
        def _col(key: str, default: Any) -> Any:
            try:
                return row[key]
            except (IndexError, KeyError):
                return default

        return MultimodalAsset(
            asset_id=row["asset_id"],
            payload_ref=row["payload_ref"],
            sha256=row["sha256"],
            perceptual_hash=row["perceptual_hash"],
            media_kind=row["media_kind"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            created_by_principal_id=row["created_by_principal_id"],
            status=AssetStatus(row["status"]),
            retention_class=row["retention_class"],
            version=row["version"],
            created_at=row["created_at"],
            deleted_at=row["deleted_at"],
            is_sticker=bool(_col("is_sticker", 0)),
            sticker_name=_col("sticker_name", ""),
            tags=tuple(json.loads(_col("tags_json", "[]") or "[]")),
            usage_count=_col("usage_count", 0),
        )

    @staticmethod
    def _analysis_from_row(row: sqlite3.Row) -> VisionAnalysis:
        return VisionAnalysis(
            analysis_id=row["analysis_id"],
            asset_id=row["asset_id"],
            analysis_kind=row["analysis_kind"],
            model_id=row["model_id"],
            prompt_version=row["prompt_version"],
            result_schema_version=row["result_schema_version"],
            options_hash=row["options_hash"],
            status=VisionAnalysisStatus(row["status"]),
            short_description=row["short_description"],
            detailed_description=row["detailed_description"],
            extracted_text=row["extracted_text"],
            objects=tuple(json.loads(row["objects_json"] or "[]")),
            document_type=row["document_type"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            result_payload_ref=row["result_payload_ref"],
            error_category=row["error_category"],
            retryable=bool(row["retryable"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
