"""SignalWriter — memory_signals 写入与 reinforcement 聚合。

PLAN-13 P13-04：强化信号幂等写入，不直接修改 reinforcement 计数。
reinforcement 由 aggregate_reinforcement() 从 signals 聚合，供
memory.recompute_weight Task 和纯权重函数使用。
"""

from __future__ import annotations

import logging
import sqlite3

from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_store import EventStore
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
            self._emit_signal_event(
                signal_type,
                memory_id,
                signal_value,
                actor_principal_id,
                turn_id,
                task_id,
                idempotency_key,
            )
        return ok

    def _emit_signal_event(
        self,
        signal_type: str,
        memory_id: str,
        signal_value: int,
        actor_principal_id: str,
        turn_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> None:
        """在信号行所在事务内追加受限的规范事件。"""
        store = EventStore(self._conn)
        stream = store.read_stream("memory", memory_id)
        source = stream[-1] if stream else None
        context = source.context if source else EventContext()
        store.append(
            Event(
                event_type="memory.signal.recorded",
                stream_type="memory",
                stream_id=memory_id,
                producer="memory-signal-writer",
                event_class=EventClass.DOMAIN,
                context=EventContext(
                    trace_id=context.trace_id,
                    correlation_id=context.correlation_id,
                    causation_id=source.event_id if source else context.causation_id,
                    actor_id=actor_principal_id,
                    principal_id=actor_principal_id or context.principal_id,
                    turn_id=turn_id,
                    task_id=task_id,
                ),
                summary="Memory signal recorded",
                attributes={"signal_type": signal_type, "signal_value": signal_value},
                outcome="recorded",
                idempotency_key=(f"memory-signal:{idempotency_key}" if idempotency_key else ""),
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
