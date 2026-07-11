"""KnowledgeSync — 来源增删改级联（PLAN-13 P13-10 M5）。

每个 Connector/source root 使用 stable_source_id + content_hash + watermark。
Diff 分类：added、modified、unchanged、deleted。
级联规则：modified → 旧 stale + 新 active；deleted → tombstone + 撤销检索。
"""
from __future__ import annotations

import logging
import sqlite3
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
    resource_id = KnowledgeService(conn).sync_source(
        stable_source_id=stable_source_id,
        source_kind=source_kind,
        content_hash=content_hash,
        raw_text=raw_text,
        principal_id=principal_id,
        trust_label=trust_label,
    )
    _LOGGER.info("Synced resource %s", resource_id)
    return resource_id


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
    return KnowledgeService(conn).delete_source(
        stable_source_id=stable_source_id, principal_id=principal_id,
    )


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


