"""Tool Call Repository — tool_calls 表持久化。

利用已存在的 tool_calls 数据库表（见 schema.py）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal


@dataclass
class ToolCallRecord:
    """tool_calls 表的值对象。"""
    tool_call_id: str
    attempt_id: str
    attempt_type: str = "run"
    tool_name: str = ""
    tool_version: str = "1.0"
    arguments: str = "{}"
    idempotency_key: str = ""
    status: Literal["pending", "approved", "executing", "succeeded", "failed", "unknown", "cancelled"] = "pending"
    started_at: int | None = None
    completed_at: int | None = None


class ToolCallRepository:
    """ToolCall 持久化。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: ToolCallRecord) -> None:
        """插入一条 ToolCall 记录。"""
        self._conn.execute(
            "INSERT INTO tool_calls "
            "(tool_call_id, attempt_id, attempt_type, tool_name, tool_version, "
            "arguments, idempotency_key, status, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.tool_call_id, record.attempt_id, record.attempt_type,
             record.tool_name, record.tool_version,
             record.arguments, record.idempotency_key,
             record.status, record.started_at, record.completed_at),
        )

    def update_status(
        self,
        tool_call_id: str,
        status: str,
        completed_at: int | None = None,
    ) -> None:
        """更新 ToolCall 状态。"""
        if completed_at is not None:
            self._conn.execute(
                "UPDATE tool_calls SET status=?, completed_at=? WHERE tool_call_id=?",
                (status, completed_at, tool_call_id),
            )
        else:
            self._conn.execute(
                "UPDATE tool_calls SET status=? WHERE tool_call_id=?",
                (status, tool_call_id),
            )

    def find_by_attempt(self, attempt_id: str) -> list[ToolCallRecord]:
        """查询某次 Attempt 的所有 ToolCall。"""
        rows = self._conn.execute(
            "SELECT * FROM tool_calls WHERE attempt_id=? ORDER BY started_at ASC",
            (attempt_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find(self, tool_call_id: str) -> ToolCallRecord | None:
        """按 ID 查询 ToolCall。"""
        row = self._conn.execute(
            "SELECT * FROM tool_calls WHERE tool_call_id=?",
            (tool_call_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def _row_to_record(self, row: sqlite3.Row) -> ToolCallRecord:
        return ToolCallRecord(
            tool_call_id=row["tool_call_id"],
            attempt_id=row["attempt_id"],
            attempt_type=row.get("attempt_type", "run"),
            tool_name=row.get("tool_name", ""),
            tool_version=row.get("tool_version", "1.0"),
            arguments=row.get("arguments", "{}"),
            idempotency_key=row.get("idempotency_key", ""),
            status=row["status"],
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
        )
