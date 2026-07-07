"""Memory Repository — memory_items 表查询。

基于已存在的 memory_items 表和 SQLite 连接。
"""

from __future__ import annotations

import sqlite3
from typing import Any


class MemoryRepository:
    """MemoryItem 数据访问层。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def search(
        self,
        query: str,
        limit: int = 5,
        scope: str = "",
    ) -> list[dict[str, Any]]:
        """按文本搜索已确认的 memory_items。

        SELECT 条件：
        - status='confirmed'
        - 可选 scope 过滤
        - value/subject LIKE %query%
        """
        sql = (
            "SELECT memory_id, kind, subject, predicate, value, scope, "
            "       confidence, status, source_type, source_id, created_at "
            "FROM memory_items "
            "WHERE status='confirmed' "
            "AND (value LIKE ? OR subject LIKE ? OR predicate LIKE ?)"
        )
        params = [f"%{query}%", f"%{query}%", f"%{query}%"]

        if scope:
            sql += " AND scope=?"
            params.append(scope)

        sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            result.append({
                "memory_id": row["memory_id"],
                "kind": row["kind"],
                "subject": row["subject"],
                "predicate": row["predicate"],
                "value": row["value"],
                "scope": row["scope"],
                "confidence": row["confidence"],
                "status": row["status"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "created_at": row["created_at"],
            })
        return result

    def get_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取最近的 memory_items。"""
        rows = self._conn.execute(
            "SELECT memory_id, kind, subject, predicate, value, scope, "
            "       confidence, status, source_type, source_id, created_at "
            "FROM memory_items WHERE status='confirmed' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE status='confirmed'",
        ).fetchone()
        return row[0] if row else 0
