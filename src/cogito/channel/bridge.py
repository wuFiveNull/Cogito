"""LangBot Event → Cogito Inbound 桥接转换。

QQ-ONEBOT-E2E-01: 增强 OneBot 来源 message_id 提取，修复 QQ-09。
"""
from __future__ import annotations

import logging

from cogito.channel.vendor.langbot.compatibility import events as lb_events
from cogito.channel.vendor.langbot.compatibility import message as lb_message

# PLAN-10 M2: 经 contracts.inbound 引用；本模块仅由 legacy qqofficial adapter 使用
from cogito.contracts.inbound import Inbound, InboundContent, InboundRoute

_LOG = logging.getLogger("cogito.channel.bridge")


def _extract_onebot_message_id(event: lb_events.MessageEvent) -> str:
    """提取 OneBot message_id —— 增强路径。

    QQ-09 修复：OneBot ID 实际在 Event 的 message_id（source_platform_object 上），
    不在 source_platform_object.message.message_id。
    """
    src = getattr(event, "source_platform_object", None)
    if src is None:
        return ""

    # 1. 直接属性访问
    msg_id = getattr(src, "message_id", None)
    if msg_id is not None:
        return str(msg_id)

    # 2. dict 访问（aiocqhttp.Event 是 dict 子类）
    if isinstance(src, dict):
        msg_id = src.get("message_id")
        if msg_id is not None:
            return str(msg_id)

    # 3. source_platform_object.id
    obj_id = getattr(src, "id", None)
    if obj_id is not None:
        return str(obj_id)

    # 4. 旧路径 fallback
    source_msg = getattr(src, "message", None)
    if source_msg is not None:
        return str(getattr(source_msg, "message_id", "") or "")

    return ""


async def langbot_event_to_inbound(
    event: lb_events.MessageEvent,
    channel_type: str,
    instance_id: str,
) -> Inbound:
    """将 LangBot MessageEvent 转换为 Cogito Inbound。

    这是 Phase 1 的兼容转换。Phase 2 中适配器将直接生成 Inbound，
    跳过此转换层。

    QQ-ONEBOT-E2E-01: 使用增强的 message_id 提取路径。
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

    # ── 提取来源消息 ID 供回复引用（增强路径）──
    message_id = _extract_onebot_message_id(event)

    # ── 提取 reply_to_message_id ──
    source_msg = getattr(event.source_platform_object, "message", None)
    reply_to_id: str | None = None
    if source_msg is not None:
        reply_to_id = str(getattr(source_msg, "reply_to_message_id", None) or None)
        if reply_to_id == "None":
            reply_to_id = None

    return Inbound(
        channel=channel_type,
        channel_instance_id=instance_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        message_id=message_id,
        reply_to_message_id=reply_to_id,
        content=content,
        timestamp=int(getattr(event, "time", 0)),
        metadata={},
        route=InboundRoute(
            adapter_id=instance_id,
            channel_type=channel_type,
            conversation_id=conversation_id,
            source_message_id=message_id,
            raw={
                "adapter_id": instance_id,
                "channel_type": channel_type,
                "conversation_id": conversation_id,
                "source_message_id": message_id,
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
