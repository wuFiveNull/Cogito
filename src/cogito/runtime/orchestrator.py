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

from cogito.model.router import ModelRouter
from cogito.runtime.clock import Clock, ProductionClock
from cogito.runtime.context import ContextBuilder
from cogito.runtime.loop import AgentLoop, LoopResult
from cogito.service.completion import TurnCompletionService
from cogito.service.dispatcher import Dispatcher
from cogito.store.model_call_repo import ModelCallRecord, ModelCallRepository
from cogito.store.time_utils import epoch_ms


class OrchestratorError(Exception):
    """Orchestrator 层错误。"""
    pass


class Orchestrator:
    """Orchestrator — 调度并协调 Turn 的完整执行。

    流程：
    1. Dispatcher.claim_next → 获取 ClaimedRun
    2. ContextBuilder.build → 创建 ContextSnapshot
    3. AgentLoop.run → 执行 Model 调用循环（事务外）
    4. 执行期间按配置 heartbeat
    5. 完成前重新验证 Lease 有效性
    6. TurnCompletionService._complete → 原子写入结果
    7. 每次 Provider 调用通过 ModelCallRepository 记录

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
        self._model_role = model_role
        self._heartbeat_interval_s = heartbeat_interval_s
        self._clock = clock or ProductionClock()
        self._model_call_repo = ModelCallRepository(conn)

        # 创建带有 ModelCall 回调的 Router
        self._router = router
        self._router._on_call_completed = self._record_model_call

        self._dispatcher = Dispatcher(conn, clock=self._clock)
        self._context_builder = ContextBuilder(
            conn, clock=self._clock, max_input_tokens=max_input_tokens,
        )
        self._loop = AgentLoop(router)
        self._completion = TurnCompletionService(conn, clock=self._clock)

    def _now_ms(self) -> int:
        return epoch_ms(self._clock.now())

    def _record_model_call(self, info: dict) -> None:
        """通过 Router 回调记录每次 Provider 调用。"""
        usage = info.get("usage")
        record = ModelCallRecord(
            attempt_id=info.get("attempt_id", ""),
            request_id=info.get("request_id", ""),
            provider_id=info.get("provider_id", ""),
            model_id=info.get("model_id", ""),
            status=info.get("status", "error"),
            finish_reason=info.get("finish_reason"),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cached_tokens=usage.cached_tokens if usage else 0,
            latency_ms=info.get("latency_ms", 0),
            error_category=info.get("error_category"),
            retry_count=info.get("retry_count", 0),
            trace_id=info.get("trace_id", ""),
        )
        self._model_call_repo.insert(record)

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

        # ── 3. 执行 Agent Loop（事务外，附带 heartbeat）──
        try:
            loop_task = asyncio.create_task(
                self._loop.run(
                    context,
                    model_role=self._model_role,
                    cancel_flag=cancel_flag,
                )
            )

            # 执行期间发送 heartbeat
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(
                    turn.turn_id, attempt.attempt_id, worker_id, attempt.lease_version,
                )
            )

            done, _ = await asyncio.wait(
                [loop_task, heartbeat_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Loop 完成 → 取消 heartbeat
            heartbeat_task.cancel()
            loop_result = loop_task.result()

        except Exception as e:
            self._fail_attempt(turn, attempt, worker_id)
            raise OrchestratorError(f"Agent loop failed: {e}") from e

        # ── 4. 如果未产生有效响应，不提交结果 ──
        if not loop_result.is_success:
            self._fail_attempt(turn, attempt, worker_id)
            return None

        # ── 5. 完成前重新验证 Lease 有效性 ──
        lease_valid = self._verify_lease(
            turn.turn_id, attempt.attempt_id,
            turn.version, worker_id, attempt.lease_version,
        )
        if not lease_valid:
            self._fail_attempt(turn, attempt, worker_id)
            return None

        # ── 6. 写入最终结果（短事务）──
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
            self._fail_attempt(turn, attempt, worker_id)
            raise

        return message_id

    def _verify_lease(
        self, turn_id: str, attempt_id: str, expected_version: int,
        worker_id: str, lease_version: int,
    ) -> bool:
        """重新验证 Lease 有效性（完成前调用）。"""
        return self._dispatcher.complete(
            turn_id, attempt_id, expected_version,
            worker_id=worker_id, lease_version=lease_version,
            clock=self._clock.now(),
        )

    def _fail_attempt(self, turn, attempt, worker_id: str) -> None:
        """安全地标记 Attempt 失败。"""
        try:
            self._dispatcher.fail(
                turn.turn_id, attempt.attempt_id,
                turn.version,
                worker_id=worker_id,
                lease_version=attempt.lease_version,
                clock=self._clock.now(),
            )
        except Exception:
            pass  # 标记失败不抛出

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
                    return  # heartbeat 失败 → 停止
            except Exception:
                return

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
