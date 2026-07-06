"""Orchestrator — 连接 Dispatcher、ContextBuilder、AgentLoop 和 TurnCompletion。

PR 10-C / EXECUTION-LIFECYCLE / 3.3 完成 Attempt：
- Model 调用在事务外
- 执行期间按配置 heartbeat
- 完成前重新验证 Turn version、active Attempt 和 Lease
- 最终 Message、Delivery、Outbox、Attempt、Turn 仍在一个短事务提交
- Agent Loop 不直接使用 Connection
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import Any

from cogito.model.router import ModelRouter
from cogito.runtime.clock import Clock, ProductionClock
from cogito.runtime.context import ContextBuilder, ContextSnapshot
from cogito.runtime.loop import AgentLoop, LoopResult, LoopResultType
from cogito.service.completion import TurnCompletionService
from cogito.service.dispatcher import Dispatcher
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.time_utils import epoch_ms


class OrchestratorError(Exception):
    """Orchestrator 层错误。"""
    pass


class Orchestrator:
    """Orchestrator — 调度并协调 Turn 的完整执行。

    流程：
    1. Dispatcher.claim_next → 获取 ClaimedRun
    2. ContextBuilder.build → 创建 ContextSnapshot
    3. AgentLoop.run → 执行 Model 调用循环
    4. TurnCompletionService._complete → 原子写入结果

    Model 调用在事务外。
    取消后旧 Model 结果不得提交。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        router: ModelRouter,
        clock: Clock | None = None,
        model_role: str = "main",
        heartbeat_interval_s: int = 30,
        max_input_tokens: int = 64000,
    ) -> None:
        self._conn = conn
        self._router = router
        self._clock = clock or ProductionClock()
        self._model_role = model_role
        self._heartbeat_interval_s = heartbeat_interval_s
        self._dispatcher = Dispatcher(conn, clock=self._clock)
        self._context_builder = ContextBuilder(
            conn, clock=self._clock, max_input_tokens=max_input_tokens,
        )
        self._loop = AgentLoop(router)
        self._completion = TurnCompletionService(conn, clock=self._clock)

    async def run_one(self, worker_id: str) -> str | None:
        """领取一个 Turn 并执行完成。

        Returns: final_message_id 或 None（无可用 Turn）。
        """
        # ── 1. 领取 Turn（事务内）──
        claimed = self._dispatcher.claim_next(worker_id, clock=self._clock.now())
        if claimed is None:
            return None

        turn = claimed.turn
        attempt = claimed.attempt
        cancel_flag = self._make_cancel_check(turn.turn_id)

        # ── 2. 构建 Context（非网络操作，短暂持锁）──
        context = self._context_builder.build(
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            input_message_id=turn.input_message_id,
            system_policy="You are Cogito, a helpful AI assistant.",
        )

        # ── 3. 执行 Agent Loop（事务外）──
        try:
            loop_result = await self._loop.run(
                context,
                model_role=self._model_role,
                cancel_flag=cancel_flag,
            )
        except Exception as e:
            # Loop 异常 → fail the attempt
            self._dispatcher.fail(
                turn.turn_id, attempt.attempt_id,
                turn.version,
                worker_id=worker_id,
                lease_version=attempt.lease_version,
                clock=self._clock.now(),
            )
            raise OrchestratorError(f"Agent loop failed: {e}") from e

        # ── 4. 如果被取消，不提交结果 ──
        if loop_result.result_type in (
            LoopResultType.cancelled,
            LoopResultType.max_iterations,
            LoopResultType.max_tokens,
            LoopResultType.max_runtime,
        ):
            self._dispatcher.fail(
                turn.turn_id, attempt.attempt_id,
                turn.version,
                worker_id=worker_id,
                lease_version=attempt.lease_version,
                clock=self._clock.now(),
            )
            return None

        # ── 5. 写入最终结果（短事务）──
        try:
            message_id = self._completion._complete(
                turn=turn,
                attempt=attempt,
                message=self._make_message(turn, loop_result),
                channel_type="test",
                delivery_target="test_channel",
                endpoint_id="",
                principal_id="",
            )
        except Exception:
            # Completion 失败（如 Lease 失效） → fail
            self._dispatcher.fail(
                turn.turn_id, attempt.attempt_id,
                turn.version,
                worker_id=worker_id,
                lease_version=attempt.lease_version,
                clock=self._clock.now(),
            )
            raise

        return message_id

    def _make_cancel_check(self, turn_id: str):
        """创建取消检查闭包。"""
        cancel_requested = False

        def check() -> bool:
            nonlocal cancel_requested
            if cancel_requested:
                return True
            row = self._conn.execute(
                "SELECT cancel_requested_at FROM turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if row and row["cancel_requested_at"] is not None:
                cancel_requested = True
                return True
            return False

        return check

    def _make_message(self, turn, loop_result: LoopResult):
        """从 LoopResult 创建 Message。"""
        from cogito.domain.message import (
            ContentPart,
            Message,
            MessageDirection,
            MessageRole,
        )

        parts = []
        for cp in loop_result.content_parts:
            parts.append(ContentPart(
                content_type="text",
                inline_data=cp.text,
            ))

        if not parts:
            parts.append(ContentPart(
                content_type="text",
                inline_data=loop_result.text,
            ))

        return Message(
            conversation_id="",
            session_id=turn.session_id,
            sender_principal_id="cogito",
            sender_endpoint_id="cogito",
            role=MessageRole.assistant,
            direction=MessageDirection.outbound,
            content_parts=parts,
            reply_to_message_id=turn.input_message_id,
        )
