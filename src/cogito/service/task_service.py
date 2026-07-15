"""TaskService —— Task/TaskAttempt 聚合的唯一公开写入口。

SYSTEM-BOUNDARIES / 4: Task/TaskAttempt 的唯一写入者是 TaskService。
其他模块通过 TaskService 或 Command 请求变更，不得直接操作 task 表。

当前实现：`SqliteTaskService`（SQLite 后端）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from cogito.domain.task import Task, TaskAttempt, TaskStatus


@dataclass(frozen=True)
class TaskClaim:
    """Task 领取结果。"""

    task_id: str
    attempt_id: str
    task: Task
    attempt: TaskAttempt


@dataclass(frozen=True)
class TaskOutcome:
    """TaskAttempt 执行结果。"""

    attempt_id: str
    status: TaskStatus
    result: str = ""


class TaskService(Protocol):
    """Task 生命周期管理接口。

    唯一写入口：所有 Task/TaskAttempt 的状态变更经此接口。
    """

    def create(
        self,
        task_type: str,
        payload_ref: str = "",
        *,
        scheduled_at: datetime | None = None,
        idempotency_key: str = "",
        origin: str = "system",
        priority: int = 0,
        retry_policy: dict[str, Any] | None = None,
    ) -> Task:
        """创建并排队一个新 Task。"""
        ...

    def claim(self, worker_id: str) -> TaskClaim | None:
        """原子领取下一个可执行的 Task，创建 TaskAttempt + Lease。"""
        ...

    def heartbeat(self, task: Task, attempt: TaskAttempt) -> bool:
        """续租。返回 False 表示 Lease 已失效。"""
        ...

    def complete(self, task: Task, attempt: TaskAttempt, worker_id: str) -> TaskOutcome:
        """提交成功结果，完成 Attempt + Task。"""
        ...

    def fail(self, task: Task, attempt: TaskAttempt, worker_id: str) -> TaskOutcome:
        """提交失败结果；超出重试上限则 Task 进入 failed。"""
        ...

    def cancel(self, task_id: str, reason: str = "") -> bool:
        """取消 Task（仅 queued/running 可取消）。"""
        ...

    def retry(self, task_id: str) -> bool:
        """重置 failed/cancelled 的 Task 为 queued。"""
        ...

    def get(self, task_id: str) -> Task | None:
        """按 ID 获取 Task。"""
        ...

    def get_attempt(self, attempt_id: str) -> TaskAttempt | None:
        """按 ID 获取 TaskAttempt。"""
        ...


class SqliteTaskService:
    """TaskService 的 SQLite 实现。

    内部使用 TaskRepository / TaskAttemptRepository / TaskDispatcher，
    对外暴露稳定的 TaskService Protocol。
    """

    def __init__(self, conn: Any) -> None:
        from cogito.service.task_dispatcher import TaskDispatcher
        from cogito.store.task_repo import TaskAttemptRepository, TaskRepository

        self._conn = conn
        self._task_repo = TaskRepository(conn)
        self._attempt_repo = TaskAttemptRepository(conn)
        self._dispatcher = TaskDispatcher(conn)

    def create(
        self,
        task_type: str,
        payload_ref: str = "",
        *,
        scheduled_at: datetime | None = None,
        idempotency_key: str = "",
        origin: str = "system",
        priority: int = 0,
        retry_policy: dict[str, Any] | None = None,
    ) -> Task:
        task = Task(
            task_type=task_type,
            payload_ref=payload_ref,
            status=TaskStatus.queued,
            scheduled_at=scheduled_at,
            idempotency_key=idempotency_key,
            origin=origin,
            priority=priority,
            retry_policy=retry_policy or {},
        )
        return self._task_repo.insert(task)

    def claim(self, worker_id: str) -> TaskClaim | None:
        from cogito.service.task_dispatcher import ClaimedTask

        claimed: ClaimedTask | None = self._dispatcher.claim_next(worker_id)
        if claimed is None:
            return None
        return TaskClaim(
            task_id=claimed.task.task_id,
            attempt_id=claimed.attempt.task_attempt_id,
            task=claimed.task,
            attempt=claimed.attempt,
        )

    def heartbeat(self, task: Task, attempt: TaskAttempt) -> bool:
        return self._dispatcher.heartbeat(
            task.task_id,
            attempt.task_attempt_id,
            attempt.lease_owner or "",
            attempt.lease_version,
        )

    def complete(self, task: Task, attempt: TaskAttempt, worker_id: str) -> TaskOutcome:
        ok = self._dispatcher.complete(task, attempt, worker_id)
        status = TaskStatus.completed if ok else attempt.status
        return TaskOutcome(attempt_id=attempt.task_attempt_id, status=status)

    def fail(self, task: Task, attempt: TaskAttempt, worker_id: str) -> TaskOutcome:
        ok = self._dispatcher.fail(task, attempt, worker_id)
        status = TaskStatus.failed if ok else attempt.status
        return TaskOutcome(attempt_id=attempt.task_attempt_id, status=status)

    def cancel(self, task_id: str, reason: str = "") -> bool:
        return self._task_repo.cancel(task_id, reason=reason)

    def retry(self, task_id: str) -> bool:
        return self._task_repo.reset_to_queued(task_id)

    def get(self, task_id: str) -> Task | None:
        return self._task_repo.get(task_id)

    def get_attempt(self, attempt_id: str) -> TaskAttempt | None:
        return self._attempt_repo.get_attempt(attempt_id)
