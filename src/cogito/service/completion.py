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

from cogito.domain.events import DomainEvent
from cogito.domain.message import ContentPart, Message, MessageDirection, MessageRole
from cogito.domain.turn import RunAttempt, Turn
from cogito.runtime.clock import Clock, ProductionClock
from cogito.service.dispatcher import Dispatcher
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.time_utils import epoch_ms


class TurnCompletionService:
    """执行完成后台事务 —— 原子写入最终 Message、Delivery、Outbox 并完成 Turn。"""

    STUB_REPLY_TEXT = "Hello! I'm Cogito, your personal agent. I'm currently in stub mode — this is an automated reply."

    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None) -> None:
        self._conn = conn
        self._clock = clock or ProductionClock()
        self._dispatcher = Dispatcher(conn, clock=self._clock)

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
        input_row = self._conn.execute(
            "SELECT conversation_id, session_id, sender_principal_id, sender_endpoint_id, "
            "reply_route_json, capability_snapshot_json "
            "FROM messages WHERE message_id=?",
            (turn.input_message_id,),
        ).fetchone()

        conversation_id = input_row["conversation_id"] if input_row else ""
        session_id = input_row["session_id"] if input_row else (turn.session_id or "")
        sender_principal_id = input_row["sender_principal_id"] if input_row else ""
        sender_endpoint_id = input_row["sender_endpoint_id"] if input_row else ""
        reply_route_json = input_row["reply_route_json"] if input_row else "{}"
        capability_snapshot_json = input_row["capability_snapshot_json"] if input_row else "{}"

        reply_route = json.loads(reply_route_json) if isinstance(reply_route_json, str) else reply_route_json
        capability_snapshot = json.loads(capability_snapshot_json) if isinstance(capability_snapshot_json, str) else capability_snapshot_json

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
        with UnitOfWork(self._conn) as uow:
            # 1. 写入 Assistant Message
            uow.message.insert(message)
            for part in message.content_parts:
                uow.message.insert_content_part(part, message.message_id)

            # 2. 写入 Delivery（pending 状态，等待 Outbox Worker 发送）
            delivery_id = ""
            if delivery_target or reply_route:
                import uuid
                delivery_id = uuid.uuid4().hex
                now_int = epoch_ms(self._clock.now())
                target = {"target": delivery_target} if delivery_target else {}
                if reply_route:
                    target["reply_route"] = reply_route
                self._conn.execute(
                    "INSERT INTO deliveries (delivery_id, target_snapshot, content_ref, "
                    "status, idempotency_key, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (delivery_id,
                     json.dumps(target),
                     message.message_id,
                     "pending",
                     f"delivery_{message.message_id}",
                     now_int),
                )

            # 3. 写入 TurnCompleted Event Outbox
            now = self._clock.now()
            uow.outbox.insert(DomainEvent(
                event_type="TurnCompleted",
                aggregate_type="turn",
                aggregate_id=turn.turn_id,
                aggregate_version=turn.version + 1,
                payload={
                    "turn_id": turn.turn_id,
                    "message_id": message.message_id,
                    "delivery_id": delivery_id,
                },
                occurred_at=now,
                correlation_id=turn.turn_id,
                causation_id=turn.turn_id,
                origin="agent",
            ))

            # 4. 完成 Turn + Attempt（在同一 UoW 中，不自成事务）
            ok = self._dispatcher.complete(
                turn.turn_id,
                attempt.attempt_id,
                turn.version,
                worker_id=attempt.worker_id,
                lease_version=attempt.lease_version,
                final_message_id=message.message_id,
                _uow=uow,
            )
            if not ok:
                # Turn 或 Attempt 状态已变化（版本不匹配、Lease 过期等）
                return None  # 不提交，回滚 Message/Delivery

            uow.commit()

        return message.message_id


class StubAgent:
    """固定回复 Agent —— 不调用真实模型，用于测试和开发。"""

    REPLY_TEXT = TurnCompletionService.STUB_REPLY_TEXT

    @staticmethod
    def generate_reply(turn: Turn, input_message: Message | None = None) -> list[ContentPart]:
        return [
            ContentPart(content_type="text", inline_data=StubAgent.REPLY_TEXT),
        ]
