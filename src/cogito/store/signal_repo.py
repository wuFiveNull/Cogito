"""SignalRepository — memory_signals 追加式事件存储。

PLAN-13 P13-04：强化/展示/反馈使用幂等追加事件表。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class MemorySignal:
    """单条强化/展示/反馈信号。"""

    signal_id: str
    memory_id: str
    signal_type: str
    signal_value: int = 0
    actor_principal_id: str = ""
    turn_id: str = ""
    task_id: str = ""
    idempotency_key: str = ""
    algorithm_version: str = ""
    occurred_at: str = ""
    metadata_json: str = "{}"


SIGNAL_TYPES = frozenset(
    {
        "exposed",
        "referenced",
        "user_affirmed",
        "task_succeeded",
        "user_corrected",
        "negative_feedback",
    }
)


class SignalRepository:
    """memory_signals 数据访问层。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _ensure_table(self) -> bool:
        try:
            self._conn.execute("SELECT 1 FROM memory_signals LIMIT 1").fetchone()
            return True
        except sqlite3.OperationalError:
            return False

    def insert(self, signal: MemorySignal) -> bool:
        """幂等插入一条信号。

        - 有 idempotency_key → 基于唯一约束去重
        - 无键 → 直接写入（每次都是独立事件）
        """
        if not self._ensure_table():
            return False
        if signal.signal_type not in SIGNAL_TYPES:
            return False
        if not signal.signal_id:
            signal.signal_id = uuid.uuid4().hex
        if not signal.occurred_at:
            signal.occurred_at = datetime.now(UTC).isoformat()
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO memory_signals ("
                "  signal_id, memory_id, signal_type, signal_value, "
                "  actor_principal_id, turn_id, task_id, idempotency_key, "
                "  algorithm_version, occurred_at, metadata_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    signal.signal_id,
                    signal.memory_id,
                    signal.signal_type,
                    signal.signal_value,
                    signal.actor_principal_id,
                    signal.turn_id,
                    signal.task_id,
                    signal.idempotency_key,
                    signal.algorithm_version,
                    signal.occurred_at,
                    signal.metadata_json,
                ),
            )
            return self._conn.total_changes > 0
        except sqlite3.OperationalError:
            return False

    def list_for_memory(self, memory_id: str) -> list[MemorySignal]:
        """获取记忆的所有信号。"""
        if not self._ensure_table():
            return []
        try:
            rows = self._conn.execute(
                "SELECT * FROM memory_signals WHERE memory_id=? ORDER BY occurred_at ASC",
                (memory_id,),
            ).fetchall()
            return [_row_to_signal(dict(r)) for r in rows]
        except sqlite3.OperationalError:
            return []

    def aggregate_reinforcement(self, memory_id: str) -> int:
        """聚合计算 reinforcement 值（PLAN-13 权重纯函数输入）。

        仅 user_affirmed (+2)、task_succeeded (+1)、user_corrected (+2) 贡献。
        negative_feedback 不产生负溢出。
        """
        signals = self.list_for_memory(memory_id)
        value = 0
        for s in signals:
            if s.signal_type == "user_affirmed":
                value += 2 * max(s.signal_value, 1)
            elif s.signal_type == "task_succeeded":
                value += 1 * max(s.signal_value, 1)
            elif s.signal_type == "user_corrected":
                value += 2 * max(s.signal_value, 1)
        return value

    def count_by_type(self, memory_id: str) -> dict[str, int]:
        """统计各类型信号数。"""
        signals = self.list_for_memory(memory_id)
        counts: dict[str, int] = {}
        for s in signals:
            counts[s.signal_type] = counts.get(s.signal_type, 0) + 1
        return counts


def _row_to_signal(d: dict[str, Any]) -> MemorySignal:
    return MemorySignal(
        signal_id=d.get("signal_id", ""),
        memory_id=d.get("memory_id", ""),
        signal_type=d.get("signal_type", ""),
        signal_value=int(d.get("signal_value", 0)),
        actor_principal_id=d.get("actor_principal_id", ""),
        turn_id=d.get("turn_id", ""),
        task_id=d.get("task_id", ""),
        idempotency_key=d.get("idempotency_key", ""),
        algorithm_version=d.get("algorithm_version", ""),
        occurred_at=d.get("occurred_at", ""),
        metadata_json=d.get("metadata_json", "{}"),
    )
