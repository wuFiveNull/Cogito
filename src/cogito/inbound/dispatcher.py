"""Inbound Dispatcher — 将 Inbound 转换为 ChannelEnvelope 并交给 InboundService。

数据流:
    Inbound → ChannelEnvelope → InboundService.accept()

QQ-ONEBOT-E2E-01: 传递 metadata 中的 conversation_type / trust_label / capability，
让 InboundService 可以正确建模群聊 vs 私聊。
"""

from __future__ import annotations

from datetime import UTC, datetime

from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
from cogito.contracts.inbound import Inbound
from cogito.service.inbound_service import InboundService


class InboundDispatcher:
    """Inbound 分发器（实现 contracts.inbound.InboundHandler Protocol）。

    将 Channel Adapter 的统一 Inbound 消息转换为 Cogito Core 的 ChannelEnvelope，
    然后调用 InboundService.accept() 完成入站事务。
    """

    def __init__(self, inbound_service: InboundService) -> None:
        self._inbound_service = inbound_service

    # PLAN-10 M2: 实现 InboundHandler Protocol

    async def dispatch(self, inbound: Inbound) -> None:
        """分发一条 Inbound 消息到 Agent Core。"""
        envelope = self._build_envelope(inbound)
        self._inbound_service.accept(envelope)

    def _build_envelope(self, inbound: Inbound) -> ChannelEnvelope:
        """将 Inbound 转换为 ChannelEnvelope。"""
        content_parts = [
            {
                "content_type": c.type,
                "inline_data": c.data,
                "mime": c.mime,
                "name": c.name,
                # Older adapters may still pass None; normalize at the contract
                # boundary so database defaults are never bypassed by an
                # explicit SQL NULL.
                "size": int(c.size or 0),
                "trust_label": "unverified",
                "metadata": {
                    "mime": c.mime or "",
                    "name": c.name or "",
                },
            }
            for c in inbound.content
        ]

        # 优先从 metadata 取 target_endpoint_ref（QQ OneBot Facade 提供稳定 ID）
        metadata_target_ref = inbound.metadata.get("target_endpoint_ref")

        reply_route = ReplyRoute(
            channel_instance_id=inbound.channel_instance_id,
            platform_conversation_id=inbound.conversation_id,
            reply_to_platform_message_id=inbound.route.source_message_id,
            target_endpoint_ref=metadata_target_ref
            or (f"{inbound.channel}:{inbound.sender_id}" if inbound.sender_id else ""),
        )

        return ChannelEnvelope(
            channel_type=inbound.channel,
            channel_instance_id=inbound.channel_instance_id,
            platform_sender_id=inbound.sender_id,
            platform_conversation_id=inbound.conversation_id,
            platform_message_id=(inbound.message_id or inbound.route.source_message_id),
            content_parts=content_parts,
            reply_route=reply_route,
            sender_endpoint_ref=inbound.metadata.get(
                "sender_endpoint_ref",
                f"{inbound.channel}:{inbound.sender_id}",
            ),
            conversation_endpoint_ref=inbound.metadata.get(
                "conversation_endpoint_ref",
                f"{inbound.channel}:{inbound.conversation_id}",
            ),
            capability_snapshot=inbound.metadata.get("capability", {}),
            trust_label=inbound.metadata.get("trust_label", "unverified"),
            metadata={
                "conversation_type": inbound.metadata.get("conversation_type", "private"),
                "group_id": inbound.metadata.get("group_id", ""),
            },
            received_at=datetime.now(UTC).isoformat(),
        )
