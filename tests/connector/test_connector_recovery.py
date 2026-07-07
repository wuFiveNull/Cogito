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
