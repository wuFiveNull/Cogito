"""InboundService — 入站消息处理应用服务。

实现 P2 核心事务流程：
1. 校验 ChannelEnvelope
2. Inbox 幂等去重
3. 解析/创建 Principal、Endpoint、Conversation、Session
4. 分配 receive_sequence
5. 写入 Message 与 ContentPart（含回复路由快照）
6. 创建 Turn（accepted → queued）
7. 追加规范 Event
8. 返回 message_id、turn_id
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cogito.bench import timing as _bench_timing
from cogito.contracts.envelope import ChannelEnvelope
from cogito.domain.conversation import (
    ContextPartitionPolicy,
    Conversation,
    ConversationStatus,
    ConversationType,
    Session,
    SessionStatus,
)
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.message import ContentPart, Message, MessageDirection, MessageRole
from cogito.domain.principal import (
    Endpoint,
    EndpointStatus,
    Principal,
    PrincipalStatus,
    PrincipalType,
)
from cogito.domain.state_machines import validate_transition_turn
from cogito.domain.turn import Turn, TurnStatus
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.event_store import EventStore
from cogito.store.event_replay import replay_turn
from cogito.store.repositories import InboxRecord

_LOGGER = logging.getLogger("cogito.inbound")


@dataclass
class AcceptInboundResult:
    """入站事务返回值。"""

    message_id: str = ""
    turn_id: str = ""
    is_new: bool = True


class InboundService:
    """入站消息处理服务。

    本阶段不调用模型、网络、Channel、MCP 或 Tool。
    不创建 RunAttempt。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        notify: callable | None = None,
        *,
        asset_service: Any | None = None,
        vision_service: Any | None = None,
        payload_store: Any | None = None,
        max_assets_per_message: int = 4,
        drift_preemption: Any = None,  # None ⇒ 不发射抢占(P0-05 默认关闭)
    ) -> None:
        self._conn = conn
        # 入站新建 Turn 后的唤醒回调（用于即时唤醒后台 worker，消除轮询睡眠）
        self._notify = notify
        self._asset_service = asset_service
        self._vision_service = vision_service
        if payload_store is not None:
            self._payload_store = payload_store
        else:
            from cogito.infrastructure.payload_store import PayloadStore
            import tempfile

            self._payload_store = PayloadStore(tempfile.mkdtemp(prefix="cogito-inbound-"), conn)
        self._max_assets_per_message = max_assets_per_message
        self._drift_preemption = drift_preemption

    def accept(self, envelope: ChannelEnvelope) -> AcceptInboundResult:
        """接受入站消息并创建 Turn。

        幂等保证：同一 (channel_instance_id, platform_message_id)
        首次返回新 ID，重复返回已有 ID。
        """
        _bench_timing.checkpoint("inbound:enter")
        producer = f"channel:{envelope.channel_type or 'unknown'}"
        inbound_idempotency_key = (
            f"inbound-message:{envelope.channel_instance_id}:"
            f"{envelope.platform_message_id or envelope.message_id}"
        )
        with UnitOfWork(self._conn, payload_store=self._payload_store) as uow:
            # ── 1. Inbox 幂等去重 ──
            platform_event_id = envelope.platform_message_id or envelope.message_id
            accepted_event = (
                EventStore(self._conn).find_idempotent(producer, inbound_idempotency_key)
                if self._payload_store is not None
                else None
            )
            if accepted_event is not None:
                return AcceptInboundResult(
                    message_id=accepted_event.stream_id,
                    turn_id=self._find_turn_id_by_message(accepted_event.stream_id, uow),
                    is_new=False,
                )
            if self._payload_store is None:
                existing = uow.inbox.find(envelope.channel_instance_id, platform_event_id)
                if existing is not None and existing.message_id:
                    return AcceptInboundResult(
                        message_id=existing.message_id,
                        turn_id=self._find_turn_id_by_message(existing.message_id, uow),
                        is_new=False,
                    )

            # ── 2. 解析/创建 Principal ──
            # 优先使用 sender_endpoint_ref，兼容旧测试：Ref 为空时退回现有 platform ID
            principal: Principal | None = None
            if envelope.sender_endpoint_ref:
                endpoint_by_ref = uow.endpoint.find_by_ref(envelope.sender_endpoint_ref)
                if endpoint_by_ref is not None:
                    principal = uow.principal.find(endpoint_by_ref.principal_id)

            if principal is None:
                principal = uow.principal.find_by_platform(
                    envelope.channel_type,
                    envelope.platform_sender_id,
                )

            if principal is None:
                principal = Principal(
                    principal_type=PrincipalType.external_user,
                    status=PrincipalStatus.active,
                )
                principal = uow.principal.insert(principal)

            # ── 3. 解析/创建 Endpoint ──
            endpoint: Endpoint | None = None
            if envelope.sender_endpoint_ref:
                endpoint = uow.endpoint.find_by_ref(envelope.sender_endpoint_ref)

            if endpoint is None:
                endpoint = uow.endpoint.find_by_platform(
                    envelope.channel_instance_id,
                    envelope.platform_sender_id,
                )

            if endpoint is None:
                endpoint = Endpoint(
                    channel_type=envelope.channel_type,
                    channel_instance_id=envelope.channel_instance_id,
                    platform_account_id=envelope.platform_sender_id,
                    principal_id=principal.principal_id,
                    endpoint_ref=envelope.sender_endpoint_ref or "",
                    status=EndpointStatus.active,
                )
                endpoint = uow.endpoint.insert(endpoint)
                # An idempotent endpoint append can return the aggregate won
                # by another worker; continue with that canonical principal.
                principal = uow.principal.find(endpoint.principal_id) or principal

            # ── 4. 解析/创建 Conversation ──
            conversation: Conversation | None = None
            if envelope.conversation_endpoint_ref:
                conversation = uow.conversation.find_by_endpoint_ref(
                    envelope.conversation_endpoint_ref,
                )

            if conversation is None:
                conversation = uow.conversation.find_by_platform(
                    endpoint.endpoint_id,
                    envelope.platform_conversation_id,
                )

            if conversation is None:
                # QQ-ONEBOT-E2E-01: 支持群聊/私聊类型区分
                conv_type_str = (
                    envelope.metadata.get("conversation_type", "private")
                    if envelope.metadata
                    else "private"
                )
                conversation_type = (
                    ConversationType.group if conv_type_str == "group" else ConversationType.private
                )
                conversation = Conversation(
                    conversation_endpoint_id=endpoint.endpoint_id,
                    platform_conversation_id=envelope.platform_conversation_id,
                    conversation_endpoint_ref=envelope.conversation_endpoint_ref or "",
                    conversation_type=conversation_type,
                    context_partition_policy=ContextPartitionPolicy.isolated,
                    status=ConversationStatus.active,
                )
                uow.conversation.insert(conversation)

            # ── 5. 解析/创建 Session ──
            context_key = conversation.conversation_id  # isolated → partition = conversation
            session = uow.session.find_active(
                conversation.conversation_id,
                context_key,
            )
            if session is None:
                session = Session(
                    conversation_id=conversation.conversation_id,
                    context_partition_key=context_key,
                    status=SessionStatus.active,
                )
                uow.session.insert(session)

            # ── 6. 分配单调 receive_sequence ──
            seq = uow.message.next_receive_sequence(conversation.conversation_id)

            # ── 7. 创建 Message ──
            content_parts = []
            asset_count = 0
            for ordinal, cp in enumerate(envelope.content_parts):
                part = ContentPart(
                    content_type=cp.get("content_type", "text"),
                    inline_data=cp.get("inline_data", ""),
                    payload_ref=cp.get("payload_ref"),
                    size=cp.get("size", 0),
                    sha256=cp.get("sha256", ""),
                    metadata={
                        **cp.get("metadata", {}),
                        **({"mime": cp.get("mime")} if cp.get("mime") else {}),
                        **({"name": cp.get("name")} if cp.get("name") else {}),
                    },
                    trust_label=cp.get("trust_label", "unverified"),
                    ordinal=ordinal,
                )
                is_image = part.content_type == "image" or part.content_type.startswith("image/")
                if self._asset_service is not None and is_image:
                    if asset_count >= self._max_assets_per_message:
                        part.inline_data = ""
                        part.metadata = {**part.metadata, "asset_error": "too_many_assets"}
                    else:
                        try:
                            asset = self._asset_service.materialize_part(
                                part,
                                principal_id=principal.principal_id,
                            )
                            if asset is not None:
                                asset_count += 1
                        except Exception as exc:
                            # Invalid binary input must not block the text-only Turn.
                            part.inline_data = ""
                            part.metadata = {
                                **part.metadata,
                                "asset_error": type(exc).__name__,
                            }
                content_parts.append(part)

            message = Message(
                conversation_id=conversation.conversation_id,
                session_id=session.session_id,
                sender_principal_id=principal.principal_id,
                sender_endpoint_id=endpoint.endpoint_id,
                role=MessageRole.user,
                direction=MessageDirection.inbound,
                content_parts=content_parts,
                platform_message_id=platform_event_id,
                receive_sequence=seq,
                trust_label=envelope.trust_label,
                reply_route=envelope.reply_route.to_dict() if envelope.reply_route else None,
                capability_snapshot=envelope.capability_snapshot or None,
                created_at=datetime.fromisoformat(envelope.received_at)
                if envelope.received_at
                else None,
            )
            uow.message.insert(message)
            for part in content_parts:
                uow.message.insert_content_part(part, message.message_id)
                if self._asset_service is not None:
                    self._asset_service.link_part(message.message_id, part)

            # ── 8. 创建 Turn（accepted → queued）──
            turn = Turn(
                session_id=session.session_id,
                input_message_id=message.message_id,
                status=TurnStatus.accepted,
            )
            trace_id = envelope.message_id or platform_event_id
            message_context = EventContext(
                trace_id=trace_id,
                correlation_id=trace_id,
                causation_id=trace_id,
                actor_id=principal.principal_id,
                principal_id=principal.principal_id,
                conversation_id=conversation.conversation_id,
                session_id=session.session_id,
                turn_id=turn.turn_id,
            )
            events = EventStore(self._conn)
            accepted = events.append(
                Event(
                    event_type="interaction.message.accepted",
                    stream_type="message",
                    stream_id=message.message_id,
                    producer=producer,
                    event_class=EventClass.DOMAIN,
                    context=message_context,
                    summary="Inbound message accepted",
                    attributes={
                        "channel_type": envelope.channel_type,
                        "channel_instance_id": envelope.channel_instance_id,
                        "platform_message_id": platform_event_id,
                        "content_part_count": len(content_parts),
                        "receive_sequence": seq,
                    },
                    outcome="accepted",
                    occurred_at=int(datetime.now(UTC).timestamp() * 1000),
                    idempotency_key=inbound_idempotency_key,
                )
            )
            if accepted.stream_id != message.message_id:
                # A competing worker committed the same channel Event first.
                # Returning without commit rolls this provisional payload/Event
                # back; only the immutable winning aggregate remains.
                return AcceptInboundResult(
                    message_id=accepted.stream_id,
                    turn_id=self._find_turn_id_by_message(accepted.stream_id, uow),
                    is_new=False,
                )
            turn_accepted = uow.turn.insert(
                turn,
                event_context=EventContext(
                    trace_id=message_context.trace_id,
                    correlation_id=message_context.correlation_id,
                    causation_id=accepted.event_id,
                    actor_id=message_context.actor_id,
                    principal_id=message_context.principal_id,
                    conversation_id=message_context.conversation_id,
                    session_id=message_context.session_id,
                    turn_id=message_context.turn_id,
                ),
                event_producer=producer,
            )
            # 状态推进：accepted → queued
            validate_transition_turn(turn.turn_id, turn.status, TurnStatus.queued)
            ok = uow.turn.update_status(
                turn.turn_id,
                TurnStatus.queued,
                turn.version,
                event_context=EventContext(
                    trace_id=message_context.trace_id,
                    correlation_id=message_context.correlation_id,
                    causation_id=turn_accepted.event_id,
                    actor_id=message_context.actor_id,
                    principal_id=message_context.principal_id,
                    conversation_id=message_context.conversation_id,
                    session_id=message_context.session_id,
                    turn_id=message_context.turn_id,
                ),
                event_producer=producer,
            )
            if not ok:
                raise RuntimeError(f"Turn status transition failed: {turn.turn_id}")

            # ── 9. 记录 Inbox ──
            if self._payload_store is None:
                uow.inbox.insert(
                    InboxRecord(
                        channel_instance_id=envelope.channel_instance_id,
                        platform_event_id=platform_event_id,
                        status="processed",
                        message_id=message.message_id,
                        received_at=datetime.now(UTC).isoformat(),
                    )
                )

            uow.commit()
        _bench_timing.checkpoint("inbound:commit_done", extra={"turn_id": turn.turn_id})

        if self._vision_service is not None:
            try:
                self._vision_service.request_message_assets(message.message_id)
            except Exception:
                # Vision scheduling is fail-open for the user Turn.
                pass

        # 唤醒后台 worker（若存在）——即时处理新 Turn，无需等待轮询间隔
        if self._notify is not None:
            try:
                self._notify()
            except Exception:
                pass

        result = AcceptInboundResult(
            message_id=message.message_id,
            turn_id=turn.turn_id,
            is_new=True,
        )

        # PLAN-17 R4 P0-05：入站 Turn 提交后发射 Drift 抢占信号。
        # 持有 active Drift Lease 的 Attempt 在下一安全点消费该信号并暂停；
        # 信号不在读取时立即清除，避免错误 Worker 吃掉信号。
        if result.is_new and self._drift_preemption is not None:
            try:
                from cogito.service.drift_preemption import request_preemption

                request_preemption(
                    self._conn, self._drift_preemption.default_principal_id, "inbound_turn"
                )
            except Exception:
                _LOGGER.warning("drift preemption signal emit failed", exc_info=True)

        return result

    def _find_turn_id_by_message(self, message_id: str, uow: UnitOfWork) -> str:
        """通过 input_message_id 查找已创建的 turn_id。"""
        events = EventStore(uow._conn).read_stream_type("turn")
        for turn_id in {event.stream_id for event in events}:
            if (state := replay_turn(events, turn_id)) is not None and state.input_message_id == message_id:
                return turn_id
        return ""
