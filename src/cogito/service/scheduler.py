"""Scheduler —— 周期触发到期 schedule，生成 connector.poll Task。

每次 tick：
1. 查 due schedules（enabled AND next_fire_at <= now）
2. 逐条 claim schedule lease（条件更新版本号防并发）
3. 幂等创建 ScheduledFire
4. 创建 connector.poll Task（payload_ref 指向 connector_id）
5. 更新 next_fire_at（按表达式计算下次）并释放 lease
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from cogito.domain.schedule import (
    FireStatus,
    Schedule,
    ScheduledFire,
    next_fire_at,
)
from cogito.domain.task import Task, TaskStatus
from cogito.runtime.clock import Clock, ProductionClock
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.schedule_repo import ScheduledFireRepository, ScheduleRepository
from cogito.store.task_repo import TaskRepository
from cogito.store.time_utils import epoch_ms

_LOGGER = logging.getLogger(__name__)

POLL_TASK_TYPE = "connector.poll"
MCP_POLL_TASK_TYPE = "mcp_connector.poll"


class Scheduler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or ProductionClock()
        self._schedule_repo = ScheduleRepository(conn)
        self._fire_repo = ScheduledFireRepository(conn)
        self._task_repo = TaskRepository(conn)

    @staticmethod
    def task_type_for_connector(connector_type: str) -> str:
        """根据 Connector 类型分派 Task 类型。

        RSS/Atom/JSON → connector.poll（现有 RSS 流程）
        MCP → mcp_connector.poll（本计划新增）
        """
        if connector_type == "mcp":
            return MCP_POLL_TASK_TYPE
        return POLL_TASK_TYPE

    def _now(self) -> datetime:
        return self._clock.now()

    def tick(self, limit: int = 10) -> list[Task]:
        """执行一轮调度，返回创建的 Task 列表。"""
        now = self._now()
        due = self._schedule_repo.find_due(now, limit=limit)
        created: list[Task] = []

        for schedule in due:
            try:
                task = self._process_schedule(schedule, now)
                if task is not None:
                    created.append(task)
            except Exception:
                _LOGGER.exception("Scheduler: failed to process %s", schedule.schedule_id)

        return created

    def _process_schedule(self, schedule: Schedule, now: datetime) -> Task | None:
        """处理单条到期 schedule。返回创建的 Task，或 None（跳过）。"""
        fire_at = schedule.next_fire_at or now

        # 幂等：此 fire_at 是否已触发过
        existing = self._fire_repo.find(schedule.schedule_id, fire_at)
        if existing is not None and existing.status == FireStatus.fired:
            # 已触发但仍出现在 due 列表 —— 推进到下次
            nxt = next_fire_at(schedule.expression, schedule.timezone, now)
            if nxt and nxt != schedule.next_fire_at:
                self._schedule_repo.update_fire_time(
                    schedule.schedule_id, nxt, now, schedule.version,
                )
            return None

        # Claim schedule lease: 条件更新版本号（将 next_fire_at 暂设为自身以锁定）
        if not self._schedule_repo.update_fire_time(
            schedule.schedule_id, schedule.next_fire_at, now, schedule.version,
        ):
            return None  # 并发竞争失败，跳过

        with UnitOfWork(self._conn) as uow:
            # 创建 fire 记录
            fire = ScheduledFire(
                schedule_id=schedule.schedule_id,
                scheduled_fire_at=fire_at,
                status=FireStatus.fired,
            )
            self._fire_repo.insert(fire)

            # 计算下次触发时间
            nxt = next_fire_at(schedule.expression, schedule.timezone, now)

            # 选择 task_type：按 connector_type 分派（mcp vs rss/json/atom）
            task_type = POLL_TASK_TYPE
            if schedule.connector_id:
                row = self._conn.execute(
                    "SELECT connector_type FROM connectors WHERE connector_id=?",
                    (schedule.connector_id,),
                ).fetchone()
                if row is not None:
                    task_type = self.task_type_for_connector(row[0])

            # 创建 connector.poll / mcp_connector.poll Task
            task = Task(
                task_type=task_type,
                payload_ref=schedule.connector_id or "",
                status=TaskStatus.queued,
                priority=40,
                scheduled_at=nxt,
                idempotency_key=f"{schedule.schedule_id}:{epoch_ms(fire_at)}",
                origin="scheduler",
            )
            self._task_repo.insert(task)

            # 更新 schedule 的 next_fire_at（version 再 +1）
            self._conn.execute(
                "UPDATE schedules SET next_fire_at=?, version=version+1 "
                "WHERE schedule_id=?",
                (epoch_ms(nxt), schedule.schedule_id),
            )

            # 回填 fire.task_id
            self._fire_repo.update_status(
                fire.fire_id, FireStatus.fired, task.task_id,
            )

            uow.commit()

        _LOGGER.info(
            "Scheduler: schedule=%s fired at %s, next=%s, task=%s",
            schedule.schedule_id, fire_at.isoformat(), nxt.isoformat() if nxt else None,
            task.task_id,
        )
        return task
