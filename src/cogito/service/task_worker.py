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

from cogito.contracts.clock import Clock, ProductionClock
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
            # MEM-01: 将 handler 声明的记忆依赖持久化到 task.result_ref
            self._persist_declared_dependencies(task)
        except Exception as e:
            _LOGGER.exception("Task handler failed: %s", e)
            heartbeat_task.cancel()
            try:
                policy = task.retry_policy or {}
                max_attempts = int(policy.get("max_attempts", 1))
                retryable = bool(getattr(e, "retryable", True))
                if retryable and attempt.attempt_no < max_attempts:
                    backoffs = policy.get("backoff_seconds", [5])
                    if not isinstance(backoffs, list) or not backoffs:
                        backoffs = [5]
                    index = min(attempt.attempt_no - 1, len(backoffs) - 1)
                    self._dispatcher.retry(
                        task,
                        attempt,
                        worker_id,
                        delay_seconds=int(backoffs[index]),
                    )
                else:
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

        # PLAN-16 M3 MEM-01: Task 成功后，按 handler 声明的 memory_dependencies
        # 写出 task_succeeded 信号（禁止从任意文本猜测依赖）。
        self._emit_task_succeeded_signals(task)

        return TaskRunOutcome.completed

    def _emit_task_succeeded_signals(self, task: Any) -> None:
        """读取任务 result_ref 中声明的 memory_dependencies 并写 task_succeeded。

        handler 在 result_ref 中以 JSON {"memory_dependencies": [mid, ...]} 声明
        其使用并强化的记忆；失败仅记录日志，不影响 Task 完成状态本身。
        """
        import json

        result_ref = getattr(task, "result_ref", None)
        if not result_ref:
            return
        try:
            data = json.loads(result_ref)
        except (json.JSONDecodeError, TypeError):
            return
        deps = data.get("memory_dependencies")
        if not deps:
            return
        try:
            from cogito.service.memory_signals import SignalWriter
            conn = getattr(self._dispatcher, "_conn", None)
            if conn is None:
                return
            writer = SignalWriter(conn)
            for mid in deps:
                try:
                    writer.record_task_succeeded(
                        str(mid),
                        task_id=task.task_id,
                        idempotency_key=f"task-succeeded:{task.task_id}:{mid}",
                        algorithm_version="2",
                    )
                except Exception as e:
                    _LOGGER.warning(
                        "task_succeeded signal failed for %s: %s", mid, e)
        except Exception as e:
            _LOGGER.warning("emit_task_succeeded_signals failed: %s", e)

    def _persist_declared_dependencies(self, task: Any) -> None:
        """把 handler 声明的记忆依赖写入 task.result_ref（PLAN-16 M3 MEM-01）。"""
        import json

        deps = getattr(self._handler_ctx, "declared_memory_dependencies", None)
        if not deps:
            return
        try:
            task.result_ref = json.dumps({"memory_dependencies": list(deps)})
        except Exception as e:
            _LOGGER.warning("persist_declared_dependencies failed: %s", e)

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
