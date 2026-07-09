"""Scheduler.tick() misfire 策略测试 —— 到期触发、幂等、misfire。

Plan 04 M2 / T1: skip / run_once / catch_up_limited / merge / DST。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.schedule import MisfirePolicy, Schedule, ScheduleType
from cogito.domain.task import TaskStatus
from cogito.runtime.clock import FakeClock
from cogito.service.scheduler import Scheduler
from cogito.store.schedule_repo import ScheduleRepository
from cogito.store.task_repo import TaskRepository


class TestSchedulerMisfire:
    @pytest.fixture
    def conn(self, in_memory_db):
        return in_memory_db

    @pytest.fixture
    def clock(self):
        return FakeClock(start=datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC))

    @pytest.fixture
    def scheduler(self, conn, clock):
        return Scheduler(conn, clock=clock)

    def _make_schedule(
        self,
        conn,
        *,
        policy=MisfirePolicy.skip,
        expression="30m",
        last_fire_at=None,
        max_catch_up=3,
    ) -> Schedule:
        now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        s = Schedule(
            schedule_id=f"sch-{policy.value}",
            schedule_type=ScheduleType.interval,
            expression=expression,
            next_fire_at=now,  # 已到期
            last_fire_at=last_fire_at,
            misfire_policy=policy,
            max_catch_up=max_catch_up,
            connector_id="c1",
        )
        ScheduleRepository(conn).insert(s)
        conn.commit()
        return s

    def test_misfire_skip_only_fires_once(self, conn, scheduler, clock):
        """misfire-02: 错过一次 → skip 只补一次。"""
        now = clock.now()
        # 上次触发在 1 小时前（interval=30m，错过 2 次）
        last = now - timedelta(hours=1)
        self._make_schedule(conn, policy=MisfirePolicy.skip, last_fire_at=last)

        tasks = scheduler.tick()
        assert len(tasks) == 1  # 只补一次

    def test_misfire_run_once_only_fires_once(self, conn, scheduler, clock):
        """misfire-03: 错过 5 次 → run_once 只补一次。"""
        now = clock.now()
        last = now - timedelta(hours=2, minutes=30)  # 错过 5 次（30m 间隔）
        self._make_schedule(conn, policy=MisfirePolicy.run_once, last_fire_at=last)

        tasks = scheduler.tick()
        assert len(tasks) == 1

    def test_misfire_catch_up_limited(self, conn, scheduler, clock):
        """misfire-04: 错过 5 次 → catch_up_limited(max=3) 补 3 次。"""
        now = clock.now()
        last = now - timedelta(hours=2, minutes=30)  # 错过 5 次
        self._make_schedule(
            conn, policy=MisfirePolicy.catch_up_limited,
            last_fire_at=last, max_catch_up=3,
        )

        tasks = scheduler.tick()
        assert len(tasks) == 3  # min(5, 3) = 3

    def test_misfire_catch_up_default_max(self, conn, scheduler, clock):
        """misfire-04b: 错过 2 次 → catch_up_limited 补 2 次。"""
        now = clock.now()
        last = now - timedelta(hours=1)  # 错过 2 次
        self._make_schedule(
            conn, policy=MisfirePolicy.catch_up_limited,
            last_fire_at=last, max_catch_up=3,
        )

        tasks = scheduler.tick()
        assert len(tasks) == 2  # min(2, 3) = 2

    def test_misfire_merge_single_task(self, conn, scheduler, clock):
        """misfire-05: 错过 3 次 → merge 生成 1 个 merged Task。"""
        now = clock.now()
        last = now - timedelta(hours=1, minutes=30)  # 错过 3 次
        self._make_schedule(conn, policy=MisfirePolicy.merge, last_fire_at=last)

        tasks = scheduler.tick()
        assert len(tasks) == 1
        # 验证 payload 携带合并元数据
        import json
        payload = json.loads(tasks[0].payload_ref)
        assert payload["merged_count"] == 3
        assert payload["connector_id"] == "c1"

    def test_no_miss_fires_single(self, conn, scheduler, clock):
        """misfire-01: 30s 间隔正常触发（无错过）。"""
        now = clock.now()
        # 上次触发在 31s 前（刚好超过 interval，但未达 1.5x）
        last = now - timedelta(seconds=31)
        self._make_schedule(conn, policy=MisfirePolicy.skip, expression="30s", last_fire_at=last)

        tasks = scheduler.tick()
        # 31s > 30s * 1.5 = 45s? No, 31 < 45, so NOT misfired → single fire
        assert len(tasks) == 1

    def test_misfire_idempotent_after_restart(self, conn, scheduler, clock):
        """misfire-09: 重启后 misfire fire 记录幂等。"""
        now = clock.now()
        last = now - timedelta(hours=1)
        self._make_schedule(conn, policy=MisfirePolicy.skip, last_fire_at=last)

        # 第一次 tick
        tasks1 = scheduler.tick()
        assert len(tasks1) == 1

        # 模拟重启：新建 scheduler，再次 tick
        scheduler2 = Scheduler(conn, clock=clock)
        tasks2 = scheduler2.tick()
        # 不应重复触发（幂等键已存在）
        assert len(tasks2) == 0
