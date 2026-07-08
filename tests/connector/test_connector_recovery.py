"""Connector Task 恢复测试 —— Lease 过期、断点续传、重启恢复。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.task import Task, TaskAttempt, TaskAttemptStatus, TaskStatus
from cogito.domain.schedule import Schedule, ScheduleType
from cogito.runtime.clock import FakeClock
from cogito.service.recovery_service import RecoveryService
from cogito.service.scheduler import Scheduler
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
        expires = lease_expires if lease_expires is not None else (now + timedelta(minutes=5))
        task = Task(
            task_id=task_id,
            task_type="connector.poll",
            payload_ref="c1",
            status=TaskStatus.running,
            lease_owner="worker-1",
            lease_expires_at=expires,
            idempotency_key=f"t-{task_id}",
        )
        TaskRepository(conn).insert(task)
        attempt = TaskAttempt(
            task_id=task_id,
            attempt_no=1,
            status=TaskAttemptStatus.running,
            lease_owner="worker-1",
            lease_version=1,
            lease_expires_at=expires,
            started_at=now,
        )
        TaskAttemptRepository(conn).insert(attempt)
        conn.commit()
        return task, attempt

    def test_recover_expired_task(self, conn, clock):
        now = clock.now()
        # 已过期的 task
        self._make_running_task(
            conn, "t1", lease_expires=now - timedelta(minutes=1), clock=clock,
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
            conn, "t1", lease_expires=now + timedelta(minutes=5), clock=clock,
        )

        svc = RecoveryService(conn, clock=clock)
        count = svc.recover_stale_tasks()
        assert count == 0

        task = TaskRepository(conn).get("t1")
        assert task.status == TaskStatus.running

    def test_recover_all_includes_stale_tasks(self, conn, clock):
        now = clock.now()
        self._make_running_task(
            conn, "t1", lease_expires=now - timedelta(minutes=1), clock=clock,
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
        self, conn, clock, turn_id="turn-1", turn_status="running",
        attempt_status="running", lease_expires=None,
    ):
        from cogito.store.time_utils import epoch_ms

        now = epoch_ms(clock.now())
        expires = lease_expires if lease_expires is not None else (now + 5 * 60 * 1000)
        conn.execute(
            "INSERT INTO turns (turn_id, session_id, input_message_id, status, "
            "priority, version, created_at, active_attempt_id) "
            "VALUES (?, 's1', 'm1', ?, 80, 1, ?, ?)",
            (turn_id, turn_status, now, f"{turn_id}_a"),
        )
        conn.execute(
            "INSERT INTO run_attempts (attempt_id, turn_id, attempt_no, status, "
            "lease_version, lease_expires_at, started_at) "
            "VALUES (?, ?, 1, ?, 1, ?, ?)",
            (f"{turn_id}_a", turn_id, attempt_status, expires, now),
        )
        conn.commit()

    def _make_streaming_delivery(
        self, conn, clock, delivery_id="d1", turn_id="turn-1",
        platform_message_id="pm-1", conversation_id="web:dbg",
    ):
        import json

        from cogito.store.time_utils import epoch_ms

        now = epoch_ms(clock.now())
        target = {"delivery_id": delivery_id, "conversation_id": conversation_id}
        conn.execute(
            "INSERT INTO deliveries (delivery_id, target_snapshot, status, "
            "idempotency_key, created_at, content_mode, turn_id, platform_message_id) "
            "VALUES (?, ?, 'streaming', ?, ?, 'provisional', ?, ?)",
            (delivery_id, json.dumps(target), f"idk_{delivery_id}", now,
             turn_id, platform_message_id),
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

        d = conn.execute("SELECT status FROM deliveries WHERE delivery_id='d1'").fetchone()
        assert d["status"] == "interrupted"
        t = conn.execute("SELECT status FROM turns WHERE turn_id='turn-1'").fetchone()
        assert t["status"] == "queued"

    def test_keeps_live_streaming_delivery(self, conn, clock):
        """存活的 Turn（attempt 仍 running、租约有效）→ 不撤回。"""
        from cogito.store.time_utils import epoch_ms

        now_ms = epoch_ms(clock.now())
        self._make_turn(conn, clock, "turn-1", lease_expires=now_ms + 5 * 60_000)
        self._make_streaming_delivery(conn, clock, "d1", "turn-1")

        svc = RecoveryService(conn, clock=clock)
        assert svc.recover_streaming_deliveries() == 0

        d = conn.execute("SELECT status FROM deliveries WHERE delivery_id='d1'").fetchone()
        assert d["status"] == "streaming"

    def test_withdraw_when_attempt_dead_but_turn_running(self, conn, clock):
        """Turn 名义 running 但 active attempt 已 abandoned → 撤回并复位 Turn。"""
        from cogito.store.time_utils import epoch_ms

        now_ms = epoch_ms(clock.now())
        # attempt 直接标记为 abandoned（模拟 stale_turns 已跑但 Turn 仍 running 的边界）
        self._make_turn(
            conn, clock, "turn-1", attempt_status="abandoned",
            lease_expires=now_ms - 60_000,
        )
        self._make_streaming_delivery(conn, clock, "d1", "turn-1")

        svc = RecoveryService(conn, clock=clock)
        assert svc.recover_streaming_deliveries() == 1

        d = conn.execute("SELECT status FROM deliveries WHERE delivery_id='d1'").fetchone()
        assert d["status"] == "interrupted"
        t = conn.execute("SELECT status FROM turns WHERE turn_id='turn-1'").fetchone()
        assert t["status"] == "queued"

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
