"""Scheduler.tick() 触发测试 —— 到期触发、幂等、misfire。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.schedule import Schedule, ScheduleType
from cogito.domain.task import TaskStatus
from cogito.runtime.clock import FakeClock
from cogito.service.scheduler import Scheduler
from cogito.store.schedule_repo import ScheduleRepository
from cogito.store.task_repo import TaskRepository


class TestSchedulerTick:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def clock(self):
        return FakeClock(start=datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC))

    @pytest.fixture
    def scheduler(self, conn, clock):
        return Scheduler(conn, clock=clock)

    def test_tick_creates_poll_task_when_due(self, conn, scheduler, clock):
        now = clock.now()
        s = Schedule(
            schedule_id="s1",
            schedule_type=ScheduleType.interval,
            expression="30m",
            next_fire_at=now - timedelta(seconds=1),  # 已到期
            connector_id="c1",
        )
        ScheduleRepository(conn).insert(s)

        tasks = scheduler.tick()
        assert len(tasks) == 1
        assert tasks[0].task_type == "connector.poll"
        assert tasks[0].payload_ref == "c1"
        assert tasks[0].status == TaskStatus.queued
        assert tasks[0].scheduled_at == now - timedelta(seconds=1)
        assert TaskRepository(conn).find_queued(now=now)[0].task_id == tasks[0].task_id

    def test_tick_updates_next_fire_time(self, conn, scheduler, clock):
        now = clock.now()
        s = Schedule(
            schedule_id="s1",
            schedule_type=ScheduleType.interval,
            expression="30m",
            next_fire_at=now,
            connector_id="c1",
        )
        ScheduleRepository(conn).insert(s)

        scheduler.tick()
        updated = ScheduleRepository(conn).get("s1")
        assert updated.next_fire_at is not None
        # interval 30m → next = now + 30min
        assert updated.next_fire_at == now + timedelta(minutes=30)

    def test_tick_idempotent_no_duplicate_tasks(self, conn, scheduler, clock):
        now = clock.now()
        s = Schedule(
            schedule_id="s1",
            schedule_type=ScheduleType.interval,
            expression="30m",
            next_fire_at=now,
            connector_id="c1",
        )
        ScheduleRepository(conn).insert(s)

        tasks1 = scheduler.tick()
        assert len(tasks1) == 1
        # 第二次 tick（仍在 30min 内）不应再触发
        tasks2 = scheduler.tick()
        assert len(tasks2) == 0

    def test_tick_skips_disabled_schedule(self, conn, scheduler, clock):
        now = clock.now()
        s = Schedule(
            schedule_id="s1",
            next_fire_at=now,
            enabled=False,
            connector_id="c1",
        )
        ScheduleRepository(conn).insert(s)
        tasks = scheduler.tick()
        assert len(tasks) == 0

    def test_tick_skips_future_schedule(self, conn, scheduler, clock):
        now = clock.now()
        s = Schedule(
            schedule_id="s1",
            next_fire_at=now + timedelta(hours=1),
            connector_id="c1",
        )
        ScheduleRepository(conn).insert(s)
        tasks = scheduler.tick()
        assert len(tasks) == 0

    def test_tick_fires_after_advance(self, conn, scheduler, clock):
        now = clock.now()
        s = Schedule(
            schedule_id="s1",
            schedule_type=ScheduleType.interval,
            expression="30m",
            next_fire_at=now,
            connector_id="c1",
        )
        ScheduleRepository(conn).insert(s)

        # 第一次触发
        tasks1 = scheduler.tick()
        assert len(tasks1) == 1

        # 推进 30min 后再次触发
        clock.advance_minutes(30)
        tasks2 = scheduler.tick()
        assert len(tasks2) == 1

        # 总共 2 个 task
        all_tasks = TaskRepository(conn).find_by_type("connector.poll")
        assert len(all_tasks) == 2

    def test_tick_creates_fire_record(self, conn, scheduler, clock):
        now = clock.now()
        s = Schedule(
            schedule_id="s1",
            next_fire_at=now,
            connector_id="c1",
        )
        ScheduleRepository(conn).insert(s)
        scheduler.tick()

        from cogito.domain.schedule import FireStatus
        from cogito.store.schedule_repo import ScheduledFireRepository

        fire = ScheduledFireRepository(conn).find("s1", now)
        assert fire is not None
        assert fire.status == FireStatus.fired
        assert fire.task_id is not None
