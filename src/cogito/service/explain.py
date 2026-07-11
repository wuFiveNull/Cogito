"""Explain 查询（PLAN-13 P13-13）。

ExplainMemoryWeight、ExplainRetrievalSnapshot 等只读查询。
全部通过 service 层，不直接访问 Repository。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


class ExplainService:
    """记忆权重与检索快照解释（PLAN-13 §14.3 Query）。"""

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
        if not row:
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
            last_active_at=None,  # 使用 last_retrieved_at 作为 active 标记
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
        if not row:
            return None
        return dict(row) if hasattr(row, "keys") else {}
