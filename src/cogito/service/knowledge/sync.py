"""KnowledgeSync — 来源增删改级联（PLAN-13 P13-10 M5）。

每个 Connector/source root 使用 stable_source_id + content_hash + watermark。
Diff 分类：added、modified、unchanged、deleted。
级联规则：modified → 旧 stale + 新 active；deleted → tombstone + 撤销检索。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from cogito.domain.knowledge import ResourceStatus
from cogito.service.knowledge.embedding import invalidate_resource_segments
from cogito.service.knowledge.service import KnowledgeService

_LOGGER = logging.getLogger("cogito.knowledge.sync")


def sync_resource(
    conn: sqlite3.Connection,
    *,
    stable_source_id: str,
    source_kind: str = "explicit_local_file",
    content_hash: str = "",
    raw_text: str,
    principal_id: str = "",
    trust_label: str = "unverified",
) -> str:
    """同步知识资源（PLAN-13 P13-10）。

    - unchanged（content_hash 未变）→ 跳过，不重新 parse/embed
    - modified → 旧 Resource 标 stale + 新版本 active
    - added → 新建

    返回 resource_id。
    """
    svc = KnowledgeService(conn)
    existing = _find_by_stable_id(conn, stable_source_id, principal_id)

    if existing and existing.get("content_hash") == content_hash:
        # unchanged
        return existing["resource_id"]

    if existing:
        # modified: 标 stale + 新建
        _mark_stale(conn, existing["resource_id"])
        invalidate_resource_segments(conn, existing["resource_id"])

    r = svc.register_resource(
        source_uri_hash=stable_source_id,
        source_kind=source_kind,
        content_hash=content_hash,
        principal_id=principal_id,
        trust_label=trust_label,
        source_version=content_hash[:8],
    )
    svc.ingest(r.resource_id, raw_text)
    _LOGGER.info("Synced resource %s (modified=%s)", r.resource_id, bool(existing))
    return r.resource_id


def delete_resource(
    conn: sqlite3.Connection,
    *,
    stable_source_id: str,
    principal_id: str = "",
) -> bool:
    """删除来源的级联（PLAN-13 P13-10）。

    - Resource deleted/tombstone
    - Segment 从默认检索撤销（FTS清理）
    - 幂等：重复删除返回 True
    """
    existing = _find_by_stable_id(conn, stable_source_id, principal_id)
    if existing is None:
        return True  # 已不存在，幂等成功
    rid = existing["resource_id"]
    invalidate_resource_segments(conn, rid)
    conn.execute(
        "UPDATE knowledge_resources SET status=?, deleted_at=? WHERE resource_id=?",
        (ResourceStatus.deleted.value, datetime.now(UTC).isoformat(), rid),
    )
    conn.commit()
    _LOGGER.info("Deleted resource %s", rid)
    return True


def _find_by_stable_id(
    conn: sqlite3.Connection, stable_source_id: str, principal_id: str,
) -> dict | None:
    row = conn.execute(
        "SELECT resource_id, content_hash FROM knowledge_resources "
        "WHERE source_uri_hash=? AND principal_id=? AND deleted_at IS NULL",
        (stable_source_id, principal_id),
    ).fetchone()
    if not row:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    return {"resource_id": row[0], "content_hash": row[1]}


def _mark_stale(conn: sqlite3.Connection, resource_id: str) -> None:
    from cogito.store import knowledge_repo
    knowledge_repo.update_resource_status(conn, resource_id, ResourceStatus.stale.value)
    conn.execute(
        "UPDATE knowledge_documents SET status='stale' WHERE resource_id=?",
        (resource_id,),
    )
    conn.commit()
