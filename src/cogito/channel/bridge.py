"""LangBot Event → Cogito Inbound 桥接转换。"""
from __future__ import annotations

from cogito.channel.vendor.langbot.compatibility import events as lb_events
from cogito.channel.vendor.langbot.compatibility import message as lb_message
from cogito.inbound.models import Inbound, InboundContent, InboundRoute


async def langbot_event_to_inbound(
    event: lb_events.MessageEvent,
    channel_type: str,
    instance_id: str,
) -> Inbound:
    """将 LangBot MessageEvent 转换为 Cogito Inbound。

    这是 Phase 1 的兼容转换。Phase 2 中适配器将直接生成 Inbound，
    跳过此转换层。
    """
    # ── 提取会话和发送者 ID ──
    if isinstance(event, lb_events.GroupMessage):
        conversation_id = str(event.sender.group.id) if event.sender and event.sender.group else ""
        sender_id = str(event.sender.id) if event.sender else ""
    elif isinstance(event, lb_events.FriendMessage):
        conversation_id = str(event.sender.id) if event.sender else ""
        sender_id = str(event.sender.id) if event.sender else ""
    else:
        conversation_id = str(getattr(event.sender, "id", ""))
        sender_id = str(getattr(event.sender, "id", ""))

    # ── 提取消息内容 ──
    content = _extract_content(event.message_chain)

    # ── 提取来源消息 ID 供回复引用 ──
    source_msg = getattr(event.source_platform_object, "message", None)
    reply_to_id: str | None = None
    if source_msg is not None:
        reply_to_id = str(getattr(source_msg, "message_id", ""))

    return Inbound(
        channel=channel_type,
        channel_instance_id=instance_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        message_id=reply_to_id or "",
        reply_to_message_id=(
            str(getattr(source_msg, "reply_to_message_id", None))
            if source_msg else None
        ),
        content=content,
        timestamp=int(getattr(event, "time", 0)),
        metadata={},
        route=InboundRoute(
            adapter_id=instance_id,
            channel_type=channel_type,
            conversation_id=conversation_id,
            source_message_id=reply_to_id or "",
            raw={
                "adapter_id": instance_id,
                "channel_type": channel_type,
                "conversation_id": conversation_id,
                "source_message_id": reply_to_id or "",
            },
        ),
    )


def _extract_content(
    message_chain: lb_message.MessageChain | None,
) -> list[InboundContent]:
    """从 LangBot MessageChain 提取 Cogito InboundContent 列表。"""
    if message_chain is None:
        return []

    result: list[InboundContent] = []
    for component in message_chain:
        if isinstance(component, lb_message.Plain):
            result.append(InboundContent(
                type="text",
                data=component.text or "",
            ))
        elif isinstance(component, lb_message.Image):
            result.append(InboundContent(
                type="image",
                data=component.base64 or "",
                mime="image/jpeg",
            ))
        elif isinstance(component, lb_message.Voice):
            result.append(InboundContent(
                type="voice",
                data=component.base64 or "",
                mime="audio/ogg",
                size=0,
            ))
        elif isinstance(component, lb_message.File):
            result.append(InboundContent(
                type="file",
                data=component.base64 or "",
                mime="application/octet-stream",
                name=component.name or "",
                size=component.size or 0,
            ))
        elif isinstance(component, lb_message.At):
            result.append(InboundContent(
                type="at",
                data=component.target or "",
            ))
        # Forward 和其他复杂类型暂不处理
    return result
