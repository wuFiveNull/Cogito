"""SignalWriter — memory_signals 写入与 reinforcement 聚合。

PLAN-13 P13-04：强化信号幂等写入，不直接修改 reinforcement 计数。
reinforcement 由 aggregate_reinforcement() 从 signals 聚合，供
memory.recompute_weight Task 和纯权重函数使用。
"""
from __future__ import annotations

import logging
import sqlite3

from cogito.store.signal_repo import MemorySignal, SignalRepository

_LOGGER = logging.getLogger("cogito.memory_signals")


class SignalWriter:
    """强化/展示/反馈信号写入入口。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._repo = SignalRepository(conn)
        self._conn = conn

    def record_signal(
        self,
        signal_type: str,
        memory_id: str,
        signal_value: int = 0,
        actor_principal_id: str = "",
        turn_id: str = "",
        task_id: str = "",
        idempotency_key: str = "",
        algorithm_version: str = "",
        metadata_json: str = "{}",
    ) -> bool:
        """幂等写入一条信号。"""
        signal = MemorySignal(
            signal_id="",
            memory_id=memory_id,
            signal_type=signal_type,
            signal_value=signal_value,
            actor_principal_id=actor_principal_id,
            turn_id=turn_id,
            task_id=task_id,
            idempotency_key=idempotency_key,
            algorithm_version=algorithm_version,
            metadata_json=metadata_json,
        )
        return self._repo.insert(signal)

    def record_exposed(self, memory_id: str, **kwargs) -> bool:
        """召回展示信号（不增加 reinforcement）。"""
        return self.record_signal("exposed", memory_id, **kwargs)

    def record_user_affirmed(self, memory_id: str, **kwargs) -> bool:
        """用户明确确认（reinforcement +2）。"""
        return self.record_signal("user_affirmed", memory_id, **kwargs)

    def record_task_succeeded(self, memory_id: str, **kwargs) -> bool:
        """成功 Task 依赖（reinforcement +1）。"""
        return self.record_signal("task_succeeded", memory_id, **kwargs)

    def aggregate_reinforcement(self, memory_id: str) -> int:
        """聚合计算 reinforcement 值。"""
        return self._repo.aggregate_reinforcement(memory_id)

    def flush_reinforcement(self, memory_id: str) -> int:
        """将聚合 reinforcement 写回 memory_items 缓存。

        返回聚合值。
        """
        value = self.aggregate_reinforcement(memory_id)
        try:
            self._conn.execute(
                "UPDATE memory_items SET reinforcement=? "
                "WHERE memory_id=? AND deleted_at IS NULL",
                (value, memory_id),
            )
        except sqlite3.OperationalError:
            pass
        return value
