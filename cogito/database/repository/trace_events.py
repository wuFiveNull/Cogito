"""
cogito.database.repository.trace_events — TraceEventRepository

对应设计文档第 8 节：trace_events 表的单表 CRUD 操作。
"""

from __future__ import annotations

from typing import Any

from cogito.database.connection import AsyncDatabase
from cogito.database.ids import new_uuid


class TraceEventRepository:
    """trace_events 表的数据访问对象。"""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── 创建 ────────────────────────────────────────────────────────

    async def insert(self, params: dict[str, Any]) -> dict[str, Any]:
        """插入一条 span 记录。

        如果未提供 id，自动生成 UUIDv7。
        返回插入后的完整行。
        """
        if "id" not in params or not params["id"]:
            params["id"] = new_uuid()

        columns = ", ".join(params.keys())
        placeholders = ", ".join(f":{k}" for k in params.keys())

        sql = f"""
            INSERT INTO trace_events ({columns})
            VALUES ({placeholders})
            RETURNING *
        """
        row = await self._db.fetchone(sql, params)
        assert row is not None
        return row

    # ── 查询 ────────────────────────────────────────────────────────

    async def get_by_id(self, span_id: str) -> dict[str, Any] | None:
        """根据 span id 查询。"""
        return await self._db.fetchone(
            "SELECT * FROM trace_events WHERE id = :id",
            {"id": span_id},
        )

    async def get_by_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """查询某个 trace 的所有 span，按时间排序。"""
        return await self._db.fetchall(
            "SELECT * FROM trace_events WHERE trace_id = :trace_id "
            "ORDER BY started_at, created_at",
            {"trace_id": trace_id},
        )

    async def get_children(self, parent_span_id: str) -> list[dict[str, Any]]:
        """查询某个 span 的直接子 span。"""
        return await self._db.fetchall(
            "SELECT * FROM trace_events WHERE parent_span_id = :parent_span_id "
            "ORDER BY started_at",
            {"parent_span_id": parent_span_id},
        )

    async def get_tools_in_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """查询 trace 中所有工具调用。"""
        return await self._db.fetchall(
            "SELECT id, parent_span_id, tool_name, tool_call_id, "
            "attempt_no, status, latency_ms, error_code, "
            "decision_reason, started_at, ended_at "
            "FROM trace_events "
            "WHERE trace_id = :trace_id AND step_type = 'tool_call' "
            "ORDER BY started_at",
            {"trace_id": trace_id},
        )

    async def get_by_tool_call(
        self,
        tool_call_id: str,
    ) -> list[dict[str, Any]]:
        """查询某个工具调用的所有尝试。"""
        return await self._db.fetchall(
            "SELECT id, tool_name, tool_call_id, attempt_no, status, "
            "latency_ms, error_code, started_at "
            "FROM trace_events "
            "WHERE tool_call_id = :tool_call_id "
            "ORDER BY attempt_no",
            {"tool_call_id": tool_call_id},
        )

    async def get_recent_by_user(
        self,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """查询用户最近的 span。"""
        return await self._db.fetchall(
            "SELECT * FROM trace_events "
            "WHERE user_id = :user_id "
            "ORDER BY started_at DESC "
            "LIMIT :limit",
            {"user_id": user_id, "limit": limit},
        )

    async def get_response_span(
        self,
        trace_id: str,
    ) -> dict[str, Any] | None:
        """查询 trace 中最终回答的 span。"""
        return await self._db.fetchone(
            "SELECT id, input_event_ids_json, input_memory_ids_json, "
            "output_event_ids_json, model_name, prompt_version, metadata_json "
            "FROM trace_events "
            "WHERE trace_id = :trace_id AND step_type = 'response' "
            "ORDER BY started_at DESC LIMIT 1",
            {"trace_id": trace_id},
        )

    # ── 更新 ────────────────────────────────────────────────────────

    async def update_status(
        self,
        span_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """更新 span 状态（完成/失败等）。

        Args:
            span_id: 要更新的 span id
            updates: 需要更新的字段字典

        Returns:
            更新后的行，或 None（未找到或 status 不匹配）
        """
        if not updates:
            return await self.get_by_id(span_id)

        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        params = {**updates, "id": span_id}

        sql = f"""
            UPDATE trace_events
            SET {set_clause}
            WHERE id = :id
            RETURNING *
        """
        return await self._db.fetchone(sql, params)

    async def delete_old_traces(
        self,
        before: str,
        limit: int = 1000,
    ) -> int:
        """删除指定时间之前的 trace（仅 trace_events 记录）。

        使用子查询 + IN 而非 DELETE ... ORDER BY LIMIT 以确保兼容性。

        Args:
            before: ISO 8601 时间字符串，删除此时间之前的记录
            limit: 最大删除数量

        Returns:
            删除的行数
        """
        await self._db.execute(
            "DELETE FROM trace_events WHERE rowid IN ("
            "SELECT rowid FROM trace_events WHERE created_at < :before LIMIT :limit"
            ")",
            {"before": before, "limit": limit},
        )
        return await self._db.changes()
