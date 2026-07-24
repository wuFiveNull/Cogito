"""Turn completion — 原子完成 Turn 并写入所有下游产物。

单事务完成：
1. 创建 Assistant Message + ContentPart
2. 创建 Delivery（待发送，target_snapshot 来自输入消息 reply_route）
3. 写入 TurnCompleted Event Outbox
4. 标记 RunAttempt → succeeded
5. 标记 Turn → completed
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from cogito.contracts.clock import Clock, ProductionClock, epoch_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.message import ContentPart, Message, MessageDirection, MessageRole
from cogito.domain.turn import RunAttempt, Turn
from cogito.infrastructure.payload_store import PayloadStore
from cogito.service.delivery_effect_payload import (
    DeliveryEffectPayload,
    store_delivery_effect_payload,
)
from cogito.service.dispatcher import Dispatcher
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.event_message_reader import EventMessageReader
from cogito.store.event_replay import replay_turn
from cogito.store.event_store import EventStore


def _parse_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class TurnCompletionService:
    """执行完成后台事务 —— 原子写入最终 Message、Delivery、Outbox 并完成 Turn。"""

    STUB_REPLY_TEXT = "Hello! I'm Cogito, your personal agent. I'm currently in stub mode — this is an automated reply."

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock | None = None,
        *,
        effect_payload_store: PayloadStore | None = None,
        message_payload_store: PayloadStore | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or ProductionClock()
        self._dispatcher = Dispatcher(conn, clock=self._clock)
        self._effect_payload_store = effect_payload_store
        self._message_payload_store = message_payload_store or effect_payload_store
        if self._message_payload_store is not None:
            self._message_reader = EventMessageReader(conn, self._message_payload_store)
        else:
            from cogito.infrastructure.payload_store import PayloadStore
            import tempfile

            self._message_payload_store = PayloadStore(tempfile.mkdtemp(prefix="cogito-msg-"), conn)
            self._message_reader = EventMessageReader(conn, self._message_payload_store)

    def complete_reply(
        self,
        turn: Turn,
        attempt: RunAttempt,
        reply_text: str,
    ) -> str | None:
        """正式回复完成入口。

        自行读取输入 Message 的 conversation_id、session_id、reply_route、capability_snapshot。
        事务内完成：Assistant Message + ContentPart + Delivery + Outbox + Attempt + Turn。

        Args:
            turn: 当前 Turn。
            attempt: 当前 RunAttempt。
            reply_text: 模型回复文本。

        Returns: final_message_id 或 None（失败时回滚）。
        """
        # 读取输入消息的元数据
        input_message = self._input_message(turn.input_message_id)
        conversation_id = str(input_message.get("conversation_id", ""))
        session_id = str(input_message.get("session_id", "") or turn.session_id or "")
        sender_principal_id = str(input_message.get("sender_principal_id", ""))
        sender_endpoint_id = str(input_message.get("sender_endpoint_id", ""))
        reply_route = input_message.get("reply_route", {})
        capability_snapshot = input_message.get("capability_snapshot", {})
        if not isinstance(reply_route, dict):
            reply_route = {}
        if not isinstance(capability_snapshot, dict):
            capability_snapshot = {}

        # 构建 Assistant Message
        parts = [
            ContentPart(
                content_type="text",
                inline_data=reply_text,
            ),
        ]

        message = Message(
            conversation_id=conversation_id,
            session_id=session_id or turn.session_id,
            sender_principal_id=sender_principal_id or "cogito",
            sender_endpoint_id=sender_endpoint_id or "cogito",
            role=MessageRole.assistant,
            direction=MessageDirection.outbound,
            content_parts=parts,
            reply_to_message_id=turn.input_message_id,
            reply_route=reply_route,
            capability_snapshot=capability_snapshot,
        )

        return self._complete(
            turn=turn,
            attempt=attempt,
            message=message,
            channel_type="",
            delivery_target="",
            endpoint_id=sender_endpoint_id,
            principal_id=sender_principal_id,
            reply_route=reply_route,
        )

    def complete_with_stub(
        self,
        turn: Turn,
        attempt: RunAttempt,
        *,
        conversation_id: str = "",
        session_id: str = "",
        endpoint_id: str = "",
        principal_id: str = "",
        channel_type: str = "test",
        delivery_target: str = "",
    ) -> str | None:
        """使用 Stub Agent 完成 Turn（固定回复文本）。

        Returns: final_message_id 或 None（失败时回滚）
        """
        # 生成 Stub Assistant 回复
        parts = [
            ContentPart(
                content_type="text",
                inline_data=self.STUB_REPLY_TEXT,
            ),
        ]

        message = Message(
            conversation_id=conversation_id,
            session_id=session_id or turn.session_id,
            sender_principal_id="cogito",
            sender_endpoint_id="cogito",
            role=MessageRole.assistant,
            direction=MessageDirection.outbound,
            content_parts=parts,
            reply_to_message_id=turn.input_message_id,
        )

        return self._complete(
            turn=turn,
            attempt=attempt,
            message=message,
            channel_type=channel_type,
            delivery_target=delivery_target,
            endpoint_id=endpoint_id,
            principal_id=principal_id,
        )

    def _complete(
        self,
        turn: Turn,
        attempt: RunAttempt,
        message: Message,
        channel_type: str,
        delivery_target: str,
        endpoint_id: str,
        principal_id: str,
        reply_route: dict | None = None,
    ) -> str | None:
        """原子完成事务。

        Args:
            reply_route: 来自输入消息的回复路由快照。创建 Delivery 时使用。
        """
        with UnitOfWork(self._conn, payload_store=self._message_payload_store) as uow:
            # 为 outbound 消息分配 receive_sequence
            if message.receive_sequence == 0:
                message.receive_sequence = uow.message.next_receive_sequence(
                    message.conversation_id
                )

            # 1. 写入 Assistant Message
            uow.message.insert(message)
            for part in message.content_parts:
                uow.message.insert_content_part(part, message.message_id)

            # 2. Append the canonical Delivery request. The active worker
            # consumes this Event, never a mutable deliveries row.
            delivery_id = ""
            delivery_requested_id = ""
            turn_context = self._event_context_for_turn(
                turn,
                attempt,
                conversation_id=message.conversation_id,
                session_id=message.session_id,
                principal_id=principal_id,
                causation_id="",
            )
            if delivery_target or reply_route:
                import uuid

                delivery_id = uuid.uuid4().hex
                now_int = epoch_ms(self._clock.now())
                target: dict[str, Any] = {"target": delivery_target} if delivery_target else {}
                if reply_route:
                    target["reply_route"] = reply_route
                # 携带结构化字段，方便 Gateway 构造 ChannelSendRequest
                target["delivery_id"] = delivery_id
                target["idempotency_key"] = f"delivery_{message.message_id}"
                # adapter_id 优先从 reply_route 取
                adapter_id = ""
                if isinstance(reply_route, dict):
                    adapter_id = (
                        reply_route.get("channel_instance_id")
                        or reply_route.get("adapter_id")
                        or ""
                    )
                target["adapter_id"] = adapter_id
                # target_endpoint_ref
                target_endpoint_ref = (
                    (
                        reply_route.get("target_endpoint_ref")
                        if isinstance(reply_route, dict)
                        else None
                    )
                    or (
                        reply_route.get("channel_instance_id")
                        if isinstance(reply_route, dict)
                        else None
                    )
                    or ""
                )
                target["target_endpoint_ref"] = target_endpoint_ref
                idempotency_key = f"delivery_{message.message_id}"
                payload_ref = message.message_id
                payload_hash = ""
                payload_kind = "message-reference.v1"
                if self._effect_payload_store is not None:
                    payload_ref, payload_hash = store_delivery_effect_payload(
                        self._effect_payload_store,
                        DeliveryEffectPayload(
                            delivery_id=delivery_id,
                            target_snapshot=target,
                            content=message.content_parts[0].inline_data,
                            content_ref=message.message_id,
                            idempotency_key=idempotency_key,
                        ),
                    )
                    payload_kind = "delivery-effect.v2"
                delivery_requested = EventStore(self._conn).append(
                    Event(
                        event_type="delivery.requested",
                        stream_type="delivery",
                        stream_id=delivery_id,
                        producer="turn-completion",
                        event_class=EventClass.DOMAIN,
                        context=turn_context,
                        summary="Assistant response delivery requested",
                        attributes={"effect_payload_kind": payload_kind},
                        payload_ref=payload_ref,
                        payload_hash=payload_hash,
                        outcome="pending",
                        occurred_at=now_int,
                        idempotency_key=f"delivery-request:{idempotency_key}",
                    )
                )
                delivery_requested_id = delivery_requested.event_id

            # 3. 完成 Turn + Attempt（在同一 UoW 中，不自成事务）
            completion_context = self._event_context_for_turn(
                turn,
                attempt,
                conversation_id=message.conversation_id,
                session_id=message.session_id,
                principal_id=principal_id,
                causation_id=delivery_requested_id,
            )
            # Context and model observations can be appended after the Turn is
            # claimed.  Complete against the current aggregate stream version,
            # not the stale in-memory claim projection.
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
                event_producer="turn-completion",
                event_summary="Turn completed with assistant response",
                event_attributes={
                    "final_message_id": message.message_id,
                    "delivery_id": delivery_id,
                },
                _uow=uow,
            )
            if not ok:
                # Turn 或 Attempt 状态已变化（版本不匹配、Lease 过期等）
                return None  # 不提交，回滚 Message/Delivery

            uow.commit()

        return message.message_id

    def _input_message(self, message_id: str) -> dict[str, Any]:
        """Read reply metadata from the canonical message Event."""
        if self._message_reader is not None:
            event_message = self._message_reader.get(message_id)
            if event_message is not None:
                return event_message
        return {}

    def _event_context_for_turn(
        self,
        turn: Turn,
        attempt: RunAttempt,
        *,
        conversation_id: str,
        session_id: str,
        principal_id: str,
        causation_id: str,
    ) -> EventContext:
        """Continue the accepted Turn trace without reconstructing a projection."""
        prior = EventStore(self._conn).read_stream("turn", turn.turn_id)
        source_event = next((event for event in reversed(prior) if event.context.trace_id), None)
        source = source_event.context if source_event is not None else EventContext()
        return EventContext(
            trace_id=source.trace_id,
            correlation_id=source.correlation_id,
            causation_id=causation_id or (source_event.event_id if source_event else source.causation_id),
            actor_id=source.actor_id,
            principal_id=principal_id or source.principal_id,
            conversation_id=conversation_id or source.conversation_id,
            session_id=session_id or source.session_id or turn.session_id,
            turn_id=turn.turn_id,
            attempt_id=attempt.attempt_id,
        )


class StubAgent:
    """固定回复 Agent —— 不调用真实模型，用于测试和开发。"""

    REPLY_TEXT = TurnCompletionService.STUB_REPLY_TEXT

    @staticmethod
    def generate_reply(turn: Turn, input_message: Message | None = None) -> list[ContentPart]:
        return [
            ContentPart(content_type="text", inline_data=StubAgent.REPLY_TEXT),
        ]
