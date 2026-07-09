"""CommandAuditRepository —— commands 表数据访问（Plan 04 M4 / Plan 05 M4）。

每个 Command Envelope 执行后落盘一条记录，支持幂等键去重与状态追溯。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class CommandRecord:
    command_id: str
    actor: str
    command_type: str
    idempotency_key: str
    target_type: str | None
    target_id: str | None
    expected_version: int | None
    payload: str | None
    status: str = "pending"
    result_summary: str | None = None
    error_code: str | None = None
    created_at: int = 0
    expires_at: int | None = None
    consumed_at: int | None = None
    origin: str | None = None
    trace_id: str | None = None


class CommandAuditRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: CommandRecord) -> None:
        self._conn.execute(
            "INSERT INTO commands (command_id, actor, command_type, idempotency_key, "
            "target_type, target_id, expected_version, payload, status, "
            "created_at, expires_at, origin, trace_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.command_id, record.actor, record.command_type,
             record.idempotency_key, record.target_type, record.target_id,
             record.expected_version, record.payload, record.status,
             record.created_at, record.expires_at, record.origin, record.trace_id),
        )

    def find_by_idempotency(
        self, actor: str, command_type: str, idempotency_key: str
    ) -> CommandRecord | None:
        row = self._conn.execute(
            "SELECT * FROM commands WHERE actor=? AND command_type=? AND idempotency_key=?",
            (actor, command_type, idempotency_key),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get(self, command_id: str) -> CommandRecord | None:
        row = self._conn.execute(
            "SELECT * FROM commands WHERE command_id=?", (command_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def mark_consumed(self, command_id: str, result_summary: str | None = None) -> None:
        self._conn.execute(
            "UPDATE commands SET status='consumed', consumed_at=MAX(created_at, ?), "
            "result_summary=? WHERE command_id=? AND status='pending'",
            (_now_ms(), result_summary, command_id),
        )

    def mark_rejected(
        self, command_id: str, error_code: str,
        result_summary: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE commands SET status='rejected', error_code=?, result_summary=? "
            "WHERE command_id=? AND status='pending'",
            (error_code, result_summary, command_id),
        )

    def list_pending(self, limit: int = 100) -> list[CommandRecord]:
        rows = self._conn.execute(
            "SELECT * FROM commands WHERE status='pending' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> CommandRecord:
        return CommandRecord(
            command_id=row["command_id"],
            actor=row["actor"],
            command_type=row["command_type"],
            idempotency_key=row["idempotency_key"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            expected_version=row["expected_version"],
            payload=row["payload"],
            status=row["status"],
            result_summary=row["result_summary"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
            origin=row["origin"],
            trace_id=row["trace_id"],
        )


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)
