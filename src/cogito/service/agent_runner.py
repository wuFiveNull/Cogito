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
import json
import logging
import sqlite3
from enum import StrEnum

_LOGGER = logging.getLogger("cogito.agent_runner")

from cogito.bench import timing as _bench_timing
from cogito.capability import CapabilityRegistry
from cogito.capability.executor import ToolExecutor
from cogito.config import Config, ModelConfig
from cogito.contracts.clock import Clock, ProductionClock, epoch_ms
from cogito.contracts.context import ContextBuilder
from cogito.model.llm_manager import LLMManager, create_provider
from cogito.model.provider import ModelProvider
from cogito.model.router import ModelRouter
from cogito.runtime.loop import AgentLoop, LoopResultType
from cogito.service.completion import TurnCompletionService
from cogito.service.dispatcher import Dispatcher
from cogito.service.memory_service import SqliteMemoryService
from cogito.service.streaming_delivery import (
    StreamingDeliveryController,
    StreamInputMeta,
    StreamPolicy,
)
from cogito.store.model_call_repo import ModelCallRecord, ModelCallRepository

# ── 默认模式-Toolset 映射 (AGENT-COGNITION / 2.2) ──

MODE_TOOLSETS: dict[str, set[str]] = {
    "reactive": {
        "core",
        "memory",
        "terminal",
        "search",
        "disk",
        "file",
        "web",
        "schedule",
        "skills",
        "subagent",
        "mcp",
    },
    "proactive": {"core", "memory", "message"},
    "scheduled": {"core", "memory", "schedule"},
    "maintenance": {"core", "memory", "disk"},
}


def _parse_json(value: Any) -> dict:
    """把 DB 的 JSON 列（可能为 None / dict / str）解析为 dict。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class RunOutcome(StrEnum):
    """AgentRunner.run_once 的执行结果。"""

    idle = "idle"  # 无可用 Turn
    completed = "completed"  # 成功完成
    failed = "failed"  # 模型或提交失败
    lost = "lost"  # Lease 失效或取消
    cancelled = "cancelled"  # 被外部取消
    waiting_user = "waiting_user"  # 等待 Tool 审批
    waiting_external = "waiting_external"


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
        memory_service: SqliteMemoryService | None = None,
        channel_gateway: Any | None = None,
        channel_manager: Any | None = None,
        streaming_enabled: bool = True,
        stream_policy: StreamPolicy | None = None,
        vision_service: Any | None = None,
        multimodal_reader: Any | None = None,
        knowledge_reader: Any | None = None,
        knowledge_top_k: int = 8,
        knowledge_budget_ratio: float = 0.20,
        agent_mode: str = "reactive",
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
            conn,
            clock=self._clock,
            max_input_tokens=max_input_tokens,
            memory_reader=memory_service,
            multimodal_reader=multimodal_reader,
            knowledge_reader=knowledge_reader,
            knowledge_top_k=knowledge_top_k,
            knowledge_budget_ratio=knowledge_budget_ratio,
        )
        self._vision_service = vision_service
        # 记录每次 Provider 调用到 model_calls（可观察性 / 链路追踪）
        self._model_call_repo = ModelCallRepository(conn)
        router._on_call_completed = self._record_model_call
        from cogito.store.checkpoint_repo import CheckpointRepository

        checkpoint_repo = CheckpointRepository(conn)
        self._loop = AgentLoop(
            router,
            registry=registry,
            executor=executor,
            toolsets=toolsets,
            checkpoint_callback=lambda data: checkpoint_repo.save(
                str(data.get("turn_id", "")),
                data,
            ),
            checkpoint_loader=checkpoint_repo.load_latest,
            agent_mode=agent_mode,
        )
        self._completion = TurnCompletionService(conn, clock=self._clock)
        # ── Plan 05 M4：流式投递依赖（由组合根注入）──
        self.channel_gateway = channel_gateway
        self.channel_manager = channel_manager
        self._streaming_enabled = streaming_enabled
        self._stream_policy = stream_policy

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
        _bench_timing.checkpoint("worker:wake_up")
        claimed = self._dispatcher.claim_next(worker_id, clock=self._clock.now())
        if claimed is None:
            _bench_timing.finalize()
            return RunOutcome.idle

        turn = claimed.turn
        attempt = claimed.attempt
        _bench_timing.reset(turn.turn_id)
        _bench_timing.checkpoint("worker:claimed", extra={"turn_id": turn.turn_id})

        # ── 检查即将开始前的取消状态 ──
        if self._is_cancelled(turn.turn_id):
            _bench_timing.finalize()
            return RunOutcome.cancelled

        # ── 2. 有界等待自动视觉分析（失败/超时不阻断主 Turn）──
        if self._vision_service is not None:
            try:
                await self._vision_service.ensure_message_analyses(
                    turn.input_message_id,
                )
            except Exception:
                _LOGGER.warning("inline vision analysis failed open", exc_info=True)

        # ── 2a. 构建 Context（短暂读库，不持网络锁）──
        context = self._context_builder.build(
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            input_message_id=turn.input_message_id,
            system_policy=self._system_prompt,
        )
        self._persist_context_snapshot(context, attempt)
        _bench_timing.checkpoint("context:built")

        # ── 记忆暴露信号（PLAN-14 R-05）：注入上下文的记忆 → exposed ──
        self._record_memory_exposed(context)

        # ── 2a. 预读输入消息元数据（流式 / 非流式都可能用来向浏览器回推） ──
        stream_meta = self._read_stream_input_meta(turn)

        # ── 2b. 流式投递分支（按渠道能力 / 配置决定是否走占位→编辑→定稿）──
        if self._should_stream(turn):
            return await self._run_streaming_turn(turn, attempt, worker_id, context)

        # ── 3. 执行 Agent Loop（事务外，网络调用）──
        try:
            loop_result = await self._run_loop_with_heartbeat(
                turn,
                attempt,
                worker_id,
                context,
            )
            _bench_timing.checkpoint(
                "loop:done",
                extra={
                    "result_type": loop_result.result_type,
                    "text_len": len(loop_result.text or ""),
                },
            )
        except Exception as e:
            _LOGGER.exception("AgentLoop.run() threw: %s", e)
            self._fail_safe(turn, attempt)
            self._push_reply_error(stream_meta, "推理异常，请稍后重试")
            _bench_timing.finalize()
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
                _bench_timing.finalize()
                return RunOutcome.cancelled
            if loop_result.result_type == LoopResultType.waiting_approval:
                if self._pause_for_approval(turn, attempt, loop_result.approval_id):
                    _bench_timing.finalize()
                    return RunOutcome.waiting_user
                self._fail_safe(turn, attempt)
                _bench_timing.finalize()
                return RunOutcome.failed
            if loop_result.result_type == LoopResultType.waiting_external:
                if self._pause_for_external(turn, attempt, loop_result.waiting_id):
                    _bench_timing.finalize()
                    return RunOutcome.waiting_external
                self._fail_safe(turn, attempt)
                _bench_timing.finalize()
                return RunOutcome.failed
            if loop_result.error_message:
                _LOGGER.error("Loop failed: %s", loop_result.error_message)
            self._fail_safe(turn, attempt)
            self._push_reply_error(stream_meta, "推理失败，请稍后重试")
            _bench_timing.finalize()
            return RunOutcome.failed

        # ── 4. 完成前检查取消和 Lease ──
        if self._is_cancelled(turn.turn_id):
            _bench_timing.finalize()
            return RunOutcome.cancelled

        if not self._is_lease_valid(
            turn.turn_id,
            attempt.attempt_id,
            attempt.worker_id,
            attempt.lease_version,
        ):
            _bench_timing.finalize()
            return RunOutcome.lost

        # ── 5. 写入结果（事务内）──
        try:
            message_id = self._completion.complete_reply(
                turn=turn,
                attempt=attempt,
                reply_text=loop_result.text,
            )
            _bench_timing.checkpoint("completion:reply_written")
            if message_id is None:
                self._push_reply_error(stream_meta, "处理失败，请重试")
                _bench_timing.finalize()
                return RunOutcome.failed

            # 非流式路径：reply 只写入了 DB，需主动推一条 send 事件到浏览器队列，
            # 否则依赖 WS 的 chat 页面永远看不到回复（占位气泡也不会出现）。
            self._push_reply_event(stream_meta, loop_result.text)
            _bench_timing.checkpoint("completion:pushed_to_ws")

            _bench_timing.finalize()
            return RunOutcome.completed
        except Exception as e:
            _LOGGER.exception("complete_reply failed: %s", e)
            self._fail_safe(turn, attempt)
            self._push_reply_error(stream_meta, "处理失败，请稍后重试")
            return RunOutcome.failed

    async def _run_loop_with_heartbeat(
        self,
        turn,
        attempt,
        worker_id: str,
        context,
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
                turn.turn_id,
                attempt.attempt_id,
                worker_id,
                attempt.lease_version,
            )
        )

        done, _ = await asyncio.wait(
            [loop_task, heartbeat_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        heartbeat_task.cancel()
        return loop_task.result()

    async def _heartbeat_loop(
        self,
        turn_id: str,
        attempt_id: str,
        worker_id: str,
        lease_version: int,
    ) -> None:
        """定期发送 heartbeat 防止 Lease 过期。"""
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            try:
                ok = self._dispatcher.heartbeat(
                    turn_id,
                    attempt_id,
                    worker_id,
                    lease_version,
                    clock=self._clock.now(),
                )
                if not ok:
                    return
            except Exception:
                return

    # ── Plan 05 M4：流式投递分支 ───────────────────────────────────────────

    def _should_stream(self, turn: Any) -> bool:
        """判断是否走流式投递：配置开启 + 渠道支持 edit / streaming。"""
        if not self._streaming_enabled:
            return False
        if self.channel_gateway is None or self.channel_manager is None:
            return False
        meta = self._read_stream_input_meta(turn)
        if meta is None:
            return False
        adapter_id = (
            meta.reply_route.get("channel_instance_id") or meta.reply_route.get("adapter_id") or ""
        )
        adapter = self.channel_manager.get_adapter(adapter_id)
        if adapter is None:
            return False
        caps = adapter.capabilities()
        return bool(caps.supports_edit and caps.supports_streaming)

    def _read_stream_input_meta(self, turn: Any) -> StreamInputMeta | None:
        """读取输入消息元数据，构造 StreamInputMeta。"""
        row = self._conn.execute(
            "SELECT conversation_id, session_id, sender_principal_id, "
            "sender_endpoint_id, reply_route_json, capability_snapshot_json "
            "FROM messages WHERE message_id=?",
            (turn.input_message_id,),
        ).fetchone()
        if row is None:
            return None
        return StreamInputMeta(
            conversation_id=row["conversation_id"] or "",
            session_id=row["session_id"] or turn.session_id or "",
            endpoint_id=row["sender_endpoint_id"] or "",
            principal_id=row["sender_principal_id"] or "",
            reply_route=_parse_json(row["reply_route_json"]),
            capability_snapshot=_parse_json(row["capability_snapshot_json"]),
            input_message_id=turn.input_message_id,
        )

    async def _run_streaming_turn(
        self,
        turn: Any,
        attempt: Any,
        worker_id: str,
        context: Any,
    ) -> RunOutcome:
        """流式投递回合：占位 → 增量编辑 → 定稿（单事务完成 Turn）。"""
        _bench_timing.checkpoint("streaming:start_controller")
        meta = self._read_stream_input_meta(turn)
        if meta is None:
            self._fail_safe(turn, attempt)
            _bench_timing.finalize()
            return RunOutcome.failed

        adapter_id = (
            meta.reply_route.get("channel_instance_id") or meta.reply_route.get("adapter_id") or ""
        )
        adapter = self.channel_manager.get_adapter(adapter_id)
        if adapter is None:
            # _should_stream 已保证 adapter 存在；防御性兜底走非流式
            self._fail_safe(turn, attempt)
            _bench_timing.finalize()
            return RunOutcome.failed
        caps = adapter.capabilities()

        controller = StreamingDeliveryController(
            conn=self._conn,
            gateway=self.channel_gateway,
            loop=self._loop,
            capabilities=caps,
            clock=self._clock,
            policy=self._stream_policy or StreamPolicy(),
            dispatcher=self._dispatcher,
        )

        cancel_check = self._make_cancel_check(turn.turn_id)
        hb = asyncio.create_task(
            self._heartbeat_loop(
                turn.turn_id,
                attempt.attempt_id,
                worker_id,
                attempt.lease_version,
            )
        )
        try:
            final_message_id = await controller.run_streaming_turn(
                turn=turn,
                attempt=attempt,
                context=context,
                input_meta=meta,
                cancel_flag=cancel_check,
            )
        except Exception:
            hb.cancel()
            _LOGGER.exception("Streaming turn failed: %s", turn.turn_id)
            self._fail_safe(turn, attempt)
            _bench_timing.finalize()
            return RunOutcome.failed
        finally:
            hb.cancel()

        if final_message_id:
            _bench_timing.checkpoint(
                "streaming:finalized",
                extra={
                    "final_message_id": final_message_id,
                },
            )
            _bench_timing.finalize()
            return RunOutcome.completed
        if self._is_cancelled(turn.turn_id):
            return RunOutcome.cancelled
        self._fail_safe(turn, attempt)
        return RunOutcome.failed

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
        self,
        turn_id: str,
        attempt_id: str,
        worker_id: str,
        lease_version: int,
    ) -> bool:
        """验证 Lease 有效性（不修改状态）。"""
        row = self._conn.execute(
            "SELECT 1 FROM run_attempts "
            "WHERE attempt_id=? AND turn_id=? "
            "AND status='running' AND worker_id=? AND lease_version=? "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (attempt_id, turn_id, worker_id, lease_version, epoch_ms(self._clock.now())),
        ).fetchone()
        return row is not None

    def _record_memory_exposed(self, context) -> None:
        """PLAN-14 R-05: 被注入 Turn 上下文的记忆 → exposed 信号（非阻塞）。"""
        memory_ids = getattr(context, "memory_ids", None)
        if not memory_ids:
            return
        try:
            from cogito.service.memory_signals import SignalWriter

            writer = SignalWriter(self._conn)
            for mid in memory_ids:
                writer.record_exposed(
                    mid,
                    idempotency_key=f"context-exposed:{context.turn_id}:{mid}",
                    algorithm_version="2",
                )
        except Exception:
            _LOGGER.debug("exposed signal recording failed", exc_info=True)

    def _fail_safe(self, turn, attempt) -> None:
        """安全地标记 Attempt 失败，不抛出。"""
        try:
            self._dispatcher.fail(
                turn.turn_id,
                attempt.attempt_id,
                turn.version,
                worker_id=attempt.worker_id,
                lease_version=attempt.lease_version,
                clock=self._clock.now(),
            )
        except Exception:
            pass

    def _pause_for_approval(self, turn, attempt, approval_id: str) -> bool:
        """Finish the current Attempt and put its Turn in waiting_user."""
        now = epoch_ms(self._clock.now())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            attempt_cur = self._conn.execute(
                "UPDATE run_attempts SET status='succeeded', finished_at=? "
                "WHERE attempt_id=? AND turn_id=? AND status='running' "
                "AND worker_id=? AND lease_version=?",
                (now, attempt.attempt_id, turn.turn_id, attempt.worker_id, attempt.lease_version),
            )
            turn_cur = self._conn.execute(
                "UPDATE turns SET status='waiting_user', active_attempt_id=NULL, "
                "version=version+1 WHERE turn_id=? AND version=? "
                "AND status='running' AND active_attempt_id=?",
                (turn.turn_id, turn.version, attempt.attempt_id),
            )
            if attempt_cur.rowcount != 1 or turn_cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.commit()
            _LOGGER.info(
                "Turn %s paused for Tool approval %s",
                turn.turn_id,
                approval_id,
            )
            return True
        except Exception:
            self._conn.rollback()
            _LOGGER.exception("failed to pause Turn for approval")
            return False

    def _pause_for_external(self, turn, attempt, waiting_id: str) -> bool:
        """Finish the Attempt and leave the Turn resumable by a join evaluator."""
        now = epoch_ms(self._clock.now())
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            attempt_cur = self._conn.execute(
                "UPDATE run_attempts SET status='succeeded',finished_at=? "
                "WHERE attempt_id=? AND turn_id=? AND status='running' "
                "AND worker_id=? AND lease_version=?",
                (now, attempt.attempt_id, turn.turn_id, attempt.worker_id, attempt.lease_version),
            )
            turn_cur = self._conn.execute(
                "UPDATE turns SET status='waiting_external',active_attempt_id=NULL,"
                "version=version+1 "
                "WHERE turn_id=? AND version=? AND status='running' AND active_attempt_id=?",
                (turn.turn_id, turn.version, attempt.attempt_id),
            )
            if attempt_cur.rowcount != 1 or turn_cur.rowcount != 1:
                self._conn.rollback()
                return False
            # A very fast child may satisfy the join before the parent reaches
            # waiting_external. In that race, queue the parent immediately.
            self._conn.execute(
                "UPDATE turns SET status='queued',version=version+1 WHERE turn_id=? "
                "AND status='waiting_external' AND EXISTS ("
                "SELECT 1 FROM waiting_conditions WHERE owner_type='turn' AND owner_id=? "
                "AND waiting_id=? AND status='satisfied')",
                (turn.turn_id, turn.turn_id, waiting_id),
            )
            self._conn.commit()
            _LOGGER.info("Turn %s paused for external work %s", turn.turn_id, waiting_id)
            return True
        except Exception:
            self._conn.rollback()
            _LOGGER.exception("failed to pause Turn for external work")
            return False

    def _record_model_call(self, info: dict) -> None:
        """Router 回调：把每次 Provider 调用写入 model_calls，用于链路追踪。"""
        usage = info.get("usage")
        status = info.get("status", "error")
        if status == "success" and not usage:
            from cogito.model.contracts import Usage

            usage = Usage()
        elif not usage:
            from cogito.model.contracts import Usage

            usage = Usage()
        record = ModelCallRecord(
            attempt_id=info.get("attempt_id", ""),
            request_id=info.get("request_id", ""),
            provider_id=info.get("provider_id", ""),
            model_id=info.get("model_id", ""),
            status=status,
            finish_reason=info.get("finish_reason"),
            input_tokens=(usage.input_tokens if usage else 0) or 0,
            output_tokens=(usage.output_tokens if usage else 0) or 0,
            cached_tokens=(usage.cached_tokens if usage else 0) or 0,
            latency_ms=info.get("latency_ms", 0),
            error_category=info.get("error_category"),
            retry_count=info.get("retry_count", 0),
            trace_id=info.get("trace_id", ""),
        )
        try:
            self._model_call_repo.insert(record)
        except Exception:
            _LOGGER.warning("model_call insert failed", exc_info=True)

    def _persist_context_snapshot(self, context, attempt) -> None:
        """Persist the exact immutable context selected for this RunAttempt."""
        from cogito.store.context_snapshot_repo import (
            ContextSnapshotRecord,
            ContextSnapshotRepository,
            SnapshotItem,
        )

        record = ContextSnapshotRecord(
            snapshot_id=context.snapshot_id,
            session_id=context.session_id,
            attempt_id=attempt.attempt_id,
            message_upper_bound=context.message_upper_bound,
            query_plan_version=context.query_plan_version,
            selection_policy_version=context.selection_policy_version,
            token_budget=self._context_builder._max_input_tokens,
            tokens_used=context.total_tokens,
            excluded_summary=bool(context.excluded_summary),
            created_at=context.created_at,
            per_source_tokens=dict(context.per_source_tokens),
            exclusion_stats=dict(context.exclusion_stats),
            items=[
                SnapshotItem(
                    item_index=index,
                    source=item.source,
                    content_ref=f"{item.item_type}:{item.item_id}",
                    score=item.score,
                    tokens=item.tokens,
                    trust_label=item.trust_label,
                    retrieval_path=item.retrieval_path,
                    provenance=dict(item.provenance),
                )
                for index, item in enumerate(context.items)
            ],
        )
        ContextSnapshotRepository(self._conn).insert(record)
        self._conn.commit()

    # ── 非流式路径：通过网关把回复/错误推入浏览器 WS 队列 ────────────────

    def _build_target_json(self, meta: Any) -> str | None:
        """把 StreamInputMeta 序列化成 gateway 可路由的 target_snapshot JSON。"""
        if meta is None:
            return None
        reply_route = meta.reply_route or {}
        adapter_id = reply_route.get("channel_instance_id") or reply_route.get("adapter_id") or ""
        conversation_id = reply_route.get("platform_conversation_id") or meta.conversation_id
        return json.dumps(
            {
                "adapter_id": adapter_id,
                "target_endpoint_ref": reply_route.get("target_endpoint_ref") or adapter_id,
                "conversation_id": conversation_id,
                "reply_route": reply_route,
            }
        )

    def _push_reply_event(self, meta: Any, text: str) -> None:
        """非流式 Turn 完成后：把最终回复文本作为 send 事件推入浏览器队列。"""
        if not text or self.channel_gateway is None:
            return
        if (meta.reply_route or {}).get("channel_instance_id") == "terminal":
            # process_terminal_message reads the persisted assistant message and
            # returns it directly; there is intentionally no terminal adapter.
            return
        target_json = self._build_target_json(meta)
        if target_json is None:
            return
        try:
            result = self.channel_gateway.send_text(target_json, text)
            if result.status != "sent":
                _LOGGER.warning(
                    "non-stream reply push returned status=%s conversation=%s",
                    result.status,
                    meta.conversation_id if meta else "?",
                )
        except Exception:
            _LOGGER.warning("non-stream reply push failed", exc_info=True)

    def _push_reply_error(self, meta: Any, message: str) -> None:
        """把错误提示推入浏览器队列，让用户至少看到反馈而非无声无息。"""
        if not message or self.channel_gateway is None:
            return
        if (meta.reply_route or {}).get("channel_instance_id") == "terminal":
            return
        target_json = self._build_target_json(meta)
        if target_json is None:
            return
        try:
            self.channel_gateway.send_text(target_json, f"(错误) {message}")
        except Exception:
            _LOGGER.warning("reply error push failed", exc_info=True)


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
    memory_service: SqliteMemoryService | None = None,
    channel_gateway: Any | None = None,
    channel_manager: Any | None = None,
    streaming_enabled: bool = True,
    stream_policy: StreamPolicy | None = None,
    llm_manager: LLMManager | None = None,
    vision_service: Any | None = None,
    multimodal_reader: Any | None = None,
    knowledge_reader: Any | None = None,
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

    # 创建 LLMManager（多 Provider 角色路由门面）
    # 传入显式 provider 时（测试/自定义），退化到单 Provider 行为
    if llm_manager is None:
        if provider is not None:
            llm_manager = LLMManager.from_provider(provider)
        else:
            llm_manager = LLMManager.build(config.model)

    router = llm_manager.router

    # ── PLAN-09 M4a/C2 破环：registry 由组合根预装配后传入，
    #    service.agent_runner 不再反向 import cogito.tools ──
    resolved_registry = registry or CapabilityRegistry()

    # 创建 Executor。Auto Mode 是确定性 ToolPolicy 之后的附加安全闸门。
    auto_mode_gate = None
    if config.capability.auto_mode.enabled:
        from cogito.capability.auto_mode import AutoModeGate, LLMAutoModeClassifier

        auto_cfg = config.capability.auto_mode
        auto_mode_gate = AutoModeGate(
            LLMAutoModeClassifier(
                router,
                model_role=auto_cfg.model_role,
                stage1_timeout_seconds=auto_cfg.stage1_timeout_seconds,
                stage2_timeout_seconds=auto_cfg.stage2_timeout_seconds,
            ),
            safe_tools=set(auto_cfg.safe_tools),
            max_argument_chars=auto_cfg.max_argument_chars,
        )
    from cogito.infrastructure.payload_store import PayloadStore
    from cogito.service.approval_service import SqliteApprovalService
    from cogito.service.task_service import SqliteTaskService
    from cogito.service.tool_sinks import ToolCallRepositorySink
    from cogito.store.receipt_repo import SideEffectReceiptRepository
    from cogito.store.tool_call_repo import ToolCallRepository

    executor = ToolExecutor(
        resolved_registry,
        sink=ToolCallRepositorySink(
            ToolCallRepository(connection),
            SideEffectReceiptRepository(connection),
            SqliteTaskService(connection),
            connection,
        ),
        auto_mode=auto_mode_gate,
        approval_service=SqliteApprovalService(connection),
        payload_store=PayloadStore(config.resolve_payload_dir(), connection),
    )

    # 解析 Toolset（默认 reactive）
    resolved_toolsets = toolsets
    if resolved_toolsets is None:
        agent_mode = config.agent.mode if hasattr(config.agent, "mode") else "reactive"
        resolved_toolsets = MODE_TOOLSETS.get(agent_mode, {"core"})

    # 配置覆盖
    if config.agent.enabled_toolsets:
        resolved_toolsets = set(config.agent.enabled_toolsets)
    if config.agent.disabled_toolsets:
        resolved_toolsets -= set(config.agent.disabled_toolsets)

    from cogito.runtime.delegation import create_delegation_tool_defs
    from cogito.service.delegation_lifecycle import DelegationLifecycleService

    for delegation_tool in create_delegation_tool_defs(
        connection=connection,
        router=router,
        registry=resolved_registry,
        executor=executor,
        parent_toolsets=resolved_toolsets,
        lifecycle=DelegationLifecycleService(connection),
    ):
        resolved_registry.register(delegation_tool)

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
        memory_service=memory_service,
        channel_gateway=channel_gateway,
        channel_manager=channel_manager,
        streaming_enabled=streaming_enabled,
        stream_policy=stream_policy,
        vision_service=vision_service,
        multimodal_reader=multimodal_reader,
        knowledge_reader=knowledge_reader,
        knowledge_top_k=config.knowledge.retrieval.top_k,
        knowledge_budget_ratio=config.knowledge.retrieval.token_budget_ratio,
        agent_mode=config.agent.mode,
    )


async def build_and_start_agent_runner(
    config: Config,
    connection: sqlite3.Connection,
    provider: ModelProvider | None = None,
    clock: Clock | None = None,
    registry: CapabilityRegistry | None = None,
    toolsets: set[str] | None = None,
    memory_service: SqliteMemoryService | None = None,
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
        memory_service=memory_service,
    )

    # 启动 MCP Server
    if config.capability.mcp_servers:
        try:
            from cogito.capability.mcp.manager import MCPServerManager

            manager = MCPServerManager(
                runner._registry,
                aliases=config.capability.mcp_aliases,
                sampling_callback=_make_mcp_sampling_callback(runner._router),
            )
            for entry in config.capability.mcp_servers:
                if not entry.enabled:
                    continue
                mcp_cfg = _to_mcp_server_config(entry)
                try:
                    await manager.start_server(mcp_cfg)
                except Exception:
                    pass
        except Exception:
            pass

    return runner


def _create_provider(model_cfg: ModelConfig) -> ModelProvider:
    """根据配置创建真实 ModelProvider（向后兼容包装）。

    - provider="echo"：回显 Provider，最后一条用户消息原样返回（离线调试用）
    - 缺省配置时创建 stub provider 以免意外使用真实模型。
    - 仅在完整配置时才创建真实 provider。

    实际逻辑已下沉到 cogito.model.llm_manager.create_provider。
    """
    return create_provider(model_cfg.main, default_adapter=model_cfg.provider)


async def start_mcp_servers(
    config: Config,
    registry: CapabilityRegistry,
    router: Any | None = None,
) -> MCPServerManager | None:
    """从配置启动 MCP Server 并注册工具。"""
    if not config.capability.mcp_servers:
        return None

    from cogito.capability.mcp.manager import MCPServerManager

    manager = MCPServerManager(
        registry,
        aliases=config.capability.mcp_aliases,
        sampling_callback=_make_mcp_sampling_callback(router) if router else None,
    )
    for entry in config.capability.mcp_servers:
        if not entry.enabled:
            continue

        mcp_cfg = _to_mcp_server_config(entry)
        try:
            await manager.start_server(mcp_cfg)
        except Exception:
            # MCP Server 启动失败不影响整体启动
            pass

    return manager


def _to_mcp_server_config(entry: Any):
    from cogito.capability.mcp import MCPServerConfig

    return MCPServerConfig(
        name=entry.name,
        transport=entry.transport,
        command=entry.command,
        args=entry.args,
        url=entry.url,
        enabled=entry.enabled,
        toolset=entry.toolset,
        cwd=entry.cwd,
        include_tools=entry.include_tools,
        exclude_tools=entry.exclude_tools,
        timeout_seconds=entry.timeout_seconds,
        max_output_chars=entry.max_output_chars,
        allow_resources=entry.allow_resources,
        allow_prompts=entry.allow_prompts,
        allow_roots=entry.allow_roots,
        allow_sampling=entry.allow_sampling,
        isolation=entry.isolation,
        env=entry.env,
        headers=entry.headers,
        oauth_enabled=entry.oauth_enabled,
        oauth_token_file=entry.oauth_token_file,
        oauth_redirect_uri=entry.oauth_redirect_uri,
        oauth_scope=entry.oauth_scope,
        secret_root=entry.secret_root,
        tool_policy=entry.tool_policy,
        roots=entry.roots,
    )


def _make_mcp_sampling_callback(router: Any):
    """Create a no-Tool, tightly budgeted Sampling adapter for MCP."""

    import time

    usage_by_scope: dict[tuple[str, str], dict[str, float | int]] = {}

    async def sample(server_name: str, attempt_id: str, context: Any, params: Any) -> Any:
        from mcp import types
        from mcp.shared.exceptions import McpError

        from cogito.model.contracts import ModelRequest

        messages: list[dict[str, str]] = []
        if params.systemPrompt:
            messages.append({"role": "system", "content": params.systemPrompt[:4_000]})
        for message in params.messages[-20:]:
            content = getattr(message.content, "text", "")
            messages.append(
                {
                    "role": str(message.role),
                    "content": str(content)[:8_000],
                }
            )
        # Sampling belongs to the Agent Attempt that initiated the MCP Tool
        # call. Connector calls use a separate bounded scope.
        scope = (server_name, attempt_id or "connector")
        consumed = usage_by_scope.setdefault(
            scope,
            {"calls": 0, "tokens": 0, "started_at": time.monotonic()},
        )
        requested = min(max(1, int(params.maxTokens)), 2_048)
        if (
            int(consumed["calls"]) >= 4
            or int(consumed["tokens"]) + requested > 8_192
            or time.monotonic() - float(consumed["started_at"]) > 120
        ):
            raise McpError(types.ErrorData(code=-32001, message="MCP sampling budget exhausted"))
        consumed["calls"] += 1
        response = await router.generate(
            ModelRequest(
                model_role="mcp_sampling",
                messages=tuple(messages),
                tools=(),
                max_output_tokens=requested,
                temperature=params.temperature,
            ),
            model_role="mcp_sampling",
        )
        consumed["tokens"] += response.usage.total_tokens
        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text=response.text),
            model=response.model_id or "cogito-mcp-sampling",
            stopReason="endTurn",
        )

    return sample
