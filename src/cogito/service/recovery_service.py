"""RecoveryService — 启动恢复扫描和定期清理。

处理以下场景（EXECUTION-LIFECYCLE / 5.3 进程重启）：
1. 无有效执行权的 running Turn/RunAttempt
2. Event-first 流式 Delivery 的中断恢复

投递副作用的待办与未知状态由 Event 流恢复；本服务不再读取或修改
旧 ``deliveries`` 行。其余恢复操作必须使用条件更新，重新验证
lease_version + lease_expires_at，防止与 heartbeat 产生竞态。

recover_stale_turns 原子保证：
1. SELECT 保存 attempt 的 lease_version 和原 lease_expires_at
2. Attempt UPDATE 条件验证 lease_version + lease_expires_at 未变化
3. 只有 Attempt 更新成功后才更新 Turn
4. Turn UPDATE 同时验证 active_attempt_id
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from cogito.contracts.clock import Clock, ProductionClock, epoch_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.turn import RunAttemptStatus, TurnStatus
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.event_replay import replay_delivery, replay_run_attempt, replay_turn
from cogito.store.event_store import EventStore, StreamVersionConflictError


class RecoveryService:
    """启动恢复扫描和定期 Lease 清理。"""

    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None) -> None:
        self._conn = conn
        self._clock = clock or ProductionClock()

    def _now(self, override: datetime | None = None) -> datetime:
        return override if override is not None else self._clock.now()

    def recover_stale_turns(self, clock: datetime | None = None) -> int:
        """标记无有效执行权的 running Turn/RunAttempt 为 abandoned。

        原子操作：
        1. SELECT 出 lease_version 和 lease_expires_at 用于条件更新
        2. Attempt UPDATE 验证 lease_version + lease_expires_at 未变化 + 仍过期
        3. 只有 Attempt rowcount > 0 才更新 Turn
        4. Turn UPDATE 验证 version + active_attempt_id

        Worker 在 SELECT 和 UPDATE 之间 heartbeat 成功时，
        Attempt UPDATE 的 lease_version/lease_expires_at 条件不匹配，行数=0，
        Turn 不会被修改。
        """
        now_ms_val = epoch_ms(self._now(clock))
        return self._recover_event_stale_turns(now_ms_val)

    def recover_stale_tasks(self, clock: datetime | None = None) -> int:
        """标记无有效执行权的 running Task/TaskAttempt 为 abandoned+queued。

        与 recover_stale_turns 同款条件更新模式：
        1. SELECT 出 attempt 的 lease_version + lease_expires_at
        2. Attempt UPDATE 验证 lease_version + lease_expires_at 仍过期
        3. 仅 Attempt rowcount > 0 才将 Task 回 queued
        """
        now_ms_val = epoch_ms(self._now(clock))
        return self._recover_event_stale_tasks(now_ms_val)

    def recover_streaming_deliveries(self, clock: datetime | None = None) -> int:
        """撤回崩溃遗留的流式 Delivery（status='streaming' 且 Turn 已不再 running）。

        流式 Delivery 在 AgentRunner.run_once 内由 Turn 的 RunAttempt lease 拥有。
        若进程在流式过程中崩溃，delivery 永久卡在 'streaming'（平台已创建占位气泡），
        而其 Turn/RunAttempt 将由 recover_stale_turns 标记为 abandoned / queued。

        孤儿判定（与 recover_stale_turns 顺序配合，recover_all 先跑 stale_turns）：
        - Turn 不存在 → 孤儿
        - Turn 已不在 running（queued/completed/abandoned 等）→ 孤儿
        - Turn 名义 running 但其 active attempt 已不 running（崩溃但 lease 未过期的
          中间态）→ 孤儿，并把 Turn 重置为 queued 以便重新尝试

        注意：run_once 创建流式 delivery 前会把 Turn 置为 running，故一个合法的
        流式 delivery 必然对应 running 的 Turn。重放时旧 delivery 已被本函数撤回，
        不会出现 "Turn queued 但仍有合法 streaming delivery" 的误杀。

        不变量：本函数只写 status='streaming' 的行（条件 UPDATE），与运行中的
        AgentRunner 不冲突——后者要么已把 delivery 推进到 sent/interrupted，要么
        其 Turn 仍 running，不会被选中。
        """
        return self._recover_event_streaming_deliveries(clock)

    def _recover_event_streaming_deliveries(self, clock: datetime | None = None) -> int:
        """Recover Event-first streams; legacy rows are handled separately."""
        events = EventStore(self._conn)
        grouped: dict[str, list[Event]] = {}
        for event in events.read_stream_type("delivery"):
            grouped.setdefault(event.stream_id, []).append(event)

        count = 0
        with UnitOfWork(self._conn) as uow:
            for delivery_id, stream in grouped.items():
                state = replay_delivery(stream, delivery_id)
                if (
                    state is None
                    or state.delivery_mode != "streaming"
                    or state.status not in {"streaming", "sending"}
                ):
                    continue
                reset_turn = self._streaming_turn_is_orphan(state.turn_id)
                if reset_turn is None:
                    continue
                events.append(
                    Event(
                        event_type="delivery.cancelled",
                        stream_type="delivery",
                        stream_id=delivery_id,
                        event_class=EventClass.DOMAIN,
                        producer="streaming-recovery",
                        context=EventContext(
                            conversation_id=state.conversation_id,
                            session_id=state.session_id,
                            turn_id=state.turn_id,
                            attempt_id=state.attempt_id,
                        ),
                        summary="Streaming delivery cancelled during startup recovery",
                        outcome="cancelled",
                        error_category="startup_recovery",
                        occurred_at=epoch_ms(self._now(clock)),
                        idempotency_key=f"streaming-recovery:{delivery_id}:cancelled",
                    )
                )
                count += 1
                if reset_turn:
                    self._reset_orphaned_turn(state.turn_id)
            if count:
                uow.commit()
        return count

    def _recover_event_stale_turns(self, now_ms_val: int) -> int:
        """Abandon an expired RunAttempt and requeue its Turn using Events only."""
        events = EventStore(self._conn)
        grouped: dict[str, list[Event]] = {}
        for event in events.read_stream_type("turn"):
            grouped.setdefault(event.stream_id, []).append(event)

        recovered = 0
        with UnitOfWork(self._conn) as uow:
            for turn_id, turn_stream in grouped.items():
                turn = replay_turn(turn_stream, turn_id)
                if turn is None or turn.status != "running" or not turn.active_attempt_id:
                    continue
                attempt_stream = events.read_stream("run_attempt", turn.active_attempt_id)
                attempt = replay_run_attempt(attempt_stream, turn.active_attempt_id)
                if (
                    attempt is None
                    or attempt.status != "running"
                    or attempt.lease_expires_at is None
                    or attempt.lease_expires_at > now_ms_val
                ):
                    continue

                source = attempt_stream[-1].context if attempt_stream else EventContext()
                abandoned = Event(
                    event_type="runtime.attempt.abandoned",
                    stream_type="run_attempt",
                    stream_id=attempt.attempt_id,
                    producer="startup-recovery",
                    event_class=EventClass.OPERATION,
                    context=EventContext(
                        trace_id=source.trace_id,
                        span_id=source.span_id,
                        parent_span_id=source.parent_span_id,
                        correlation_id=source.correlation_id,
                        causation_id=attempt_stream[-1].event_id if attempt_stream else source.causation_id,
                        actor_id=source.actor_id,
                        principal_id=source.principal_id,
                        conversation_id=source.conversation_id,
                        session_id=source.session_id,
                        turn_id=turn_id,
                        attempt_id=attempt.attempt_id,
                    ),
                    summary="Expired run attempt abandoned during startup recovery",
                    attributes={"lease_version": attempt.lease_version},
                    outcome="abandoned",
                    occurred_at=now_ms_val,
                    idempotency_key=f"startup-recovery:{attempt.attempt_id}:abandoned",
                )
                requeued = Event(
                    event_type="runtime.turn.queued",
                    stream_type="turn",
                    stream_id=turn_id,
                    producer="startup-recovery",
                    event_class=EventClass.DOMAIN,
                    context=EventContext(
                        trace_id=source.trace_id,
                        correlation_id=source.correlation_id,
                        causation_id=abandoned.event_id,
                        actor_id=source.actor_id,
                        principal_id=source.principal_id,
                        conversation_id=source.conversation_id,
                        session_id=turn.session_id or source.session_id,
                        turn_id=turn_id,
                    ),
                    summary="Expired Turn requeued during startup recovery",
                    outcome="queued",
                    occurred_at=now_ms_val,
                    idempotency_key=f"startup-recovery:{turn_id}:queued",
                )
                try:
                    events.append_many(
                        (abandoned, requeued),
                        expected_versions={
                            ("run_attempt", attempt.attempt_id): attempt.stream_version,
                            ("turn", turn_id): turn.stream_version,
                        },
                    )
                except StreamVersionConflictError:
                    continue
                recovered += 1
            if recovered:
                uow.commit()
        return recovered

    def _recover_event_stale_tasks(self, now_ms_val: int) -> int:
        """Abandon expired TaskAttempts and requeue their Task by Event only."""
        from cogito.store.task_repo import TaskAttemptRepository, TaskRepository

        task_repo = TaskRepository(self._conn)
        attempt_repo = TaskAttemptRepository(self._conn)
        recovered = 0
        with UnitOfWork(self._conn) as uow:
            for task in task_repo.list_filtered(status="running", limit=50_000):
                if task.lease_expires_at is None or epoch_ms(task.lease_expires_at) > now_ms_val:
                    continue
                attempts = attempt_repo.list_for_task(task.task_id)
                active = next(
                    (
                        attempt
                        for attempt in reversed(attempts)
                        if attempt.status.value == "running"
                        and attempt.lease_version == task.lease_version
                    ),
                    None,
                )
                if active is None:
                    continue
                if not attempt_repo.abandon(active.task_attempt_id, finished_at=now_ms_val):
                    continue
                if task_repo.recover_expired_lease(
                    task.task_id,
                    task.lease_owner or "",
                    task.lease_version,
                    now_ms_val,
                ):
                    recovered += 1
            if recovered:
                uow.commit()
        return recovered

    def _streaming_turn_is_orphan(self, turn_id: str) -> bool | None:
        """Return ``None`` for a live turn, else whether it must be reset."""
        if not turn_id:
            return False
        events = EventStore(self._conn)
        turn = replay_turn(events.read_stream("turn", turn_id), turn_id)
        if turn is None:
            return False
        if turn.status != "running":
            return False
        attempt = replay_run_attempt(
            events.read_stream("run_attempt", turn.active_attempt_id), turn.active_attempt_id
        )
        if attempt is not None and attempt.status == "running":
            return None
        return True

    def _reset_orphaned_turn(self, turn_id: str) -> None:
        events = EventStore(self._conn)
        turn = replay_turn(events.read_stream("turn", turn_id), turn_id)
        if turn is None or turn.status != "running":
            return
        events.append(
            Event(
                event_type="runtime.turn.queued",
                stream_type="turn",
                stream_id=turn_id,
                producer="streaming-recovery",
                event_class=EventClass.DOMAIN,
                context=EventContext(session_id=turn.session_id, turn_id=turn_id),
                summary="Orphaned streaming Turn requeued during recovery",
                outcome="queued",
                idempotency_key=f"streaming-recovery:{turn_id}:requeued",
            ),
            expected_version=turn.stream_version,
        )

    def recover_stale_ingestion_batches(self, clock: datetime | None = None) -> int:
        """收尾崩溃遗留的 MCP ingestion batch。

        合法的 started batch 必须仍有 running Task。启动恢复发生在 Worker 开始
        领取新任务之前，因此没有 running Task 的 started 行可安全标记为 failed。
        """
        now_ms_val = epoch_ms(self._now(clock))
        with UnitOfWork(self._conn) as uow:
            updated = self._conn.execute(
                "UPDATE ingestion_batches SET status='failed', "
                "error_ref=CASE WHEN error_ref='' THEN 'startup_recovery' ELSE error_ref END, "
                "completed_at=? WHERE status='started' AND (task_id IS NULL OR task_id='' "
                "OR NOT EXISTS (SELECT 1 FROM tasks t WHERE t.task_id=ingestion_batches.task_id "
                "AND t.status='running'))",
                (now_ms_val,),
            )
            if updated.rowcount:
                uow.commit()
            return int(updated.rowcount)

    def recover_all(self, clock: datetime | None = None) -> dict[str, int]:
        return {
            "stale_turns": self.recover_stale_turns(clock),
            "stale_tasks": self.recover_stale_tasks(clock),
            "streaming_deliveries": self.recover_streaming_deliveries(clock),
            "stale_ingestion_batches": self.recover_stale_ingestion_batches(clock),
        }
