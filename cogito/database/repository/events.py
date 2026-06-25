"""
cogito.database.repository.events — EventRepository

对应设计文档第 9、10 节：events 表的单表 CRUD 操作。
"""

from __future__ import annotations

from typing import Any

from cogito.database.connection import AsyncDatabase
from cogito.database.ids import new_uuid


class EventRepository:
    """events 表的数据访问对象。"""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── 创建 ────────────────────────────────────────────────────────

    async def insert(self, params: dict[str, Any]) -> dict[str, Any]:
        """插入一条 event 记录。

        如果未提供 id，自动生成 UUIDv7。
        返回插入后的完整行。
        """
        if "id" not in params or not params["id"]:
            params["id"] = new_uuid()

        columns = ", ".join(params.keys())
        placeholders = ", ".join(f":{k}" for k in params.keys())

        sql = f"""
            INSERT INTO events ({columns})
            VALUES ({placeholders})
            RETURNING *
        """
        row = await self._db.fetchone(sql, params)
        assert row is not None
        return row

    # ── 查询 ────────────────────────────────────────────────────────

    async def get_by_id(self, event_id: str) -> dict[str, Any] | None:
        """根据 event id 查询。"""
        return await self._db.fetchone(
            "SELECT * FROM events WHERE id = :id",
            {"id": event_id},
        )

    async def get_session_events(
        self,
        user_id: str,
        session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询某个 session 的事件序列（按 seq_no 升序）。"""
        return await self._db.fetchall(
            "SELECT * FROM events "
            "WHERE user_id = :user_id AND session_id = :session_id "
            "ORDER BY seq_no ASC LIMIT :limit",
            {"user_id": user_id, "session_id": session_id, "limit": limit},
        )

    async def get_last_seq_no(
        self,
        user_id: str,
        session_id: str,
    ) -> int:
        """获取某个 session 的最新 seq_no，无事件时返回 0。"""
        row = await self._db.fetchone(
            "SELECT COALESCE(MAX(seq_no), 0) AS max_seq "
            "FROM events "
            "WHERE user_id = :user_id AND session_id = :session_id",
            {"user_id": user_id, "session_id": session_id},
        )
        return row["max_seq"] if row else 0

    async def get_by_extraction_group(
        self,
        group_id: str,
    ) -> list[dict[str, Any]]:
        """查询某个提取组的所有事件。"""
        return await self._db.fetchall(
            "SELECT id, seq_no, role, event_type, content, content_json, created_at "
            "FROM events "
            "WHERE extraction_group_id = :group_id "
            "ORDER BY seq_no",
            {"group_id": group_id},
        )

    async def get_context_events(
        self,
        user_id: str,
        session_id: str,
        before_seq: int,
        context_count: int = 4,
    ) -> list[dict[str, Any]]:
        """查询提取时使用的重叠上下文事件。"""
        return await self._db.fetchall(
            "SELECT id, seq_no, role, event_type, content "
            "FROM events "
            "WHERE user_id = :user_id "
            "AND session_id = :session_id "
            "AND seq_no < :before_seq "
            "ORDER BY seq_no DESC LIMIT :limit",
            {
                "user_id": user_id,
                "session_id": session_id,
                "before_seq": before_seq,
                "limit": context_count,
            },
        )

    async def get_pending_extraction_summary(
        self,
    ) -> list[dict[str, Any]]:
        """查询所有待提取的会话摘要。

        对应文档第 22.6 节。
        """
        return await self._db.fetchall(
            "SELECT user_id, session_id, "
            "COUNT(*) AS pending_event_count, "
            "MIN(seq_no) AS first_pending_seq, "
            "MAX(seq_no) AS last_pending_seq, "
            "MIN(created_at) AS first_pending_at, "
            "MAX(created_at) AS last_pending_at "
            "FROM events "
            "WHERE extraction_status = 'pending' "
            "GROUP BY user_id, session_id "
            "ORDER BY last_pending_at",
        )

    async def get_failed_extraction_groups(
        self,
    ) -> list[dict[str, Any]]:
        """查询所有失败的提取组。

        对应文档第 22.7 节。
        """
        return await self._db.fetchall(
            "SELECT extraction_group_id, user_id, session_id, "
            "COUNT(*) AS event_count, "
            "MAX(extraction_attempts) AS attempts, "
            "MAX(extraction_error) AS last_error "
            "FROM events "
            "WHERE extraction_status = 'failed' "
            "GROUP BY extraction_group_id, user_id, session_id "
            "ORDER BY MAX(updated_at) DESC",
        )

    # ── 更新 ────────────────────────────────────────────────────────

    async def claim_extraction(
        self,
        user_id: str,
        session_id: str,
        start_seq: int,
        end_seq: int,
        group_id: str,
    ) -> int:
        """并发安全地领取一个提取组。

        对应文档第 10.3 节「SQLite 中的并发领取方式」。

        Returns:
            实际领取的事件数（0 表示已被其他 Worker 领取）
        """
        await self._db.execute(
            "UPDATE events "
            "SET extraction_status = 'processing', "
            "    extraction_group_id = :group_id, "
            "    extraction_attempts = extraction_attempts + 1, "
            "    extraction_error = NULL "
            "WHERE user_id = :user_id "
            "AND session_id = :session_id "
            "AND extraction_status = 'pending' "
            "AND seq_no BETWEEN :start_seq AND :end_seq",
            {
                "user_id": user_id,
                "session_id": session_id,
                "start_seq": start_seq,
                "end_seq": end_seq,
                "group_id": group_id,
            },
        )
        return await self._db.changes()

    async def complete_extraction(
        self,
        group_id: str,
    ) -> int:
        """标记提取组为完成。

        对应文档第 13.1 节。
        """
        await self._db.execute(
            "UPDATE events "
            "SET extraction_status = 'done', "
            "    extraction_error = NULL, "
            "    extracted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE extraction_group_id = :group_id "
            "AND extraction_status = 'processing'",
            {"group_id": group_id},
        )
        return await self._db.changes()

    async def fail_extraction(
        self,
        group_id: str,
        error_message: str | None = None,
    ) -> int:
        """标记提取组为失败。

        对应文档第 13.2 节。
        """
        params: dict[str, Any] = {
            "group_id": group_id,
            "error_message": error_message,
        }
        sql = (
            "UPDATE events "
            "SET extraction_status = 'failed', "
            "    extraction_error = :error_message "
            "WHERE extraction_group_id = :group_id "
            "AND extraction_status = 'processing'"
        )
        await self._db.execute(sql, params)
        return await self._db.changes()

    async def retry_failed_extraction(
        self,
        group_id: str,
        max_attempts: int = 3,
    ) -> int:
        """重试失败的提取组。

        对应文档第 13.2 节「重试」。
        """
        await self._db.execute(
            "UPDATE events "
            "SET extraction_status = 'processing', "
            "    extraction_attempts = extraction_attempts + 1, "
            "    extraction_error = NULL "
            "WHERE extraction_group_id = :group_id "
            "AND extraction_status = 'failed' "
            "AND extraction_attempts < :max_attempts",
            {"group_id": group_id, "max_attempts": max_attempts},
        )
        return await self._db.changes()
