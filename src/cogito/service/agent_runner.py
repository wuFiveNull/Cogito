"""AgentRunner — Turn 完整执行入口。

Plan 01 / 十、AgentRunner：
1. Dispatcher.claim_next → 领取 Turn（事务内）
2. ContextBuilder.build → 构建上下文
3. AgentLoop.run → 调用模型（事务外，带 heartbeat）
4. TurnCompletionService.complete_reply → 原子完成（事务内）

规则：
- claim_next() 事务结束后才能调用模型。
- 模型网络调用期间不持有数据库事务。
- 调用前后检查取消和 Lease。
- 模型成功但提交失败时，不能返回 completed。
- AgentRunner 不直接拼 SQL。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from enum import StrEnum

_LOGGER = logging.getLogger("cogito.agent_runner")

from cogito.capability import CapabilityRegistry
from cogito.capability.executor import ToolExecutor
from cogito.config import Config, ModelConfig
from cogito.model.provider import ModelProvider
from cogito.model.router import ModelRouter
from cogito.runtime.clock import Clock, ProductionClock
from cogito.runtime.context import ContextBuilder
from cogito.runtime.loop import AgentLoop, LoopResultType
from cogito.service.completion import TurnCompletionService
from cogito.service.dispatcher import Dispatcher
from cogito.store.time_utils import epoch_ms

# ── 默认模式-Toolset 映射 (AGENT-COGNITION / 2.2) ──

MODE_TOOLSETS: dict[str, set[str]] = {
    "reactive": {"core", "memory", "terminal", "search", "disk"},
    "proactive": {"core", "memory", "message"},
    "scheduled": {"core", "memory", "schedule"},
    "maintenance": {"core", "memory", "disk"},
}


class RunOutcome(StrEnum):
    """AgentRunner.run_once 的执行结果。"""
    idle = "idle"                    # 无可用 Turn
    completed = "completed"          # 成功完成
    failed = "failed"                # 模型或提交失败
    lost = "lost"                    # Lease 失效或取消
    cancelled = "cancelled"          # 被外部取消


class AgentRunner:
    """Turn 执行器 —— 领取、构建、推理、完成。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        router: ModelRouter,
        clock: Clock | None = None,
        model_role: str = "main",
        heartbeat_interval_s: int = 30,
        max_input_tokens: int = 64000,
        system_prompt: str = "You are Cogito, a helpful AI assistant.",
        context_memory_window: int = 50,
        registry: CapabilityRegistry | None = None,
        executor: ToolExecutor | None = None,
        toolsets: set[str] | None = None,
    ) -> None:
        self._conn = conn
        self._router = router
        self._model_role = model_role
        self._clock = clock or ProductionClock()
        self._heartbeat_interval_s = heartbeat_interval_s
        self._system_prompt = system_prompt
        self._context_memory_window = context_memory_window
        self._registry = registry
        self._executor = executor
        self._toolsets = toolsets or set()

        self._dispatcher = Dispatcher(conn, clock=self._clock)
        self._context_builder = ContextBuilder(
            conn, clock=self._clock, max_input_tokens=max_input_tokens,
        )
        self._loop = AgentLoop(
            router,
            registry=registry,
            executor=executor,
            toolsets=toolsets,
        )
        self._completion = TurnCompletionService(conn, clock=self._clock)

    async def run_once(self, worker_id: str, cancel_flag: callable | None = None) -> RunOutcome:
        """领取一个 Turn 并执行完成。

        流程：
        1. claim_next（事务内）
        2. build（短暂读库）
        3. AgentLoop.run（事务外，网络调用）
        4. 重验证 Lease
        5. complete_reply（事务内）
        """
        # ── 1. 领取 Turn（事务内）──
        claimed = self._dispatcher.claim_next(worker_id, clock=self._clock.now())
        if claimed is None:
            return RunOutcome.idle

        turn = claimed.turn
        attempt = claimed.attempt

        # ── 检查即将开始前的取消状态 ──
        if self._is_cancelled(turn.turn_id):
            return RunOutcome.cancelled

        # ── 2. 构建 Context（短暂读库，不持网络锁）──
        context = self._context_builder.build(
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            input_message_id=turn.input_message_id,
            system_policy=self._system_prompt,
        )

        # ── 3. 执行 Agent Loop（事务外，网络调用）──
        try:
            loop_result = await self._run_loop_with_heartbeat(
                turn, attempt, worker_id, context,
            )
        except Exception as e:
            _LOGGER.exception("AgentLoop.run() threw: %s", e)
            self._fail_safe(turn, attempt)
            return RunOutcome.failed

        # ── 检查 Loop 是否成功 ──
        if not loop_result.is_success:
            _LOGGER.warning(
                "Loop did not succeed: type=%s error=%s text=%s",
                loop_result.result_type,
                loop_result.error_message,
                loop_result.text[:200] if loop_result.text else "(empty)",
            )
            if loop_result.result_type == LoopResultType.cancelled:
                return RunOutcome.cancelled
            if loop_result.error_message:
                _LOGGER.error("Loop failed: %s", loop_result.error_message)
            self._fail_safe(turn, attempt)
            return RunOutcome.failed

        # ── 4. 完成前检查取消和 Lease ──
        if self._is_cancelled(turn.turn_id):
            return RunOutcome.cancelled

        if not self._is_lease_valid(
            turn.turn_id, attempt.attempt_id,
            attempt.worker_id, attempt.lease_version,
        ):
            return RunOutcome.lost

        # ── 5. 写入结果（事务内）──
        try:
            message_id = self._completion.complete_reply(
                turn=turn,
                attempt=attempt,
                reply_text=loop_result.text,
            )
            if message_id is None:
                return RunOutcome.failed
            return RunOutcome.completed
        except Exception as e:
            _LOGGER.exception("complete_reply failed: %s", e)
            self._fail_safe(turn, attempt)
            return RunOutcome.failed

    async def _run_loop_with_heartbeat(
        self, turn, attempt, worker_id: str, context,
    ):
        """运行 Agent Loop，同时保持 heartbeat。"""
        cancel_check = self._make_cancel_check(turn.turn_id)

        loop_task = asyncio.create_task(
            self._loop.run(
                context,
                model_role=self._model_role,
                cancel_flag=cancel_check,
            )
        )

        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                turn.turn_id, attempt.attempt_id,
                worker_id, attempt.lease_version,
            )
        )

        done, _ = await asyncio.wait(
            [loop_task, heartbeat_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        heartbeat_task.cancel()
        return loop_task.result()

    async def _heartbeat_loop(
        self, turn_id: str, attempt_id: str, worker_id: str, lease_version: int,
    ) -> None:
        """定期发送 heartbeat 防止 Lease 过期。"""
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            try:
                ok = self._dispatcher.heartbeat(
                    turn_id, attempt_id, worker_id, lease_version,
                    clock=self._clock.now(),
                )
                if not ok:
                    return
            except Exception:
                return

    def _make_cancel_check(self, turn_id: str):
        """创建取消检查闭包。"""
        cancelled = False

        def check() -> bool:
            nonlocal cancelled
            if cancelled:
                return True
            row = self._conn.execute(
                "SELECT cancel_requested_at FROM turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if row and row["cancel_requested_at"] is not None:
                cancelled = True
                return True
            return False

        return check

    def _is_cancelled(self, turn_id: str) -> bool:
        """检查 Turn 是否已被取消。"""
        row = self._conn.execute(
            "SELECT cancel_requested_at FROM turns WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        return bool(row and row["cancel_requested_at"] is not None)

    def _is_lease_valid(
        self, turn_id: str, attempt_id: str,
        worker_id: str, lease_version: int,
    ) -> bool:
        """验证 Lease 有效性（不修改状态）。"""
        row = self._conn.execute(
            "SELECT 1 FROM run_attempts "
            "WHERE attempt_id=? AND turn_id=? "
            "AND status='running' AND worker_id=? AND lease_version=? "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (attempt_id, turn_id, worker_id, lease_version,
             epoch_ms(self._clock.now())),
        ).fetchone()
        return row is not None

    def _fail_safe(self, turn, attempt) -> None:
        """安全地标记 Attempt 失败，不抛出。"""
        try:
            self._dispatcher.fail(
                turn.turn_id, attempt.attempt_id,
                turn.version,
                worker_id=attempt.worker_id,
                lease_version=attempt.lease_version,
                clock=self._clock.now(),
            )
        except Exception:
            pass


# =============================================================================
# 组装入口
# =============================================================================


def build_agent_runner(
    config: Config,
    connection: sqlite3.Connection,
    provider: ModelProvider | None = None,
    clock: Clock | None = None,
    registry: CapabilityRegistry | None = None,
    toolsets: set[str] | None = None,
) -> AgentRunner:
    """构建 AgentRunner。

    Args:
        config: 系统配置。
        connection: SQLite 连接。
        provider: 可选的 ModelProvider。
        clock: 可选的时钟实现。
        registry: 可选的 CapabilityRegistry。未传时自动创建并发现内置工具。
        toolsets: 启用的 Toolset。未传时使用 reactive 默认。

    Returns: 配置好的 AgentRunner 实例。
    """
    resolved_clock = clock or ProductionClock()

    # 创建或使用 Provider
    if provider is None:
        provider = _create_provider(config.model)

    # 创建 Router
    router = ModelRouter(
        providers={"main": provider},
        role_map={"main": "main"},
    )

    # 创建 Registry 并发现内置工具
    resolved_registry = registry
    if resolved_registry is None:
        from cogito.capability import CapabilityRegistry
        from cogito.service.memory_service import SqliteMemoryService
        from cogito.tools.registry import discover_builtin_tools

        # 创建 MemoryService 供记忆工具使用
        memory_service = SqliteMemoryService(conn=connection)

        resolved_registry = CapabilityRegistry()
        discover_builtin_tools(resolved_registry, memory_service=memory_service)

    # 创建 Executor
    executor = ToolExecutor(resolved_registry)

    # 解析 Toolset（默认 reactive）
    resolved_toolsets = toolsets
    if resolved_toolsets is None:
        agent_mode = config.agent.mode if hasattr(config.agent, 'mode') else "reactive"
        resolved_toolsets = MODE_TOOLSETS.get(agent_mode, {"core"})

    # 配置覆盖
    if config.agent.enabled_toolsets:
        resolved_toolsets = set(config.agent.enabled_toolsets)
    if config.agent.disabled_toolsets:
        resolved_toolsets -= set(config.agent.disabled_toolsets)

    return AgentRunner(
        conn=connection,
        router=router,
        clock=resolved_clock,
        model_role="main",
        heartbeat_interval_s=config.worker.heartbeat_interval_seconds,
        max_input_tokens=config.agent.max_output_tokens * 8,
        system_prompt=config.agent.system_prompt,
        context_memory_window=config.agent.context_memory_window,
        registry=resolved_registry,
        executor=executor,
        toolsets=resolved_toolsets,
    )


async def build_and_start_agent_runner(
    config: Config,
    connection: sqlite3.Connection,
    provider: ModelProvider | None = None,
    clock: Clock | None = None,
    registry: CapabilityRegistry | None = None,
    toolsets: set[str] | None = None,
) -> AgentRunner:
    """异步构建 AgentRunner 并启动 MCP Server。

    在 build_agent_runner 基础上追加 MCP 服务器启动。
    """
    runner = build_agent_runner(
        config=config,
        connection=connection,
        provider=provider,
        clock=clock,
        registry=registry,
        toolsets=toolsets,
    )

    # 启动 MCP Server
    if config.capability.mcp_servers:
        try:
            from cogito.capability.mcp.manager import MCPServerManager

            manager = MCPServerManager(runner._registry)
            for entry in config.capability.mcp_servers:
                if not entry.enabled:
                    continue
                from cogito.capability.mcp import MCPServerConfig

                mcp_cfg = MCPServerConfig(
                    name=entry.name,
                    transport=entry.transport,
                    command=entry.command,
                    args=entry.args,
                    url=entry.url,
                    enabled=entry.enabled,
                    toolset=entry.toolset,
                )
                try:
                    await manager.start_server(mcp_cfg)
                except Exception:
                    pass
        except Exception:
            pass

    return runner


def _create_provider(model_cfg: ModelConfig) -> ModelProvider:
    """根据配置创建真实 ModelProvider。

    缺省配置时创建 stub provider 以免意外使用真实模型。
    仅在完整配置时才创建真实 provider。
    """
    endpoint = model_cfg.main
    if not endpoint.is_configured():
        from cogito.model.stub_provider import StubModelProvider
        return StubModelProvider()

    from cogito.model.openai_compat import OpenAICompatProvider
    return OpenAICompatProvider(
        model=endpoint.model,
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
        timeout_seconds=endpoint.timeout_seconds,
    )


async def start_mcp_servers(
    config: Config,
    registry: CapabilityRegistry,
) -> MCPServerManager | None:
    """从配置启动 MCP Server 并注册工具。"""
    if not config.capability.mcp_servers:
        return None

    from cogito.capability.mcp.manager import MCPServerManager

    manager = MCPServerManager(registry)
    for entry in config.capability.mcp_servers:
        if not entry.enabled:
            continue

        from cogito.capability.mcp import MCPServerConfig

        mcp_cfg = MCPServerConfig(
            name=entry.name,
            transport=entry.transport,
            command=entry.command,
            args=entry.args,
            url=entry.url,
            enabled=entry.enabled,
            toolset=entry.toolset,
        )
        try:
            await manager.start_server(mcp_cfg)
        except Exception as e:
            # MCP Server 启动失败不影响整体启动
            pass

    return manager
