"""Explain 查询（PLAN-13 P13-13）。

ExplainMemoryWeight、ExplainRetrievalSnapshot、KnowledgeResourceExplain 等只读查询。
全部通过 service 层，不直接访问 Repository。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


class ExplainService:
    """记忆权重、检索快照、知识资源解释（PLAN-13 §14.3 Query）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def explain_memory_weight(self, memory_id: str) -> dict | None:
        """解释单条记忆的检索权重分项（PLAN-13 §13 Explain API）。"""
        from cogito.service.memory_weight import explain_weight
        from cogito.store.weight_policy import MemoryWeightPolicy

        row = self._conn.execute(
            "SELECT * FROM memory_items WHERE memory_id=? AND deleted_at IS NULL",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row) if hasattr(row, "keys") else None
        if d is None:
            return None
        now = datetime.now(UTC)
        policy = MemoryWeightPolicy()
        return explain_weight(
            importance=d.get("importance", 0.5),
            explicitness=d.get("explicitness", "model_inference"),
            status=d.get("status", "candidate"),
            kind=d.get("kind", "fact"),
            last_active_at=None,
            now=now,
            reinforcement=d.get("reinforcement", 0),
            emotional_weight=d.get("emotional_weight", 0.5),
            policy=policy,
        )

    def list_memory_sources(self, memory_id: str) -> list[dict]:
        """列出记忆的来源集合。"""
        try:
            rows = self._conn.execute(
                "SELECT * FROM memory_sources "
                "WHERE memory_id=? AND deleted_at IS NULL ORDER BY created_at ASC",
                (memory_id,),
            ).fetchall()
            return [dict(r) if hasattr(r, "keys") else {} for r in rows]
        except sqlite3.OperationalError:
            return []

    def get_memory_detail(self, memory_id: str) -> dict | None:
        """获取记忆详情（安全摘要，不泄漏敏感正文）。"""
        row = self._conn.execute(
            "SELECT memory_id, kind, subject, predicate, value, status, "
            "confidence, importance, explicitness, retrieval_weight, "
            "reinforcement, created_at "
            "FROM memory_items WHERE memory_id=? AND deleted_at IS NULL",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row) if hasattr(row, "keys") else {}

    # ── Knowledge 解释 ────────────────────────────────────────

    def get_knowledge_resource(self, resource_id: str) -> dict | None:
        """获取知识资源详情 + 段统计（解释检索覆盖）。"""
        row = self._conn.execute(
            "SELECT kr.resource_id, kr.source_kind, kr.source_uri_hash, kr.status, "
            "kr.trust_label, kr.source_version, kr.content_hash, kr.created_at, "
            "kr.updated_at, kr.deleted_at, "
            "(SELECT COUNT(*) FROM knowledge_documents kd "
            " WHERE kd.resource_id=kr.resource_id AND kd.deleted_at IS NULL) AS doc_count, "
            "(SELECT COUNT(*) FROM knowledge_segments ks "
            " JOIN knowledge_documents kd2 ON kd2.document_id=ks.document_id "
            " WHERE kd2.resource_id=kr.resource_id AND ks.deleted_at IS NULL) AS segment_count, "
            "(SELECT COUNT(*) FROM knowledge_segments ks2 "
            " JOIN knowledge_documents kd3 ON kd3.document_id=ks2.document_id "
            " WHERE kd3.resource_id=kr.resource_id AND ks2.deleted_at IS NULL "
            " AND ks2.embedding_status='ready') AS embedded_count "
            "FROM knowledge_resources kr "
            "WHERE kr.resource_id=?",
            (resource_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row) if hasattr(row, "keys") else {}

    def list_knowledge_resources(self, *, principal_id: str = "owner",
                                  limit: int = 50, status_filter: str = "") -> list[dict]:
        """列出知识资源摘要（用于 Dashboard / API）。"""
        sql = (
            "SELECT resource_id, source_kind, source_uri_hash, status, trust_label, "
            " source_version, created_at, updated_at "
            "FROM knowledge_resources WHERE deleted_at IS NULL"
        )
        params: list = []
        if principal_id:
            sql += " AND principal_id=?"
            params.append(principal_id)
        if status_filter:
            sql += " AND status=?"
            params.append(status_filter)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) if hasattr(r, "keys") else {} for r in
                self._conn.execute(sql, params).fetchall()]

    def explain_knowledge_retrieval(self, resource_id: str) -> dict | None:
        """解释知识资源是否可被检索、当前检索路径覆盖。"""
        res = self.get_knowledge_resource(resource_id)
        if res is None:
            return None
        status = res.get("status", "")
        embedded = res.get("embedded_count", 0) or 0
        return {
            "resource": res,
            "retrievable": status == "active" and res.get("deleted_at") is None,
            "fts_available": True,
            "embedding_available": embedded > 0,
            "retrieval_path": (
                "keyword+vector" if embedded > 0 and status == "active"
                else "keyword" if status == "active"
                else "none"
            ),
            "status_note": {
                "active": "可检索",
                "stale": "已失效，需 refresh 后重新检索",
                "queued": "排队中，尚未 ingest",
                "deleted": "已删除",
            }.get(status, f"未知状态: {status}"),
        }
