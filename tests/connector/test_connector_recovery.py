"""Connector Task 恢复测试 —— Lease 过期、断点续传、重启恢复。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.task import Task, TaskAttempt, TaskAttemptStatus, TaskStatus
from cogito.domain.schedule import Schedule, ScheduleType
from cogito.domain.event import Event, EventClass, EventContext
from cogito.runtime.clock import FakeClock
from cogito.service.recovery_service import RecoveryService
from cogito.service.scheduler import Scheduler
from cogito.store.event_replay import replay_delivery, replay_turn
from cogito.store.event_store import EventStore
from cogito.store.schedule_repo import ScheduleRepository
from cogito.store.task_repo import TaskAttemptRepository, TaskRepository


class TestRecoverStaleTasks:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def clock(self):
        return FakeClock(start=datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC))

    def _commit(self, conn):
        conn.commit()

    def _make_running_task(self, conn, task_id="t1", lease_expires=None, clock=None):
        now = clock.now() if clock else datetime.now(UTC)
        # Create queued task first
        TaskRepository(conn).insert(
            Task(
                task_id=task_id,
                task_type="connector.poll",
                payload_ref="c1",
                status=TaskStatus.queued,
                idempotency_key=f"t-{task_id}",
                created_at=now,
            )
        )
        conn.commit()
        # Claim via Dispatcher to create proper Event-based lease
        from cogito.service.task_dispatcher import TaskDispatcher

        claimed = TaskDispatcher(conn, clock=clock).claim_next("worker-1", clock=now)
        assert claimed is not None, f"Could not claim task {task_id}"
        return claimed.task, claimed.attempt

    def test_recover_expired_task(self, conn, clock):
        now = clock.now()
        # 已过期的 task
        self._make_running_task(
            conn,
            "t1",
            lease_expires=now - timedelta(minutes=1),
            clock=clock,
        )

        svc = RecoveryService(conn, clock=clock)
        count = svc.recover_stale_tasks()
        assert count == 1

        # Task 回 queued
        task = TaskRepository(conn).get("t1")
        assert task.status == TaskStatus.queued
        assert task.lease_owner is None

        # Attempt abandoned
        # (task_attempts 没有 get 方法，直接查)
        row = conn.execute(
            "SELECT status FROM task_attempts WHERE task_id='t1'",
        ).fetchone()
        assert row["status"] == "abandoned"

    def test_not_recover_valid_lease(self, conn, clock):
        now = clock.now()
        # 未过期的 task
        self._make_running_task(
            conn,
            "t1",
            lease_expires=now + timedelta(minutes=5),
            clock=clock,
        )

        svc = RecoveryService(conn, clock=clock)
        count = svc.recover_stale_tasks()
        assert count == 0

        task = TaskRepository(conn).get("t1")
        assert task.status == TaskStatus.running

    def test_recover_all_includes_stale_tasks(self, conn, clock):
        now = clock.now()
        self._make_running_task(
            conn,
            "t1",
            lease_expires=now - timedelta(minutes=1),
            clock=clock,
        )
        svc = RecoveryService(conn, clock=clock)
        result = svc.recover_all()
        assert "stale_tasks" in result
        assert result["stale_tasks"] == 1


class TestRecoverStreamingDeliveries:
    """Plan 05 M5：崩溃后撤回孤儿流式 Delivery 并重置 Turn。"""

    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def clock(self):
        return FakeClock(start=datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC))

    def _make_turn(
        self,
        conn,
        clock,
        turn_id="turn-1",
        turn_status="running",
        attempt_status="running",
        lease_expires=None,
    ):
        from cogito.store.time_utils import epoch_ms

        now = epoch_ms(clock.now())
        expires = lease_expires if lease_expires is not None else (now + 5 * 60 * 1000)
        attempt_id = f"{turn_id}_a"
        context = EventContext(session_id="s1", turn_id=turn_id, attempt_id=attempt_id)
        events = EventStore(conn)
        events.append_many(
            (
                Event(
                    event_type="runtime.turn.accepted",
                    stream_type="turn",
                    stream_id=turn_id,
                    event_class=EventClass.DOMAIN,
                    producer="test-recovery",
                    context=EventContext(session_id="s1", turn_id=turn_id),
                    summary="Test Turn accepted",
                    attributes={"input_message_id": "m1", "priority": 80},
                    outcome="queued",
                    occurred_at=now,
                ),
                Event(
                    event_type="runtime.turn.started",
                    stream_type="turn",
                    stream_id=turn_id,
                    event_class=EventClass.OPERATION,
                    producer="test-recovery",
                    context=context,
                    summary="Test Turn started",
                    attributes={"active_attempt_id": attempt_id},
                    outcome=turn_status,
                    occurred_at=now,
                ),
                Event(
                    event_type="runtime.attempt.started",
                    stream_type="run_attempt",
                    stream_id=attempt_id,
                    event_class=EventClass.OPERATION,
                    producer="test-recovery",
                    context=context,
                    summary="Test run attempt started",
                    attributes={
                        "attempt_no": 1,
                        "lease_version": 1,
                        "lease_expires_at": expires,
                    },
                    outcome="running",
                    occurred_at=now,
                ),
            )
        )
        if attempt_status == "abandoned":
            events.append(
                Event(
                    event_type="runtime.attempt.abandoned",
                    stream_type="run_attempt",
                    stream_id=attempt_id,
                    event_class=EventClass.OPERATION,
                    producer="test-recovery",
                    context=context,
                    summary="Test run attempt abandoned",
                    outcome="abandoned",
                    occurred_at=now,
                )
            )
        conn.commit()

    def _make_streaming_delivery(
        self,
        conn,
        clock,
        delivery_id="d1",
        turn_id="turn-1",
        platform_message_id="pm-1",
        conversation_id="web:dbg",
    ):
        from cogito.store.time_utils import epoch_ms

        now = epoch_ms(clock.now())
        EventStore(conn).append(
            Event(
                event_type="delivery.requested",
                stream_type="delivery",
                stream_id=delivery_id,
                event_class=EventClass.DOMAIN,
                producer="test-recovery",
                context=EventContext(
                    conversation_id=conversation_id,
                    session_id="s1",
                    turn_id=turn_id,
                    attempt_id=f"{turn_id}_a",
                ),
                summary="Test streaming delivery requested",
                attributes={
                    "delivery_mode": "streaming",
                    "platform_message_id": platform_message_id,
                },
                outcome="streaming",
                occurred_at=now,
                idempotency_key=f"idk_{delivery_id}",
            )
        )
        conn.commit()

    def test_withdraw_orphan_after_stale_turn(self, conn, clock):
        """Turn 租约过期 → stale_turns 复位 → 流式 Delivery 被撤回为 interrupted。"""
        from cogito.store.time_utils import epoch_ms

        now_ms = epoch_ms(clock.now())
        self._make_turn(conn, clock, "turn-1", lease_expires=now_ms - 60_000)  # 已过期
        self._make_streaming_delivery(conn, clock, "d1", "turn-1")

        svc = RecoveryService(conn, clock=clock)
        # 顺序与 recover_all 一致：先 stale_turns 再 streaming
        assert svc.recover_stale_turns() == 1
        assert svc.recover_streaming_deliveries() == 1

        assert replay_delivery(EventStore(conn).read_stream("delivery", "d1"), "d1").status == "cancelled"
        assert replay_turn(EventStore(conn).read_stream("turn", "turn-1"), "turn-1").status == "queued"

    def test_keeps_live_streaming_delivery(self, conn, clock):
        """存活的 Turn（attempt 仍 running、租约有效）→ 不撤回。"""
        from cogito.store.time_utils import epoch_ms

        now_ms = epoch_ms(clock.now())
        self._make_turn(conn, clock, "turn-1", lease_expires=now_ms + 5 * 60_000)
        self._make_streaming_delivery(conn, clock, "d1", "turn-1")

        svc = RecoveryService(conn, clock=clock)
        assert svc.recover_streaming_deliveries() == 0

        assert replay_delivery(EventStore(conn).read_stream("delivery", "d1"), "d1").status == "streaming"

    def test_withdraw_when_attempt_dead_but_turn_running(self, conn, clock):
        """Turn 名义 running 但 active attempt 已 abandoned → 撤回并复位 Turn。"""
        from cogito.store.time_utils import epoch_ms

        now_ms = epoch_ms(clock.now())
        # attempt 直接标记为 abandoned（模拟 stale_turns 已跑但 Turn 仍 running 的边界）
        self._make_turn(
            conn,
            clock,
            "turn-1",
            attempt_status="abandoned",
            lease_expires=now_ms - 60_000,
        )
        self._make_streaming_delivery(conn, clock, "d1", "turn-1")

        svc = RecoveryService(conn, clock=clock)
        assert svc.recover_streaming_deliveries() == 1

        assert replay_delivery(EventStore(conn).read_stream("delivery", "d1"), "d1").status == "cancelled"
        assert replay_turn(EventStore(conn).read_stream("turn", "turn-1"), "turn-1").status == "queued"

    def test_recover_all_includes_streaming(self, conn, clock):
        from cogito.store.time_utils import epoch_ms

        now_ms = epoch_ms(clock.now())
        self._make_turn(conn, clock, "turn-1", lease_expires=now_ms - 60_000)
        self._make_streaming_delivery(conn, clock, "d1", "turn-1")

        svc = RecoveryService(conn, clock=clock)
        result = svc.recover_all()
        assert result["streaming_deliveries"] == 1
        assert result["stale_turns"] == 1


class TestSchedulerRetry:
    """验证 connector.poll Task 失败后下次 tick 重新生成。"""

    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def clock(self):
        return FakeClock(start=datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC))

    def test_next_tick_creates_new_task(self, conn, clock):
        now = clock.now()
        s = Schedule(
            schedule_id="s1",
            schedule_type=ScheduleType.interval,
            expression="30m",
            next_fire_at=now,
            connector_id="c1",
        )
        ScheduleRepository(conn).insert(s)
        scheduler = Scheduler(conn, clock=clock)

        # 第一次触发
        tasks1 = scheduler.tick()
        assert len(tasks1) == 1

        # 推进 30min 后再次触发（新 Task）
        clock.advance_minutes(30)
        tasks2 = scheduler.tick()
        assert len(tasks2) == 1

        # 两个 Task 的 idempotency_key 不同（不同 fire_at）
        all_tasks = TaskRepository(conn).find_by_type("connector.poll")
        assert len(all_tasks) == 2
        keys = {t.idempotency_key for t in all_tasks}
        assert len(keys) == 2  # 不同 fire_at → 不同 key
