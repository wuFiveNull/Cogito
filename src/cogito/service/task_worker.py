"""TaskWorker — 后台 Task 执行循环。

里程碑 B4：接入生产 Worker 循环。

模式复用 AgentRunner.run_once：
1. TaskDispatcher.claim_next → 领取 Task
2. TaskHandlerRegistry.get_handler → 找到处理器
3. 处理器执行（带 TaskHandlerContext）
4. TaskDispatcher.complete/fail → 完成或失败

每个 Task 通过 Lease 机制确保：同一时间只有一个 Worker 处理。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from enum import StrEnum

_LOGGER = logging.getLogger("cogito.task_worker")

from cogito.runtime.clock import Clock, ProductionClock
from cogito.service.task_dispatcher import TaskDispatcher
from cogito.service.task_handlers import TaskHandlerContext, TaskHandlerRegistry

TASK_WORKER_ID_PREFIX = "task-wkr-"


class TaskRunOutcome(StrEnum):
    idle = "idle"            # 无可用 Task
    completed = "completed"  # 成功完成
    failed = "failed"        # 执行失败
    lost = "lost"            # Lease 失效
    no_handler = "no_handler"  # 无对应处理器


class TaskWorker:
    """后台 Task 执行器。

    Worker 负责：
    - claim Task（带 Lease）
    - 用 TaskHandler 执行
    - complete/fail（条件更新）
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        dispatcher: TaskDispatcher,
        registry: TaskHandlerRegistry,
        handler_context: TaskHandlerContext | None = None,
        clock: Clock | None = None,
        heartbeat_interval_s: int = 30,
    ) -> None:
        self._conn = conn
        self._dispatcher = dispatcher
        self._registry = registry
        self._handler_ctx = handler_context or TaskHandlerContext()
        self._clock = clock or ProductionClock()
        self._heartbeat_interval_s = heartbeat_interval_s

    async def run_once(self, worker_id: str) -> TaskRunOutcome:
        """领取一个 Task 并执行完成。

        流程：
        1. claim_next（事务内）
        2. 查找 handler
        3. 执行（事务外，带 heartbeat）
        4. complete/fail（事务内）
        """
        # ── 1. 领取 Task ──
        try:
            claimed = self._dispatcher.claim_next(worker_id)
        except Exception as e:
            _LOGGER.warning("TaskWorker.claim_next error: %s", e)
            return TaskRunOutcome.failed

        if claimed is None:
            return TaskRunOutcome.idle

        task, attempt = claimed.task, claimed.attempt
        _LOGGER.info("TaskWorker claimed: %s type=%s", task.task_id, task.task_type)

        # ── 2. 查找 Handler ──
        handler = self._registry.get(task.task_type)
        if handler is None:
            _LOGGER.warning("No handler for task type: %s", task.task_type)
            try:
                self._dispatcher.fail(task, attempt, worker_id)
            except Exception:
                pass
            return TaskRunOutcome.no_handler

        # ── 3. 执行（后台心跳协程）──
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(task.task_id, attempt.task_attempt_id,
                                 worker_id, attempt.lease_version)
        )

        try:
            # Handler 可能在 async 子线程中调用模型
            result = await asyncio.to_thread(
                handler, task, self._handler_ctx,
            )
            _LOGGER.info(
                "Task handler completed: %s => %s",
                task.task_type, (result or "")[:100],
            )
        except Exception as e:
            _LOGGER.exception("Task handler failed: %s", e)
            heartbeat_task.cancel()
            try:
                self._dispatcher.fail(task, attempt, worker_id)
            except Exception:
                pass
            return TaskRunOutcome.failed

        heartbeat_task.cancel()

        # ── 4. 完成 ──
        try:
            ok = self._dispatcher.complete(task, attempt, worker_id)
        except Exception as e:
            _LOGGER.warning("Task complete error: %s", e)
            return TaskRunOutcome.failed

        if not ok:
            _LOGGER.warning("Task complete failed (lease lost): %s", task.task_id)
            return TaskRunOutcome.lost

        return TaskRunOutcome.completed

    async def _heartbeat_loop(
        self, task_id: str, attempt_id: str,
        worker_id: str, lease_version: int,
    ) -> None:
        """定期发送 heartbeat 防止 Lease 过期。"""
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            try:
                ok = self._dispatcher.heartbeat(
                    task_id, attempt_id, worker_id, lease_version,
                )
                if not ok:
                    return
            except Exception:
                return
