"""TaskDispatcher — 领取 queued Task、创建 TaskAttempt、推进到 running。

模式复用 Dispatcher（Turn Dispatcher）的领取/完成/心跳模式：
1. claim_next → 领取 queued Task，创建带有效 Lease 的 TaskAttempt
2. complete → 标记成功（全量条件验证）
3. fail → 标记失败（全量条件验证）
4. heartbeat → 延长 Lease

GLOBAL-INVARIANTS / 2.5：旧 Lease/旧版本 Attempt 的结果不得提交业务状态。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import NamedTuple

from cogito.contracts.clock import Clock, ProductionClock, epoch_ms, from_epoch_ms
from cogito.domain.task import (
    Task,
    TaskAttempt,
    TaskAttemptStatus,
    TaskStatus,
)
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.task_repo import TaskAttemptRepository, TaskRepository

TASK_LEASE_TTL_S = 120


class ClaimedTask(NamedTuple):
    task: Task
    attempt: TaskAttempt


class TaskDispatcher:
    """Task 调度器 —— 领取、运行、完成、心跳。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lease_ttl_s: int = TASK_LEASE_TTL_S,
        clock: Clock | None = None,
    ) -> None:
        self._conn = conn
        self._lease_ttl_s = lease_ttl_s
        self._clock = clock or ProductionClock()
        self._task_repo = TaskRepository(conn)
        self._attempt_repo = TaskAttemptRepository(conn)

    def _now(self, override: datetime | None = None) -> datetime:
        return override if override is not None else self._clock.now()

    def claim_next(
        self, worker_id: str, clock: datetime | None = None,
    ) -> ClaimedTask | None:
        """领取 queued/scheduled Task，创建带有效 Lease 的 TaskAttempt。"""
        now = self._now(clock)
        now_ms = epoch_ms(now)
        lease_expires = now_ms + self._lease_ttl_s * 1000

        with UnitOfWork(self._conn) as uow:
            # 查找一个可领取的 Task
            task = self._task_repo.find_queued(limit=1, now=now)
            if not task:
                return None
            task = task[0]

            # 领取：queued → running
            ok = self._task_repo.claim(
                task.task_id, worker_id, self._lease_ttl_s * 1000, now_ms=now_ms,
            )
            if not ok:
                return None

            lease_version = self._conn.execute(
                "SELECT lease_version FROM tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()[0]

            # 计算 attempt_no
            max_no = self._conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM task_attempts WHERE task_id=?",
                (task.task_id,),
            ).fetchone()[0]

            # 创建 TaskAttempt
            attempt = TaskAttempt(
                task_id=task.task_id,
                attempt_no=max_no,
                status=TaskAttemptStatus.created,
                lease_owner=worker_id,
                lease_version=lease_version,
                lease_expires_at=from_epoch_ms(lease_expires),
                started_at=now,
            )

            # 插入 Attempt
            self._attempt_repo.insert(attempt)

            # 更新 Attempt 状态为 running
            self._conn.execute(
                "UPDATE task_attempts SET status='running' "
                "WHERE task_attempt_id=?",
                (attempt.task_attempt_id,),
            )

            # 更新 Task 状态（已由 claim 完成）
            task.status = TaskStatus.running
            task.lease_owner = worker_id
            task.lease_expires_at = from_epoch_ms(lease_expires)
            attempt.status = TaskAttemptStatus.running

            uow.commit()

        return ClaimedTask(task=task, attempt=attempt)

    def complete(
        self,
        task: Task,
        attempt: TaskAttempt,
        worker_id: str,
        clock: datetime | None = None,
    ) -> bool:
        """完成 Task。全量条件验证。"""
        now_ms = epoch_ms(self._now(clock))

        with UnitOfWork(self._conn) as uow:
            ok = self._task_repo.complete(
                task.task_id, worker_id, attempt.lease_version, now_ms=now_ms,
            )
            if not ok:
                return False

            ok = self._attempt_repo.succeed(attempt.task_attempt_id, finished_at=now_ms)
            if not ok:
                uow.rollback()
                return False

            uow.commit()
            return True

    def fail(
        self,
        task: Task,
        attempt: TaskAttempt,
        worker_id: str,
        clock: datetime | None = None,
    ) -> bool:
        """标记 Task 失败。全量条件验证。"""
        now_ms = epoch_ms(self._now(clock))

        with UnitOfWork(self._conn) as uow:
            ok = self._task_repo.fail(
                task.task_id, worker_id, attempt.lease_version, now_ms=now_ms,
            )
            if not ok:
                return False

            ok = self._attempt_repo.fail(attempt.task_attempt_id, finished_at=now_ms)
            uow.commit()
            return ok

    def retry(
        self,
        task: Task,
        attempt: TaskAttempt,
        worker_id: str,
        *,
        delay_seconds: int,
        clock: datetime | None = None,
    ) -> bool:
        """Finish the current attempt and schedule a new attempt after backoff."""
        now_ms = epoch_ms(self._now(clock))
        scheduled_at = now_ms + max(0, delay_seconds) * 1000
        with UnitOfWork(self._conn) as uow:
            cur = self._conn.execute(
                "UPDATE tasks SET status='scheduled',scheduled_at=?,lease_owner=NULL,"
                "lease_expires_at=NULL WHERE task_id=? AND lease_owner=? "
                "AND lease_version=? AND status='running' AND lease_expires_at>?",
                (
                    scheduled_at,
                    task.task_id,
                    worker_id,
                    attempt.lease_version,
                    now_ms,
                ),
            )
            if cur.rowcount != 1:
                return False
            ok = self._attempt_repo.fail(attempt.task_attempt_id, finished_at=now_ms)
            uow.commit()
            return ok

    def heartbeat(
        self,
        task_id: str,
        attempt_id: str,
        worker_id: str,
        lease_version: int,
        clock: datetime | None = None,
    ) -> bool:
        """延长当前有效 Lease。"""
        now_ms = epoch_ms(self._now(clock))

        return self._task_repo.heartbeat(
            task_id, worker_id, lease_version,
            self._lease_ttl_s * 1000, now_ms=now_ms,
        )
