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
        """幂等写入一条信号（PLAN-14 R-08: 同时发 MemorySignalRecorded 到 Outbox）。"""
        # OPS-04 完整：记录信号指标（无论写入成功与否均计数类型）
        from cogito.infrastructure.metrics_access import _metrics

        _metrics().record_signal(signal_type)
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
        ok = self._repo.insert(signal)
        if ok:
            self._emit_signal_event(signal_type, memory_id, signal_value, task_id)
        return ok

    def _emit_signal_event(
        self, signal_type: str, memory_id: str, signal_value: int, task_id: str
    ) -> None:
        """PLAN-14/16 R-08: MemorySignalRecorded 领域事件（PLAN-16 M2 TX-02/TX-03 + 完整版本单调）。

        与信号行共享同一连接 / 事务：写入失败向上传播，确保信号与
        Outbox 事件原子提交。aggregate_version 由 OutboxRepository 同事务 MAX+1
        取得，保证严格单调。
        """
        from cogito.domain.events import DomainEvent
        from cogito.store.repositories import OutboxRepository

        payload = {"signal_type": signal_type, "signal_value": signal_value, "task_id": task_id}
        version = OutboxRepository(self._conn).next_aggregate_version("memory", memory_id)
        OutboxRepository(self._conn).insert(
            DomainEvent(
                event_type="MemorySignalRecorded",
                aggregate_type="memory",
                aggregate_id=memory_id,
                aggregate_version=version,
                payload=payload,
                payload_ref=__import__("json").dumps(payload, ensure_ascii=False),
                origin="signal_writer",
            )
        )

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
                "UPDATE memory_items SET reinforcement=? WHERE memory_id=? AND deleted_at IS NULL",
                (value, memory_id),
            )
        except sqlite3.OperationalError:
            pass
        return value
