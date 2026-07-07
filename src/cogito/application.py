"""RuntimeApplication — composition root for runtime lifecycle.

Single source of truth for:
- SQLite connection ownership
- Migration and recovery-all at startup (RB-06)
- Provider / Registry / Executor / AgentRunner / InboundService assembly
- Terminal interactive REPL and background worker entrypoints
- Idempotent close()

Plan 02 / `RUNNABLE-BASELINE-01` — 统一装配，避免 worker vs interactive 漂移 (RB-07)。
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import uuid
from typing import Any

from cogito.config import Config
from cogito.contracts.envelope import ChannelEnvelope
from cogito.model.provider import ModelProvider
from cogito.service.agent_runner import RunOutcome, build_agent_runner
from cogito.service.inbound_service import InboundService
from cogito.service.recovery_service import RecoveryService
from cogito.store.connection import get_connection
from cogito.store.migration import migrate

logger = logging.getLogger("cogito.application")


class RuntimeApplication:
    """组合根：拥有 SQLite 连接和运行期服务。"""

    def __init__(
        self,
        *,
        config: Config,
        conn: sqlite3.Connection,
        provider: ModelProvider,
        runner: Any,
        inbound: InboundService,
    ) -> None:
        self.config = config
        self.conn = conn
        self.provider = provider
        self.runner = runner
        self.inbound = inbound
        self._terminal_seq = 0
        self._instance_id = uuid.uuid4().hex
        self._closed = False
        self._recovery_counts: dict[str, int] = {}

    # ── factory ────────────────────────────────────────────────────────────

    @classmethod
    def build(cls, config: Config) -> "RuntimeApplication":
        """构造完整的 RuntimeApplication。

        启动顺序 (LOCAL-OPERATIONS / 3):
        1. 解析工作区路径
        2. 打开 SQLite（WAL、foreign_keys=ON）
        3. 执行 Migration（幂等，含 FK check）
        4. RecoveryService.recover_all（在新工作前）
        5. 选择并构建 Provider
        6. 构建 AgentRunner / InboundService
        """
        db_path = config.resolve_db_path()
        conn = get_connection(db_path)

        try:
            migrate(conn)
        except Exception:
            conn.close()
            raise

        try:
            recovery = RecoveryService(conn)
            recovery_counts = recovery.recover_all()
            logger.info(
                "Startup recovery → outbox=%d delivery=%d stale_turns=%d",
                recovery_counts.get("outbox_leases", 0),
                recovery_counts.get("delivery_leases", 0),
                recovery_counts.get("stale_turns", 0),
            )
        except Exception:
            conn.close()
            raise

        from cogito.service.agent_runner import _create_provider

        provider = _create_provider(config.model)
        if config.model.main.is_configured():
            logger.info(
                "Using model: %s (%s)",
                config.model.main.model,
                config.model.main.base_url,
            )
        else:
            logger.warning("No model configured — using stub provider")
            print("[stub] 未配置模型，使用 Stub Provider（固定回复）")

        runner = build_agent_runner(
            config=config,
            connection=conn,
            provider=provider,
        )
        inbound = InboundService(conn)

        app = cls(
            config=config,
            conn=conn,
            provider=provider,
            runner=runner,
            inbound=inbound,
        )
        app._recovery_counts = recovery_counts
        return app

    # ── read-only accessors ────────────────────────────────────────────────

    def recovery_counts(self) -> dict[str, int]:
        return dict(self._recovery_counts)

    # ── terminal interactive ───────────────────────────────────────────────

    def _next_terminal_message_id(self) -> str:
        """稳定的本进程消息 ID，避免幂等键冲突（RB-06 / 入站事务）。"""
        seq = self._terminal_seq
        self._terminal_seq += 1
        return f"terminal:{self._instance_id}:{seq}"

    async def process_terminal_message(self, text: str) -> str:
        """注入一条 terminal 消息、执行一轮 Turn、返回 assistant 回复文本。

        消息 ID 以 terminal:<session_uuid>:<monotonic_sequence> 生成；返回值
        以本轮 turn_id / output message ref 为边界，不用 "最新一条 assistant" 猜测。
        """
        from datetime import UTC, datetime

        msg_id = self._next_terminal_message_id()
        result = self.inbound.accept(ChannelEnvelope(
            channel_type="terminal",
            channel_instance_id="terminal",
            platform_sender_id="owner",
            platform_conversation_id="terminal:default",
            platform_message_id=msg_id,
            content_parts=[{"content_type": "text", "inline_data": text}],
            received_at=datetime.now(UTC).isoformat(),
        ))

        outcome = await self.runner.run_once("terminal-worker")
        if outcome == RunOutcome.completed:
            # Bound to THIS turn's input_message_id via reply_to_message_id —
            # never "most recent assistant in conversation".
            row = self.conn.execute(
                "SELECT cp.inline_data "
                "FROM messages m "
                "JOIN content_parts cp ON cp.message_id = m.message_id "
                "WHERE m.role='assistant' "
                "AND m.reply_to_message_id = ? "
                "AND cp.content_type='text' "
                "ORDER BY m.created_at DESC LIMIT 1",
                (result.message_id,),
            ).fetchone()
            if row:
                return row["inline_data"]
            return "(no assistant message attached to this turn)"
        if outcome == RunOutcome.failed:
            return "(turn failed — check logs)"
        if outcome == RunOutcome.cancelled:
            return "(turn cancelled)"
        # idle / lost / anything else
        logger.warning("Unexpected terminal outcome: %s (turn_id=%s)", outcome, result.turn_id)
        return f"(unexpected outcome: {outcome})"

    # ── worker entrypoint ──────────────────────────────────────────────────

    async def run_worker(
        self,
        worker_id: str,
        poll_interval: float,
        *,
        run_once: bool = False,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        """Worker 循环：轮询 Turn → 处理 Delivery → 处理 Task。"""
        from cogito.service.task_dispatcher import TaskDispatcher
        from cogito.service.task_handlers import TaskHandlerContext, _build_registry
        from cogito.service.task_worker import TASK_WORKER_ID_PREFIX, TaskWorker

        task_handler_ctx = TaskHandlerContext(
            connection_factory=lambda p=self.config.resolve_db_path(): get_connection(p),
            workspace_path=self.config.workspace_path,
        )
        task_registry = _build_registry(task_handler_ctx)
        task_dispatcher = TaskDispatcher(self.conn)
        task_worker = TaskWorker(
            conn=self.conn,
            dispatcher=task_dispatcher,
            registry=task_registry,
            handler_context=task_handler_ctx,
            heartbeat_interval_s=self.config.worker.heartbeat_interval_seconds,
        )

        local_shutdown = shutdown_event or asyncio.Event()

        def _handle_signal() -> None:
            logger.info("Shutdown signal received, stopping...")
            local_shutdown.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except (ValueError, NotImplementedError):
                pass  # Windows: no add_signal_handler

        logger.info("Starting worker loop (poll interval: %.1fs)...", poll_interval)
        print("[ok] Agent is running. Press Ctrl+C to stop.")

        try:
            while not local_shutdown.is_set():
                outcome = await self.runner.run_once(worker_id)
                if outcome == RunOutcome.idle:
                    if task_worker:
                        t = await task_worker.run_once(
                            f"{TASK_WORKER_ID_PREFIX}{worker_id}"
                        )
                        if t != "idle":
                            logger.info("Task processed: %s", t)
                    if run_once:
                        return
                    try:
                        await asyncio.wait_for(
                            local_shutdown.wait(),
                            timeout=poll_interval,
                        )
                        return  # woke up from signal
                    except TimeoutError:
                        pass
                elif outcome == RunOutcome.completed:
                    logger.info("Turn completed successfully")
                    if run_once:
                        return
                elif outcome == RunOutcome.failed:
                    logger.warning("Turn execution failed")
                elif outcome == RunOutcome.lost:
                    logger.warning("Turn lease lost")
                elif outcome == RunOutcome.cancelled:
                    logger.info("Turn was cancelled")
                else:
                    logger.debug("Worker outcome: %s", outcome)
        except asyncio.CancelledError:
            pass

        logger.info("Worker loop stopped.")

    # ── lifecycle ──────────────────────────────────────────────────────────

    def close(self) -> None:
        """幂等关闭：关闭 SQLite 连接；多次调用不抛异常。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.close()
            logger.info("Application closed.")
        except Exception as e:
            logger.warning("Error closing application: %s", e)


# 向后兼容保留（避免外部引用需要同步改）
__all__ = ["RuntimeApplication"]
