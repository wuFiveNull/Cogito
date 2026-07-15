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
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

_LOGGER = logging.getLogger("cogito.task_worker")

from cogito.contracts.clock import Clock, ProductionClock
from cogito.service.task_dispatcher import TaskDispatcher
from cogito.service.task_handlers import TaskHandlerContext, TaskHandlerRegistry, TaskHandlerWait

TASK_WORKER_ID_PREFIX = "task-wkr-"


class TaskRunOutcome(StrEnum):
    idle = "idle"  # 无可用 Task
    completed = "completed"  # 成功完成
    failed = "failed"  # 执行失败
    lost = "lost"  # Lease 失效
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
        # MEM-01 完整：每个 Task 开始前清空共享依赖字段，避免跨 Task 继承
        self._handler_ctx.declared_memory_dependencies = []
        # PLAN-17 R3 P0-03：把本次 Attempt 的真实 task_attempt_id 注入，让
        # TaskHandler 能把 checkpoint 绑定到真实 Attempt（不再依赖 fallback SELECT）。
        self._handler_ctx._task_id = task.task_id
        self._handler_ctx._attempt_id = attempt.task_attempt_id
        # PLAN-17 R4 P0-05：注入 DB 校验的 lease_checker，用
        # task_id+attempt_id+worker_id+lease_version 条件查询 tasks 表，
        # 避免外部 heartbeat 失败/抢占时仍默认 lease_valid=True 继续执行。
        _lease_version = attempt.lease_version
        _task_id = task.task_id
        _worker = worker_id

        def _lease_checker() -> bool:
            try:
                conn = self._conn
                row = conn.execute(
                    "SELECT lease_version, lease_expires_at FROM tasks "
                    "WHERE task_id=? AND lease_owner=? AND status='running'",
                    (_task_id, _worker),
                ).fetchone()
                if row is None:
                    return False
                if row["lease_version"] != _lease_version:
                    return False
                exp = row["lease_expires_at"]
                # lease_expires_at stored as epoch ms int
                if isinstance(exp, (int, float)):
                    return exp > int(datetime.now(UTC).timestamp() * 1000)
                return True
            except Exception:
                return False

        self._handler_ctx.lease_checker = _lease_checker
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                task.task_id, attempt.task_attempt_id, worker_id, attempt.lease_version
            )
        )

        try:
            # 异步 Handler（调用模型/ httpx 的）必须在主 loop 上 await，
            # 复用主 loop 的 httpx 连接池，避免 loop 不匹配错误。
            # 同步 Handler（纯 SQLite）仍在 to_thread 中运行，避免阻塞主 loop。
            if asyncio.iscoroutinefunction(handler):
                result = await handler(task, self._handler_ctx)
            else:
                result = await asyncio.to_thread(
                    handler,
                    task,
                    self._handler_ctx,
                )
            _LOGGER.info(
                "Task handler completed: %s => %s",
                task.task_type,
                (result or "")[:100],
            )
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
                    self._evaluate_delegation(task.task_id)
            except Exception:
                pass
            return TaskRunOutcome.failed

        heartbeat_task.cancel()

        if isinstance(result, TaskHandlerWait):
            try:
                now_ms = int(datetime.now(UTC).timestamp() * 1000)
                self._conn.execute(
                    "UPDATE task_attempts SET status='succeeded',finished_at=? "
                    "WHERE task_attempt_id=? AND status='running'",
                    (now_ms, attempt.task_attempt_id),
                )
                self._conn.execute(
                    "UPDATE tasks SET status=?,lease_owner=NULL,lease_expires_at=NULL,"
                    "checkpoint_ref=? WHERE task_id=? AND status='running' AND lease_owner=?",
                    (result.status, result.waiting_id, task.task_id, worker_id),
                )
                self._conn.commit()
                return TaskRunOutcome.completed
            except Exception:
                self._conn.rollback()
                return TaskRunOutcome.failed

        # ── 4. 完成 ──
        # MEM-01 完整：先将 handler 声明的依赖持久化到 result_ref
        self._persist_declared_dependencies(task)
        if result and not task.result_ref:
            task.result_ref = str(result)

        # MEM-01 + #11 完整：事实型 task_succeeded 信号在 complete 之前写入，
        # 与 Task/Attempt 完成同事务原子提交；失败向上传播（禁止 silent pass）
        try:
            self._emit_task_succeeded_signals(task)
        except Exception as e:
            _LOGGER.warning("task_succeeded signals failed for %s: %s", task.task_id, e)
            return TaskRunOutcome.failed

        try:
            ok = self._dispatcher.complete(task, attempt, worker_id)
        except Exception as e:
            _LOGGER.warning("Task complete error: %s", e)
            return TaskRunOutcome.failed

        if not ok:
            _LOGGER.warning("Task complete failed (lease lost): %s", task.task_id)
            return TaskRunOutcome.lost

        self._evaluate_delegation(task.task_id)

        return TaskRunOutcome.completed

    def _evaluate_delegation(self, task_id: str) -> None:
        try:
            from cogito.service.delegation_lifecycle import DelegationLifecycleService

            DelegationLifecycleService(self._conn).evaluate_for_task(task_id)
        except Exception:
            _LOGGER.exception("delegation join evaluation failed for %s", task_id)

    def _emit_task_succeeded_signals(self, task: Any) -> None:
        """Task 成功后写 task_succeeded 信号（PLAN-16 MEM-01 完整原子语义）。

        信号与 Task/Attempt 完成在同一事务中由 dispatcher.complete 提交。
        失败向上传播（不再 silent pass），确保事实型信号 durable。
        handler 必须通过 ctx.declare_memory_dependencies 显式声明依赖
        （禁止从任意文本猜测）。
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
        from cogito.service.memory_signals import SignalWriter

        conn = getattr(self._dispatcher, "_conn", None)
        if conn is None:
            return
        writer = SignalWriter(conn)
        # 失败即抛出 → 事务回滚，Task 不标记成功
        for mid in deps:
            writer.record_task_succeeded(
                str(mid),
                task_id=task.task_id,
                idempotency_key=f"task-succeeded:{task.task_id}:{mid}",
                algorithm_version="2",
            )

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
        self,
        task_id: str,
        attempt_id: str,
        worker_id: str,
        lease_version: int,
    ) -> None:
        """定期发送 heartbeat 防止 Lease 过期。"""
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            try:
                ok = self._dispatcher.heartbeat(
                    task_id,
                    attempt_id,
                    worker_id,
                    lease_version,
                )
                if not ok:
                    return
            except Exception:
                return
