"""RuntimeApplication — composition root for runtime lifecycle.

Single source of truth for:
- SQLite connection ownership
- Migration and recovery-all at startup (RB-06)
- Provider / Registry / Executor / AgentRunner / InboundService assembly
- Outbox / Delivery / Channel 生命周期
- Terminal interactive REPL and background worker entrypoints
- Idempotent close() and graceful async shutdown()

Plan 02 / `RUNNABLE-BASELINE-01` — 统一装配，避免 worker vs interactive 漂移 (RB-07)。
QQ-ONEBOT-E2E-01 / PR 2 — Runtime 完整拥有 Outbox、Delivery、Channel 生命周期。
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import uuid
from dataclasses import dataclass
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


@dataclass
class RuntimeCycleResult:
    """一轮公平轮询的处理结果 —— 便于测试、日志和以后健康检查。"""
    turn: int = 0
    outbox: int = 0
    delivery: int = 0
    task: int = 0
    scheduler: int = 0
    idle: bool = True


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
        self._ready = False
        self._recovery_counts: dict[str, int] = {}

        # PR 2: 后台组件（start_background() 创建）
        self.outbox_worker: Any = None
        self.delivery_worker: Any = None
        self.task_worker: Any = None
        self.channel_manager: Any = None
        self.channel_gateway: Any = None

        # Scheduler —— 周期触发到期 schedule
        self.scheduler: Any = None

        # PR 2: 关闭/drain 状态
        self._shutdown_event: asyncio.Event | None = None
        self._drain_timeout: float = 10.0

    # ── factory ────────────────────────────────────────────────────────────

    @classmethod
    def build(cls, config: Config) -> RuntimeApplication:
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

        # 构建 Channel 组件（Manager + Gateway），QQ 等渠道在 run_worker 中启动
        app.build_channel_components()

        return app

    # ── PR 2: Channel / Gateway 组装 ───────────────────────────────────────

    def build_channel_components(self) -> None:
        """构建 ChannelManager、ChannelGateway（不启动 Adapter）。

        RuntimeApplication 完整拥有这两个组件的生命周期。
        """
        from cogito.channel.manager import ChannelManager
        from cogito.inbound.dispatcher import InboundDispatcher
        from cogito.service.channel_gateway import ChannelGateway

        inbound_dispatcher = InboundDispatcher(self.inbound)
        self.channel_manager = ChannelManager(inbound_dispatcher)
        self.channel_gateway = ChannelGateway(self.conn, self.channel_manager)

    async def _start_enabled_channels(self) -> None:
        """启动配置中 enabled 的 Channel Adapter。

        目前只处理 QQ OneBot；其他渠道未来扩展。
        QQ 明确 enabled 时启动失败 → 应用 readiness 失败并退出。
        """
        if not self.config.channel.qq.enabled:
            return
        from cogito.channel.drivers.qq_onebot import QQOneBotAdapter

        adapter = QQOneBotAdapter(self.config.channel.qq)
        try:
            await self.channel_manager.start_adapter("qq", adapter)
            logger.info("QQ channel adapter started (instance=%s)", adapter.adapter_id)
        except Exception:
            logger.exception("Failed to start QQ channel adapter")
            raise

    def build_workers(self) -> None:
        """构建 OutboxWorker / DeliveryWorker / TaskWorker。"""
        from cogito.service.delivery_worker import DeliveryWorker
        from cogito.service.outbox_worker import OutboxWorker

        self.outbox_worker = OutboxWorker(
            self.conn,
            lease_ttl_s=self.config.worker.outbox_lease_ttl_seconds,
        )
        if self.channel_gateway is None:
            self.build_channel_components()
        self.delivery_worker = DeliveryWorker(
            conn=self.conn,
            gateway=self.channel_gateway,
            lease_ttl_s=self.config.worker.delivery_lease_ttl_seconds,
        )

    # ── read-only accessors ────────────────────────────────────────────────

    def recovery_counts(self) -> dict[str, int]:
        return dict(self._recovery_counts)

    @property
    def ready(self) -> bool:
        return self._ready

    # ── terminal interactive ───────────────────────────────────────────────

    def _next_terminal_message_id(self) -> str:
        """稳定的本进程消息 ID，避免幂等键冲突（RB-06 / 入站事务）。"""
        seq = self._terminal_seq
        self._terminal_seq += 1
        return f"terminal:{self._instance_id}:{seq}"

    async def process_terminal_message(self, text: str) -> str:
        """注入一条 terminal 消息、执行一轮 Turn、返回 assistant 回复文本。"""
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
        logger.warning("Unexpected terminal outcome: %s (turn_id=%s)", outcome, result.turn_id)
        return f"(unexpected outcome: {outcome})"

    # ── PR 2: 公平轮询 ───────────────────────────────────────────────────

    async def process_background_once(
        self,
        worker_id: str,
        *,
        outbox_batch: int = 10,
        delivery_batch: int = 10,
        task_batch: int = 5,
    ) -> RuntimeCycleResult:
        """一轮公平轮询 —— 分别有限批次处理各队列。

        只有所有队列都 idle 时才在 run_worker() 中外层 idle sleep。
        Turn 完成后立即尝试 Delivery，不等到下一次长期 idle。
        """
        result = RuntimeCycleResult()

        # 1 Turn
        if self.outbox_worker is None:
            # 未创建 workers（兼容旧路径）
            outcome = await self.runner.run_once(worker_id)
            if outcome == RunOutcome.completed:
                result.turn = 1
                result.idle = False
            return result

        outcome = await self.runner.run_once(worker_id)
        if outcome == RunOutcome.completed:
            result.turn = 1
            result.idle = False
        elif outcome == RunOutcome.failed:
            logger.warning("Turn execution failed")
        elif outcome == RunOutcome.lost:
            logger.warning("Turn lease lost")
        elif outcome == RunOutcome.cancelled:
            logger.info("Turn was cancelled")

        # N Outbox
        for _ in range(outbox_batch):
            lease = self.outbox_worker.lease_next(worker_id)
            if lease is None:
                break
            self.outbox_worker.publish(lease, worker_id)
            result.outbox += 1
            result.idle = False

        # N Delivery —— 通过 to_thread 调用同步 deliver()，避免阻塞主 loop
        for _ in range(delivery_batch):
            lease = self.delivery_worker.lease_next(worker_id)
            if lease is None:
                break
            await asyncio.to_thread(self.delivery_worker.deliver, lease, worker_id)
            result.delivery += 1
            result.idle = False

        # N Task
        for _ in range(task_batch):
            t = await self.task_worker.run_once(f"task-{worker_id}")
            if t == "idle":
                break
            result.task += 1
            result.idle = False

        # Scheduler tick —— 周期触发到期 schedule，生成 connector.poll Task
        if self.scheduler is not None:
            try:
                scheduled = await asyncio.to_thread(self.scheduler.tick)
                if scheduled:
                    result.scheduler = len(scheduled)
                    result.idle = False
            except Exception:
                logger.warning("Scheduler tick failed", exc_info=True)

        return result

    # ── worker entrypoint ──────────────────────────────────────────────────

    async def run_worker(
        self,
        worker_id: str,
        poll_interval: float,
        *,
        run_once: bool = False,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        """Worker 循环：公平轮询 Turn → Outbox → Delivery → Task。"""
        from cogito.service.task_dispatcher import TaskDispatcher
        from cogito.service.task_handlers import TaskHandlerContext, _build_registry
        from cogito.service.task_worker import TaskWorker

        self._shutdown_event = shutdown_event or asyncio.Event()

        # 创建 workers（Outbox / Delivery / Task）
        self.build_workers()

        # 创建 Scheduler
        from cogito.service.scheduler import Scheduler

        self.scheduler = Scheduler(self.conn)

        # 启动启用的 Channel Adapter（如 QQ）
        await self._start_enabled_channels()

        task_handler_ctx = TaskHandlerContext(
            connection_factory=lambda p=self.config.resolve_db_path(): get_connection(p),
            workspace_path=self.config.workspace_path,
        )
        task_registry = _build_registry(task_handler_ctx)
        task_dispatcher = TaskDispatcher(self.conn)
        self.task_worker = TaskWorker(
            conn=self.conn,
            dispatcher=task_dispatcher,
            registry=task_registry,
            handler_context=task_handler_ctx,
            heartbeat_interval_s=self.config.worker.heartbeat_interval_seconds,
        )

        def _handle_signal() -> None:
            logger.info("Shutdown signal received, stopping...")
            self._shutdown_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except (ValueError, NotImplementedError):
                pass  # Windows: no add_signal_handler

        # 标记 ready
        self._ready = True
        logger.info("Starting worker loop (poll interval: %.1fs)...", poll_interval)
        print("[ok] Agent is running. Press Ctrl+C to stop.")

        try:
            while not self._shutdown_event.is_set():
                cycle = await self.process_background_once(worker_id)
                if cycle.idle:
                    if run_once:
                        return
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=poll_interval,
                        )
                        return  # woke up from signal
                    except TimeoutError:
                        pass
                else:
                    logger.debug(
                        "Cycle processed: turn=%d outbox=%d delivery=%d task=%d",
                        cycle.turn, cycle.outbox, cycle.delivery, cycle.task,
                    )
                    if run_once:
                        return
        except asyncio.CancelledError:
            pass

        logger.info("Worker loop stopped.")

    # ── PR 2: shutdown / drain ────────────────────────────────────────────

    async def shutdown(self) -> None:
        """优雅关闭 —— 停止新工作、drain 当前 Attempt、释放端口。

        正常关闭路径必须 await shutdown()。
        只有关闭 SQLite 的底层兜底用同步 close()。
        """
        if self._closed:
            return
        logger.info("Shutdown requested...")

        # 1. 标记不再 ready
        self._ready = False

        # 2. 停止领取新 Lease
        if self._shutdown_event is not None:
            self._shutdown_event.set()

        # 3. 停止 Channel（释放端口）
        if self.channel_manager is not None:
            try:
                await asyncio.wait_for(
                    self.channel_manager.stop_all(),
                    timeout=self._drain_timeout,
                )
            except Exception as e:
                logger.warning("Error stopping channels: %s", e)

        # 4. 关闭 SQLite
        self.close()
        logger.info("Shutdown complete.")

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
__all__ = ["RuntimeApplication", "RuntimeCycleResult"]
