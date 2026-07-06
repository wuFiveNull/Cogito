"""
cogito.database.repository.memories — MemoryRepository

对应设计文档第 12 节：memories 表和 FTS5 索引的数据访问。
"""

from __future__ import annotations

import re
from typing import Any

from cogito.database.connection import AsyncDatabase
from cogito.database.ids import new_uuid


def sanitize_fts_query(raw: str) -> str:
    """净化 FTS5 查询字符串，移除特殊字符避免语法错误。

    FTS5 的特殊字符包括: ^ * " ( ) + - ~ ?
    对 trigram tokenizer，简单的做法是过滤掉这些字符，
    让每个词独立匹配。
    """
    if not raw or not raw.strip():
        return ""

    # 移除 FTS5 特殊字符，保留字母、数字、中日韩文字、下划线
    cleaned = re.sub(r'[\^"\(\)\+\~\?\*]', ' ', raw)
    # 拆分单词，过滤空串
    words = [w for w in cleaned.split() if len(w.strip()) > 0]
    if not words:
        return ""

    return " AND ".join(f'"{w}"' for w in words)


class MemoryRepository:
    """memories 表及 FTS5 索引的数据访问对象。"""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── 创建 ────────────────────────────────────────────────────────

    async def insert(self, params: dict[str, Any]) -> dict[str, Any]:
        """插入一条记忆记录。

        FTS5 索引由触发器自动同步。
        """
        if "id" not in params or not params["id"]:
            params["id"] = new_uuid()

        columns = ", ".join(params.keys())
        placeholders = ", ".join(f":{k}" for k in params.keys())

        sql = f"""
            INSERT INTO memories ({columns})
            VALUES ({placeholders})
            RETURNING *
        """
        row = await self._db.fetchone(sql, params)
        assert row is not None
        return row

    # ── 查询 ────────────────────────────────────────────────────────

    async def get_by_id(self, memory_id: str) -> dict[str, Any] | None:
        """根据 memory id 查询。"""
        return await self._db.fetchone(
            "SELECT * FROM memories WHERE id = :id",
            {"id": memory_id},
        )

    async def get_active_by_key(
        self,
        user_id: str,
        memory_key: str,
    ) -> dict[str, Any] | None:
        """查询用户某个 key 的 active 记忆。"""
        return await self._db.fetchone(
            "SELECT * FROM memories "
            "WHERE user_id = :user_id "
            "AND memory_key = :memory_key "
            "AND status = 'active'",
            {"user_id": user_id, "memory_key": memory_key},
        )

    async def get_active_by_user(
        self,
        user_id: str,
        status: str = "active",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询用户的所有活跃记忆。"""
        return await self._db.fetchall(
            "SELECT * FROM memories "
            "WHERE user_id = :user_id AND status = :status "
            "ORDER BY importance DESC, created_at DESC "
            "LIMIT :limit",
            {"user_id": user_id, "status": status, "limit": limit},
        )

    async def get_active_by_type(
        self,
        user_id: str,
        memory_type: str,
        status: str = "active",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """按类型查询活跃记忆。"""
        return await self._db.fetchall(
            "SELECT * FROM memories "
            "WHERE user_id = :user_id "
            "AND memory_type = :memory_type "
            "AND status = :status "
            "ORDER BY importance DESC, created_at DESC "
            "LIMIT :limit",
            {
                "user_id": user_id,
                "memory_type": memory_type,
                "status": status,
                "limit": limit,
            },
        )

    async def search_fts(
        self,
        user_id: str,
        fts_query: str,
        query_time: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """FTS5 全文检索。

        对应文档第 14.2 节。
        """
        safe_query = sanitize_fts_query(fts_query)
        if not safe_query:
            return []

        return await self._db.fetchall(
            "SELECT m.*, bm25(memories_fts) AS lexical_rank "
            "FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE memories_fts MATCH :fts_query "
            "AND m.user_id = :user_id "
            "AND m.status = 'active' "
            "AND (m.valid_from IS NULL OR m.valid_from <= :query_time) "
            "AND (m.valid_until IS NULL OR m.valid_until > :query_time) "
            "ORDER BY lexical_rank "
            "LIMIT :limit",
            {
                "user_id": user_id,
                "fts_query": safe_query,
                "query_time": query_time,
                "limit": limit,
            },
        )

    async def search_like(
        self,
        user_id: str,
        keyword: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """短关键词 LIKE 回退检索。

        对应文档第 14.3 节。
        """
        return await self._db.fetchall(
            "SELECT * FROM memories "
            "WHERE user_id = :user_id "
            "AND status = 'active' "
            "AND (content LIKE '%' || :keyword || '%' "
            "     OR memory_key = :memory_key) "
            "LIMIT :limit",
            {
                "user_id": user_id,
                "keyword": keyword,
                "memory_key": keyword,
                "limit": limit,
            },
        )

    async def get_active_embeddings(
        self,
        user_id: str,
        query_time: str,
    ) -> list[dict[str, Any]]:
        """查询用户所有含 embedding 的 active 记忆。

        对应文档第 15.1 节「查询候选」。
        """
        return await self._db.fetchall(
            "SELECT id, memory_type, memory_key, content, "
            "importance, confidence, embedding, embedding_dim "
            "FROM memories "
            "WHERE user_id = :user_id "
            "AND status = 'active' "
            "AND embedding IS NOT NULL "
            "AND (valid_from IS NULL OR valid_from <= :query_time) "
            "AND (valid_until IS NULL OR valid_until > :query_time)",
            {"user_id": user_id, "query_time": query_time},
        )

    async def get_source_events(
        self,
        memory_id: str,
    ) -> list[dict[str, Any]]:
        """查询记忆的来源事件列表。

        对应文档第 22.3 节。
        """
        return await self._db.fetchall(
            "SELECT m.id AS memory_id, m.content AS memory_content, "
            "e.id AS event_id, e.role, e.event_type, "
            "e.content AS event_content, e.created_at "
            "FROM memories m "
            "JOIN json_each(m.source_event_ids_json) source "
            "JOIN events e ON e.id = source.value "
            "WHERE m.id = :memory_id "
            "ORDER BY e.seq_no",
            {"memory_id": memory_id},
        )

    async def get_history(
        self,
        memory_id: str,
    ) -> list[dict[str, Any]]:
        """查询记忆的替代链历史。

        对应文档第 22.5 节。
        """
        return await self._db.fetchall(
            "WITH RECURSIVE memory_history AS ("
            "    SELECT id, supersedes_id, memory_key, content, "
            "           status, valid_from, valid_until, created_at "
            "    FROM memories WHERE id = :memory_id "
            "    UNION ALL "
            "    SELECT old.id, old.supersedes_id, old.memory_key, "
            "           old.content, old.status, old.valid_from, "
            "           old.valid_until, old.created_at "
            "    FROM memories old "
            "    JOIN memory_history current "
            "      ON current.supersedes_id = old.id "
            ") SELECT * FROM memory_history ORDER BY created_at DESC",
            {"memory_id": memory_id},
        )

    # ── 更新 ────────────────────────────────────────────────────────

    async def update_status(
        self,
        memory_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """更新记忆状态。

        FTS5 索引由触发器自动同步。
        """
        if not updates:
            return await self.get_by_id(memory_id)

        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        params = {**updates, "id": memory_id}

        sql = f"""
            UPDATE memories
            SET {set_clause}
            WHERE id = :id
            RETURNING *
        """
        return await self._db.fetchone(sql, params)

    async def increment_access_count(
        self,
        memory_id: str,
        query_time: str,
    ) -> None:
        """递增记忆的访问计数。"""
        await self._db.execute(
            "UPDATE memories "
            "SET access_count = access_count + 1, "
            "last_accessed_at = :query_time "
            "WHERE id = :id",
            {"id": memory_id, "query_time": query_time},
        )

    async def rebuild_fts(self) -> None:
        """重建 FTS5 索引。"""
        await self._db.executescript(
            "INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');"
        )

    async def optimize_fts(self) -> None:
        """优化 FTS5 索引。"""
        await self._db.executescript(
            "INSERT INTO memories_fts(memories_fts) VALUES ('optimize');"
        )

    # ── 删除 ────────────────────────────────────────────────────────

    async def hard_delete(self, memory_id: str) -> bool:
        """物理删除记忆行。

        触发 FTS5 的 DELETE 触发器。
        """
        await self._db.execute(
            "DELETE FROM memories WHERE id = :id",
            {"id": memory_id},
        )
        return await self._db.changes() > 0
