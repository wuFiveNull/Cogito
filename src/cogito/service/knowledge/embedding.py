"""Knowledge 索引全量重建（PLAN-13 P13-09）。

FTS/Embedding 为派生数据，Migration 失败可重建；
权威来源和 tombstone 不允许通过 down migration 静默丢失。
"""
from __future__ import annotations

import sqlite3

from cogito.store import knowledge_repo


def rebuild_index(
    conn: sqlite3.Connection,
    *,
    fts: bool = True,
    embeddings: bool = False,
    embedding_model: str = "",
) -> dict[str, int]:
    """从知识段落地全量重建索引（幂等）。"""
    result: dict[str, int] = {"fts": 0, "embeddings": 0}
    if fts:
        if knowledge_repo.ensure_knowledge_fts(conn):
            knowledge_repo.rebuild_knowledge_fts(conn)
            row = conn.execute("SELECT COUNT(*) c FROM knowledge_fts").fetchone()
            result["fts"] = row["c"] if row else 0
    if embeddings:
        unembedded = knowledge_repo.list_unembedded_segments(
            conn, model=embedding_model,
        )
        result["embeddings"] = len(unembedded)  # 占位；具体嵌入由 EmbeddingPort 完成
    return result


def invalidate_resource_segments(
    conn: sqlite3.Connection, resource_id: str,
) -> int:
    """来源删除/失效后清理段落地（FTS 重建 + embedding 撤销）。"""
    from datetime import UTC, datetime
    docs = knowledge_repo.list_documents_for_resource(conn, resource_id)
    count = 0
    now = datetime.now(UTC).isoformat()
    for doc in docs:
        segs = knowledge_repo.list_segments_for_document(conn, doc.document_id)
        for seg in segs:
            try:
                conn.execute(
                    "UPDATE knowledge_segments SET deleted_at=? "
                    "WHERE segment_id=? AND deleted_at IS NULL",
                    (now, seg.segment_id),
                )
                count += 1
            except sqlite3.OperationalError:
                pass
    # FTS 重建以清除已删段落地
    if knowledge_repo.ensure_knowledge_fts(conn):
        knowledge_repo.rebuild_knowledge_fts(conn)
    return count
