"""RuntimeApplication — composition root for runtime lifecycle.

Single source of truth for:
- SQLite connection ownership
- Migration and recovery-all at startup (RB-06)
- Provider / Registry / Executor / AgentRunner / InboundService assembly
- Outbox / Delivery / Channel 生命周期
- In-process interactive terminal entrypoint and background worker entrypoints
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
from cogito.model.llm_manager import LLMManager
from cogito.model.provider import ModelProvider
from cogito.service.agent_runner import RunOutcome, build_agent_runner
from cogito.service.inbound_service import InboundService
from cogito.service.recovery_service import RecoveryService
from cogito.service.task_handlers import TaskHandlerContext, _build_registry
from cogito.store.connection import get_connection
from cogito.store.migration import migrate

# ── Plan 06 M2: 配置版本持久化辅助 ──


def _persist_config_version(
    conn: sqlite3.Connection, config: Config
) -> None:
    """启动时记录配置版本（幂等：同一 content_hash 不重复插入）。"""
    try:
        from datetime import datetime

        from cogito.contracts.clock import epoch_ms
        from cogito.store.config_version_repo import (
            ConfigVersionRecord,
            ConfigVersionRepository,
        )

        cfg_repo = ConfigVersionRepository(conn)
        if cfg_repo.get_by_hash(config.content_hash) is not None:
            return
        cfg_repo.insert(ConfigVersionRecord(
            version_id=f"cfg-{uuid.uuid4().hex[:12]}",
            content_hash=config.content_hash,
            schema_version=config.schema_version,
            source_layers=["profile"],
            applied_at=epoch_ms(datetime.now()),
            applied_by="startup",
        ))
        conn.commit()
    except Exception as e:
        logger.warning("Config version persistence failed: %s", e)

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
        llm_manager: LLMManager | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.provider = provider
        self.runner = runner
        self.inbound = inbound
        self.llm_manager = llm_manager
        self.vision_service: Any = None
        self.vision_service_factory: Any = None
        self._terminal_seq = 0
        self._instance_id = uuid.uuid4().hex
        self._closed = False
        self._ready = False
        self._recovery_counts: dict[str, int] = {}

        # MCP Manager —— run_worker() 填充；提前声明避免 close() 属性缺失
        self.mcp_manager: Any = None

        # PR 2: 后台组件（start_background() 创建）
        self.outbox_worker: Any = None
        self.delivery_worker: Any = None
        self.task_worker: Any = None
        self.channel_manager: Any = None
        self.channel_gateway: Any = None
        self.local_gateway_client: Any = None
        self.gateway_client: Any = None
        self.delivery_service: Any = None
        self.plugin_runtime: Any = None

        # Web Dashboard 自带的内置 Channel（浏览器 WebSocket）
        self.web_channel_adapter: Any = None

        # Scheduler —— 周期触发到期 schedule
        self.scheduler: Any = None

        # PR 2: 关闭/drain 状态
        self._shutdown_event: asyncio.Event | None = None
        self._drain_timeout: float = 10.0

        # 入站唤醒事件：inbound.accept 入队新 Turn 后置位，worker 据此即时唤醒
        self._wakeup_event: asyncio.Event | None = None

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

        # Plan 06 M2: 持久化配置版本（启动时记录，供 Attempt/Task 追溯）
        _persist_config_version(conn, config)

        try:
            recovery = RecoveryService(conn)
            recovery_counts = recovery.recover_all()
            logger.info(
                "Startup recovery → outbox=%d delivery=%d stale_turns=%d streaming=%d",
                recovery_counts.get("outbox_leases", 0),
                recovery_counts.get("delivery_leases", 0),
                recovery_counts.get("stale_turns", 0),
                recovery_counts.get("streaming_deliveries", 0),
            )
        except Exception:
            conn.close()
            raise

        from cogito.contracts.memory import MemoryReader, MemoryWriter
        from cogito.service.asset_service import AssetIngestionService
        from cogito.service.memory_service import SqliteMemoryService
        from cogito.service.unit_of_work import make_unit_of_work_memory_writer
        from cogito.service.vision_service import (
            MultimodalContextProjection,
            VisionAnalysisService,
        )
        from cogito.store.memory_repo import MemoryRepository
        from cogito.tools.registry import assemble_default_registry

        llm_manager = LLMManager.build(config.model)
        provider = llm_manager.get("main")
        if config.model.provider == "echo":
            logger.info("Using echo provider — user messages will be echoed back")
            print("[echo] 使用回显 Provider（用户消息原样返回，不调用真实模型）")
        elif config.model.main.is_configured():
            logger.info(
                "Using model: %s (%s)",
                config.model.main.model,
                config.model.main.base_url,
            )
        else:
            logger.warning("No model configured — using stub provider")
            print("[stub] 未配置模型，使用 Stub Provider（固定回复）")

        # ── PLAN-09 M4a/C2 破环：注册表在组合根预装配（service→tools 切断）──
        memory_service = SqliteMemoryService(conn=conn)

        def _make_memory_writer() -> MemoryWriter:
            return make_unit_of_work_memory_writer(config.resolve_db_path())

        def _make_memory_reader() -> MemoryReader:
            from cogito.store.connection import get_connection as _gc
            reader_conn = _gc(config.resolve_db_path())
            return SqliteMemoryService(repo=MemoryRepository(reader_conn))

        vision_model_id = config.model.resolve_role("vlm")[1].model or "vlm"

        def _make_vision_service() -> VisionAnalysisService:
            from cogito.store.connection import get_connection as _gc

            vision_conn = _gc(config.resolve_db_path())
            return VisionAnalysisService(
                vision_conn,
                config.resolve_payload_dir(),
                llm_manager.router,
                config.multimodal,
                model_id=vision_model_id,
            )

        shared_vision_service = None
        multimodal_reader = None
        asset_service = None
        vision_factory = None
        if config.multimodal.enabled:
            shared_vision_service = VisionAnalysisService(
                conn,
                config.resolve_payload_dir(),
                llm_manager.router,
                config.multimodal,
                model_id=vision_model_id,
            )
            multimodal_reader = MultimodalContextProjection(
                conn,
                model_id=vision_model_id,
                config=config.multimodal,
            )
            asset_service = AssetIngestionService(
                conn,
                config.resolve_payload_dir(),
                config.multimodal,
            )
            vision_factory = _make_vision_service

        pre_assembled_registry = assemble_default_registry(
            memory_reader=memory_service,
            memory_writer=memory_service,
            make_memory_writer=_make_memory_writer,
            make_memory_reader=_make_memory_reader,
            make_vision_service=vision_factory,
        )

        runner = build_agent_runner(
            config=config,
            connection=conn,
            provider=provider,
            llm_manager=llm_manager,
            registry=pre_assembled_registry,
            memory_service=memory_service,
            streaming_enabled=config.agent.streaming_enabled,
            vision_service=shared_vision_service,
            multimodal_reader=multimodal_reader,
        )

        # 唤醒事件：入站新建 Turn 时置位，worker idle 睡眠改为等待该事件，
        # 消除最长 heartbeat_interval_seconds 的轮询延迟。
        wakeup_event = asyncio.Event()
        inbound = InboundService(
            conn,
            notify=wakeup_event.set,
            asset_service=asset_service,
            vision_service=shared_vision_service,
            max_assets_per_message=config.multimodal.max_assets_per_message,
        )

        app = cls(
            config=config,
            conn=conn,
            provider=provider,
            runner=runner,
            inbound=inbound,
            llm_manager=llm_manager,
        )
        app.vision_service = shared_vision_service
        app.vision_service_factory = vision_factory
        app._wakeup_event = wakeup_event
        app._recovery_counts = recovery_counts

        app.build_plugin_runtime()
        # 构建 Channel 组件（Manager + Gateway），QQ 等渠道在 run_worker 中启动
        app.build_channel_components()
        # Plan 05 M4：把 Channel 组件注入 AgentRunner，使其能走流式投递分支
        app.runner.channel_gateway = app.channel_gateway
        app.runner.channel_manager = app.channel_manager

        return app

    def build_plugin_runtime(self) -> None:
        """Build the unique Plugin Runtime and optionally discover/start plugins."""
        from cogito.capability.plugin_runtime import SqlitePluginRuntime

        cfg = self.config.capability.plugins
        self.plugin_runtime = SqlitePluginRuntime(
            self.conn,
            builtin_paths=cfg.builtin_paths,
            granted_permissions=set(cfg.granted_permissions),
        )
        if not cfg.enabled:
            return
        for manifest in self.plugin_runtime.discover(*cfg.project_paths):
            state = self.plugin_runtime.get(manifest.plugin_id)
            if state is None:
                state = self.plugin_runtime.install(manifest)
            if not cfg.auto_start:
                continue
            if state.status in ("installed", "configured", "disabled", "degraded", "stopped"):
                state = self.plugin_runtime.enable(manifest.plugin_id) or state
            if state.status == "enabled":
                self.plugin_runtime.start(manifest.plugin_id)

    # ── PR 2: Channel / Gateway 组装 ───────────────────────────────────────

    def build_channel_components(self) -> None:
        """构建 ChannelManager、ChannelGateway（不启动 Adapter）。

        RuntimeApplication 完整拥有这两个组件的生命周期。
        """
        from cogito.channel.manager import ChannelManager
        from cogito.inbound.dispatcher import InboundDispatcher
        from cogito.service.channel_gateway import ChannelGateway
        from cogito.service.http_gateway_client import HttpGatewayClient
        from cogito.service.loopback_gateway_client import LoopbackGatewayClient

        inbound_dispatcher = InboundDispatcher(self.inbound)
        self.channel_manager = ChannelManager(inbound_dispatcher)
        self.channel_gateway = ChannelGateway(self.conn, self.channel_manager)
        self.local_gateway_client = LoopbackGatewayClient(self.channel_gateway)
        gateway_url = self.config.channel.gateway_url.strip()
        self.gateway_client = (
            HttpGatewayClient(gateway_url) if gateway_url else self.local_gateway_client
        )

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

    async def start_web_channel(self) -> None:
        """启动 Web Dashboard 内置 Channel（浏览器 WebSocket 渠道）。

        该 adapter 始终随 serve 启动（不依赖外部平台），并注册到 ChannelManager，
        使 Agent 回复可经 ChannelGateway 路由回来。Web 服务（chat.py）通过
        ``runtime.web_channel_adapter`` 订阅/取消订阅会话队列。
        """
        if self.web_channel_adapter is not None:
            return
        from cogito.channel.drivers.web import WebChannelAdapter

        adapter = WebChannelAdapter(adapter_id="web", channel_type="web", conn=self.conn)
        await self.channel_manager.start_adapter("web", adapter)
        self.web_channel_adapter = adapter
        logger.info("Web channel adapter started (conversation-bound WebSocket queue)")

    def build_workers(self) -> None:
        """构建 OutboxWorker / DeliveryWorker / TaskWorker / EventConsumers。"""
        from cogito.service.event_consumers import build_default_registry
        from cogito.service.outbox_worker import OutboxWorker
        from cogito.service.sqlite_delivery_service import SqliteDeliveryService

        self.outbox_worker = OutboxWorker(
            self.conn,
            lease_ttl_s=self.config.worker.outbox_lease_ttl_seconds,
        )
        self.event_consumer_registry = build_default_registry()
        if self.channel_gateway is None:
            self.build_channel_components()
        elif self.gateway_client is None:
            from cogito.service.loopback_gateway_client import LoopbackGatewayClient
            self.local_gateway_client = LoopbackGatewayClient(self.channel_gateway)
            self.gateway_client = self.local_gateway_client
        self.delivery_service = SqliteDeliveryService(
            conn=self.conn,
            gateway=self.gateway_client,
            lease_ttl_s=self.config.worker.delivery_lease_ttl_seconds,
        )
        self.delivery_worker = self.delivery_service.worker()

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

        # N Outbox —— lease_next → consumer dispatch → publish/retry
        for _ in range(outbox_batch):
            lease = self.outbox_worker.lease_next(worker_id)
            if lease is None:
                break
            consumer = self.event_consumer_registry.find(lease)
            if consumer is not None:
                # Consumer 内部负责幂等+事务；失败则 retry/dead_letter，不 publish
                try:
                    ok = await asyncio.to_thread(
                        consumer.handle, self.conn, lease,
                    )
                    if ok:
                        self.outbox_worker.publish(lease, worker_id)
                        result.outbox += 1
                        result.idle = False
                    else:
                        self.outbox_worker.retry(lease, worker_id)
                except Exception:
                    logger.exception(
                        "outbox consumer failed: event=%s consumer=%s",
                        lease.event_id, consumer.name,
                    )
                    self.outbox_worker.retry(lease, worker_id)
            else:
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

        # Proactive evaluate tick —— 仅当 config.capability.proactive.enabled=true
        proactive_cfg = getattr(self.config.capability, "proactive", None)
        if proactive_cfg is not None and proactive_cfg.enabled and self.scheduler is not None:
            try:
                eval_tasks = await asyncio.to_thread(self.scheduler.tick_proactive_evaluate)
                if eval_tasks:
                    result.scheduler += len(eval_tasks)
                    result.idle = False
            except Exception:
                logger.warning("Proactive evaluate tick failed", exc_info=True)

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
        from cogito.service.task_worker import TaskWorker

        self._shutdown_event = shutdown_event or asyncio.Event()

        # 创建 workers（Outbox / Delivery / Task）
        self.build_workers()

        # 构建 MCP Manager（生命周期由 Runtime 拥有 —— M2）
        from cogito.capability.mcp.manager import MCPServerManager
        from cogito.service.agent_runner import start_mcp_servers

        mcp_registry = self.runner._registry if self.runner else None
        self.mcp_manager = None
        if mcp_registry is not None:
            self.mcp_manager = MCPServerManager(mcp_registry)
            try:
                self.mcp_manager = await start_mcp_servers(self.config, mcp_registry)
            except Exception:
                logger.warning("MCP Manager startup partially failed (non-fatal)",
                               exc_info=True)
                self.mcp_manager = MCPServerManager(mcp_registry)

        # 创建 Scheduler
        from cogito.service.scheduler import Scheduler

        self.scheduler = Scheduler(self.conn)

        # 启动启用的 Channel Adapter（如 QQ）
        await self._start_enabled_channels()

        task_handler_ctx = TaskHandlerContext(
            connection_factory=lambda p=self.config.resolve_db_path(): get_connection(p),
            model_router=self.llm_manager.router if self.llm_manager else None,
            vision_service_factory=self.vision_service_factory,
            workspace_path=self.config.workspace_path,
            mcp_manager=self.mcp_manager,
            delivery_service=self.delivery_service,
            proactive_config=self.config.capability.proactive,
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
                try:
                    cycle = await self.process_background_once(worker_id)
                except Exception:
                    # 单轮失败不应杀死 worker —— 记录后继续，避免后续 Turn 全部静默。
                    logger.exception("process_background_once failed (worker=%s)", worker_id)
                    await asyncio.sleep(min(poll_interval, 1.0))
                    continue
                if cycle.idle:
                    if run_once:
                        return
                    # 等待 shutdown 或「新 Turn 入队」唤醒事件；超时则回退轮询。
                    # 唤醒事件置位后清空，避免 worker 在处理间隙空转。
                    if self._wakeup_event is not None:
                        self._wakeup_event.clear()
                    # Python 3.11+: asyncio.wait 要求传 Task 而非 coroutine。
                    waiters = [
                        asyncio.ensure_future(e.wait())
                        for e in (self._shutdown_event, self._wakeup_event)
                        if e is not None
                    ]
                    try:
                        await asyncio.wait(
                            waiters,
                            timeout=poll_interval,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except (TimeoutError, ValueError, TypeError):
                        pass
                    for w in waiters:
                        if not w.done():
                            w.cancel()
                    if self._wakeup_event is not None and self._wakeup_event.is_set():
                        self._wakeup_event.clear()
                    if self._shutdown_event.is_set():
                        return  # woke up from signal
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
        """幂等关闭：关闭 SQLite 连接 + MCP Manager；多次调用不抛异常。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self.mcp_manager is not None:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    loop.create_task(self.mcp_manager.stop_all())
                else:
                    asyncio.run(self.mcp_manager.stop_all())
        except Exception as e:
            logger.warning("Error stopping MCP manager: %s", e)
        try:
            if self.plugin_runtime is not None:
                self.plugin_runtime.close()
        except Exception as e:
            logger.warning("Error closing plugin runtime: %s", e)
        try:
            if self.gateway_client is not None and hasattr(self.gateway_client, "close"):
                self.gateway_client.close()
        except Exception as e:
            logger.warning("Error closing gateway client: %s", e)
        try:
            self.conn.close()
            logger.info("Application closed.")
        except Exception as e:
            logger.warning("Error closing application: %s", e)


# 向后兼容保留（避免外部引用需要同步改）
__all__ = ["RuntimeApplication", "RuntimeCycleResult"]
