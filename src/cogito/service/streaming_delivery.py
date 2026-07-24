"""StreamingDeliveryController —— 流式投递核心操作层 (Plan 05)。

把 AgentLoop 的增量 token 流，转换为"先占位 → 增量编辑 → 最终定稿"的
平台投递，全程走 ChannelGateway（与现有 send 对称），并写入可重放的
delivery_receipts 证据。

设计不变量 (STREAMING-DELIVERY):
- 每个流式操作 = Delivery Attempt 内的一个 operation_seq + receipt 证据
- 最终 Assistant Message 是内容事实源；Delivery 是发送状态事实源
- 流式 Delivery 创建为 status='streaming'，避开非流式 canonical effect worker
- 降级模式在首 token 前依 adapter capabilities 写定，运行中不切换
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cogito.bench import timing as _bench_timing
from cogito.contracts.clock import Clock, ProductionClock, epoch_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.message import ContentPart, Message, MessageDirection, MessageRole
from cogito.runtime.loop import AgentLoop
from cogito.service.dispatcher import Dispatcher
from cogito.service.streaming_delivery_event_store import StreamingDeliveryEventStore
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.event_replay import replay_turn
from cogito.store.event_store import EventStore

_LOG = logging.getLogger("cogito.streaming.delivery")

# on_delta 回调签名: (conversation_id, text, operation_seq, is_final)
OnDelta = Callable[[str, str, int, bool], None]


def _noop_on_delta(*_args: Any, **_kwargs: Any) -> None:
    return None


@dataclass
class StreamPolicy:
    """流式节流策略。"""

    throttle_ms: int = 40  # 两次 edit 之间最小间隔（节流合并）
    max_operations: int = 300  # 单 Delivery 最大 edit 操作数


@dataclass
class StreamInputMeta:
    """流式投递所需的输入元数据（来自输入消息 + Turn/Attempt）。"""

    conversation_id: str
    session_id: str
    endpoint_id: str
    principal_id: str
    reply_route: dict[str, Any]
    capability_snapshot: dict[str, Any]
    input_message_id: str


class StreamingDeliveryController:
    """流式投递控制器。

    协调 AgentLoop.run_stream → ChannelGateway.edit → StreamingDeliveryEventStore，
    并在完成时写入不可变 Assistant Message 并定稿 Delivery。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        gateway: Any,
        loop: AgentLoop,
        capabilities: Any,
        clock: Clock | None = None,
        policy: StreamPolicy | None = None,
        delivery_repo: StreamingDeliveryEventStore | None = None,
        dispatcher: Dispatcher | None = None,
        payload_store: Any | None = None,
    ) -> None:
        self._conn = conn
        self._gateway = gateway
        self._loop = loop
        self._capabilities = capabilities
        self._clock = clock or ProductionClock()
        self._policy = policy or StreamPolicy()
        self._delivery_repo = delivery_repo or StreamingDeliveryEventStore(conn, clock=self._clock)
        self._dispatcher = dispatcher or Dispatcher(conn, clock=self._clock)
        self._payload_store = payload_store

    async def run_streaming_turn(
        self,
        *,
        turn: Any,
        attempt: Any,
        context: Any,
        input_meta: StreamInputMeta,
        on_delta: OnDelta | None = None,
        cancel_flag: Callable[[], bool] | None = None,
    ) -> str | None:
        """执行一次流式投递回合。返回 final_message_id，失败/取消返回 None。"""
        on_delta = on_delta or _noop_on_delta
        _start_ts = self._clock.now().timestamp()  # 回合起点（turn 被领取后）

        if not self._capabilities.supports_edit:
            # 不支持编辑 → 上层应退回非流式路径
            _LOG.warning("StreamingDeliveryController: adapter 不支持 edit，退回上层")
            return None

        degradation_mode = "edit_placeholder"

        delivery_id = uuid.uuid4().hex
        attempt_id = uuid.uuid4().hex
        idempotency_key = f"delivery_{input_meta.conversation_id}_{turn.turn_id}"
        target = self._build_target(input_meta, delivery_id, attempt_id, idempotency_key)
        target_json = json.dumps(target)
        trace_context = self._event_context_for_turn(turn, attempt, input_meta)

        # 流式状态需要在模型网络调用前落盘（供崩溃恢复扫描），但不能让
        # 这次 INSERT 的隐式写事务跨越模型调用。否则同一进程的审计、
        # memory.extract 等独立连接会被 SQLite 写锁阻塞到 busy_timeout。
        with UnitOfWork(self._conn, payload_store=self._payload_store) as uow:
            self._delivery_repo.create_streaming_delivery(
                delivery_id=delivery_id,
                attempt_id=attempt_id,
                target=target,
                content_ref="",
                degradation_mode=degradation_mode,
                idempotency_key=idempotency_key,
                policy={
                    "throttle_ms": self._policy.throttle_ms,
                    "max_operations": self._policy.max_operations,
                },
                turn_id=turn.turn_id,
                conversation_id=input_meta.conversation_id,
                session_id=input_meta.session_id or getattr(turn, "session_id", ""),
                context=trace_context,
            )
            uow.commit()

        accumulated: list[str] = []
        operation_seq = 0
        platform_message_id: str | None = None
        last_push = 0.0
        finished = False
        _model_started = False

        try:
            async for delta, is_end in self._loop.run_stream(context, cancel_flag=cancel_flag):
                if is_end:
                    finished = True
                    break
                if not delta:
                    continue

                # 首 token 到达打点（模型流刚开始产出）
                if not _model_started:
                    _model_started = True
                    _bench_timing.checkpoint(
                        "streaming:first_delta",
                        extra={"first_delta": delta, "text_so_far": delta},
                    )

                accumulated.append(delta)
                full = "".join(accumulated)
                now = self._clock.now().timestamp()

                if platform_message_id is None:
                    # 首 token：创建占位消息
                    result = self._gateway.send_text(target_json, "…")
                    platform_message_id = result.platform_message_id or f"web-msg-{delivery_id}"
                    self._delivery_repo.mark_placeholder(
                        delivery_id,
                        attempt_id,
                        platform_message_id,
                    )
                    _first_token_ms = (now - _start_ts) * 1000
                    _bench_timing.checkpoint(
                        "streaming:placeholder_pushed",
                        extra={"first_token_ms": round(_first_token_ms, 1)},
                    )
                    _LOG.info(
                        "stream first-token latency=%.0fms conversation=%s turn=%s",
                        _first_token_ms,
                        input_meta.conversation_id,
                        turn.turn_id,
                    )
                    on_delta(input_meta.conversation_id, full, 0, False)
                else:
                    # 节流合并：达到间隔且未超最大操作数才发送 edit
                    throttled = (now - last_push) * 1000 >= self._policy.throttle_ms
                    if throttled and operation_seq < self._policy.max_operations:
                        operation_seq += 1
                        self._gateway.edit(target_json, platform_message_id, full, operation_seq)
                        self._delivery_repo.record_edit(
                            delivery_id,
                            attempt_id,
                            operation_seq,
                            platform_message_id,
                            "confirmed",
                        )
                        on_delta(input_meta.conversation_id, full, operation_seq, False)
                        last_push = now
        except Exception:
            _LOG.exception("StreamingDeliveryController: run_stream 异常，withdraw")
            self._delivery_repo.withdraw(delivery_id, attempt_id, "error")
            self._push_error(target_json, input_meta, "推理异常，请稍后重试")
            return None

        if not finished:
            # 取消或未完成 → 撤回占位
            _LOG.info(
                "StreamingDeliveryController: 未完成的流式回合已 withdraw delivery=%s",
                delivery_id,
            )
            self._delivery_repo.withdraw(delivery_id, attempt_id, "cancelled")
            # 若占位已创建则撤回占位；否则推一条取消提示
            if platform_message_id is not None:
                self._gateway.delete(target_json, platform_message_id, "cancelled")
            else:
                self._push_error(target_json, input_meta, "已取消")
            on_delta(input_meta.conversation_id, "", operation_seq, True)
            return None

        final_text = "".join(accumulated)
        _bench_timing.checkpoint(
            "streaming:model_done",
            extra={
                "accumulated_chars": len(final_text),
                "operation_seq": operation_seq,
            },
        )
        # 定稿前再推一次完整文本作为最终 edit（保证前端收到最终全文，
        # 即便最后一次节流 edit 因间隔未触发）。
        operation_seq += 1
        self._gateway.edit(
            target_json,
            platform_message_id,
            final_text,
            operation_seq,
            is_final=True,
        )
        self._delivery_repo.record_edit(
            delivery_id,
            attempt_id,
            operation_seq,
            platform_message_id,
            "confirmed",
        )
        _bench_timing.checkpoint("streaming:final_edit_sent")
        final_message_id = self._finalize(
            delivery_id,
            platform_message_id,
            turn,
            attempt,
            final_text,
            input_meta,
        )
        _bench_timing.checkpoint(
            "streaming:finalize_tx_done",
            extra={
                "final_message_id": final_message_id or "",
            },
        )
        if final_message_id is None and platform_message_id is not None:
            # 定稿事务失败（如 Turn 已被其他 worker 完成）→ 撤回占位，避免残留气泡
            self._gateway.delete(target_json, platform_message_id, "finalize_failed")
            self._push_error(target_json, input_meta, "定稿失败，请重试")
        on_delta(input_meta.conversation_id, final_text, operation_seq, True)
        _total_ms = (self._clock.now().timestamp() - _start_ts) * 1000
        _LOG.info(
            "stream turn finished: total=%.0fms first-token included, "
            "conversation=%s turn=%s chars=%d ops=%d",
            _total_ms,
            input_meta.conversation_id,
            turn.turn_id,
            len(final_text),
            operation_seq,
        )
        return final_message_id

    def _push_error(self, target_json: str, input_meta: StreamInputMeta, message: str) -> None:
        """向浏览器推送一条错误/状态提示消息（经网关同步入队）。"""
        try:
            result = self._gateway.send_text(target_json, message)
            _LOG.info(
                "push_error: %s (status=%s) conversation=%s",
                message,
                result.status,
                input_meta.conversation_id,
            )
        except Exception:
            _LOG.warning("push_error failed: %s", message, exc_info=True)

    def _build_target(
        self,
        meta: StreamInputMeta,
        delivery_id: str,
        attempt_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        reply_route = meta.reply_route or {}
        adapter_id = reply_route.get("channel_instance_id") or reply_route.get("adapter_id") or ""
        target_endpoint_ref = reply_route.get("target_endpoint_ref") or adapter_id or ""
        # 路由键用平台会话 ID（与 ChannelGateway 非流式投递、Web WS 订阅键一致），
        # 而非内部 DB conversation_id（UUID），否则事件会落到信箱而非实时队列。
        conversation_id = reply_route.get("platform_conversation_id") or meta.conversation_id
        return {
            "delivery_id": delivery_id,
            "idempotency_key": idempotency_key,
            "reply_route": reply_route,
            "adapter_id": adapter_id,
            "target_endpoint_ref": target_endpoint_ref,
            "conversation_id": conversation_id,
        }

    def _finalize(
        self,
        delivery_id: str,
        platform_message_id: str | None,
        turn: Any,
        attempt: Any,
        text: str,
        meta: StreamInputMeta,
    ) -> str | None:
        """短事务：写入最终 Assistant Message + 定稿 Delivery + 完成 Turn。"""
        with UnitOfWork(self._conn, payload_store=self._payload_store) as uow:
            parts = [ContentPart(content_type="text", inline_data=text)]
            message = Message(
                conversation_id=meta.conversation_id,
                session_id=meta.session_id or turn.session_id,
                sender_principal_id=meta.principal_id or "cogito",
                sender_endpoint_id=meta.endpoint_id or "cogito",
                role=MessageRole.assistant,
                direction=MessageDirection.outbound,
                content_parts=parts,
                reply_to_message_id=turn.input_message_id,
                reply_route=meta.reply_route,
                capability_snapshot=meta.capability_snapshot,
            )
            if message.receive_sequence == 0:
                message.receive_sequence = uow.message.next_receive_sequence(meta.conversation_id)

            uow.message.insert(message)
            for part in message.content_parts:
                uow.message.insert_content_part(part, message.message_id)

            delivery_completed = self._delivery_repo.finish_streaming(
                delivery_id,
                message.message_id,
                platform_message_id or "",
                text,
            )
            completion_context = EventContext(
                trace_id=delivery_completed.context.trace_id,
                correlation_id=delivery_completed.context.correlation_id,
                causation_id=delivery_completed.event_id,
                actor_id=delivery_completed.context.actor_id,
                principal_id=delivery_completed.context.principal_id,
                conversation_id=delivery_completed.context.conversation_id,
                session_id=delivery_completed.context.session_id,
                turn_id=turn.turn_id,
                attempt_id=attempt.attempt_id,
            )

            # Context assembly and other causal observations may be appended to
            # the Turn stream after it was claimed.  Completion must replay the
            # current aggregate version rather than reuse the stale claim view.
            turn_stream = EventStore(self._conn).read_stream("turn", turn.turn_id)
            replayed_turn = replay_turn(turn_stream, turn.turn_id)
            current_turn_version = (
                replayed_turn.stream_version if replayed_turn is not None else turn.version
            )
            ok = self._dispatcher.complete(
                turn.turn_id,
                attempt.attempt_id,
                current_turn_version,
                worker_id=attempt.worker_id,
                lease_version=attempt.lease_version,
                final_message_id=message.message_id,
                event_context=completion_context,
                event_producer="streaming-delivery",
                event_summary="Turn completed with streamed response",
                event_attributes={
                    "final_message_id": message.message_id,
                    "delivery_id": delivery_id,
                },
                _uow=uow,
            )
            if not ok:
                _LOG.warning(
                    "StreamingDeliveryController: Turn 已完成或版本不匹配，回滚 delivery=%s",
                    delivery_id,
                )
                return None

            uow.commit()

        return message.message_id

    def _event_context_for_turn(
        self, turn: Any, attempt: Any, meta: StreamInputMeta
    ) -> EventContext:
        turn_id = getattr(turn, "turn_id", "")
        prior = EventStore(self._conn).read_stream("turn", turn_id)
        source_event = next((event for event in reversed(prior) if event.context.trace_id), None)
        source = source_event.context if source_event is not None else EventContext()
        return EventContext(
            trace_id=source.trace_id,
            correlation_id=source.correlation_id,
            causation_id=source_event.event_id if source_event is not None else source.causation_id,
            actor_id=source.actor_id,
            principal_id=meta.principal_id or source.principal_id,
            conversation_id=meta.conversation_id or source.conversation_id,
            session_id=meta.session_id or source.session_id or getattr(turn, "session_id", ""),
            turn_id=turn_id,
            attempt_id=getattr(attempt, "attempt_id", ""),
        )
