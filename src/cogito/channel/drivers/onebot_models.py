"""OneBot 11 稳定 ID 规则和 LangBot Event → canonical Inbound 转换。

QQ-ONEBOT-E2E-01 / PR 3:
- 私聊/群聊的稳定 ID 规则（见 plan §8.4）
- LangBot Event → canonical Inbound 转换（修复 message_id 提取）
- Owner allowlist、群 allowlist、@Bot gating
- bot 自消息过滤

本模块不依赖 aiocqhttp SDK 类型，只依赖 LangBot 兼容层类型。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from cogito.channel.vendor.langbot.compatibility import events as lb_events
from cogito.channel.vendor.langbot.compatibility import message as lb_message
from cogito.inbound.models import Inbound, InboundContent, InboundRoute

_LOG = logging.getLogger("cogito.channel.onebot")


# ── 稳定 ID 规则 ────────────────────────────────────────────────────────

def private_channel_type() -> str:
    return "qq"


def private_instance_id(cfg_instance_id: str) -> str:
    return cfg_instance_id or "qq-main"


def private_sender_endpoint_ref(instance_id: str, qq_user_id: str) -> str:
    return f"qq:{instance_id}:user:{qq_user_id}"


def private_conversation_endpoint_ref(instance_id: str, qq_user_id: str) -> str:
    return f"qq:{instance_id}:private:{qq_user_id}"


def private_platform_conversation_id(qq_user_id: str) -> str:
    return f"private:{qq_user_id}"


def private_target_endpoint_ref(instance_id: str, qq_user_id: str) -> str:
    return f"qq:{instance_id}:person:{qq_user_id}"


def group_sender_endpoint_ref(instance_id: str, member_qq_id: str) -> str:
    return f"qq:{instance_id}:user:{member_qq_id}"


def group_conversation_endpoint_ref(instance_id: str, group_id: str) -> str:
    return f"qq:{instance_id}:group:{group_id}"


def group_platform_conversation_id(group_id: str) -> str:
    return f"group:{group_id}"


def group_target_endpoint_ref(instance_id: str, group_id: str) -> str:
    return f"qq:{instance_id}:group:{group_id}"


# ── 策略配置 ────────────────────────────────────────────────────────────


@dataclass
class OneBotPolicy:
    """QQ 入站策略 —— allowlist、@Bot gating、bot 自消息过滤。"""
    owner_qq_ids: set[str] = field(default_factory=set)
    allow_private: bool = True
    allowed_group_ids: set[str,] = field(default_factory=set)  # 空 = 拒绝全部
    require_mention_in_group: bool = True
    bot_self_ids: set[str] = field(default_factory=set)

    def is_owner(self, qq_id: str | int) -> bool:
        return str(qq_id) in self.owner_qq_ids

    def is_group_allowed(self, group_id: str | int) -> bool:
        return str(group_id) in self.allowed_group_ids

    def is_bot_self(self, qq_id: str | int) -> bool:
        return str(qq_id) in self.bot_self_ids


# ── LangBot Event → canonical Inbound ────────────────────────────────────


def extract_onebot_message_id(event: lb_events.MessageEvent) -> str:
    """从 LangBot Event 提取 OneBot message_id。

    修复 QQ-09: OneBot ID 实际在 Event 的 message_id（source_platform_object 上），
    不在 source_platform_object.message.message_id。

    提取路径：
    1. source_platform_object.message_id（OneBot 标准字段）
    2. source_platform_object['message_id']（dict 访问）
    3. source_platform_object.id（fallback）
    4. source_platform_object.message.message_id（旧路径，仅最后 fallback）
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


def extract_reply_message_id(event: lb_events.MessageEvent) -> str | None:
    """提取引用回复的 message_id。

    LangBot 兼容层没有 Quote 类，reply_to 从 source_platform_object 提取。
    """
    src = getattr(event, "source_platform_object", None)
    if src is None:
        return None

    # 从 source_platform_object 属性
    reply_to = getattr(src, "reply_to_message_id", None)
    if reply_to is not None:
        return str(reply_to)

    # fallback: source_platform_object['reply_to']['message_id'] (OneBot dict)
    if isinstance(src, dict):
        reply_to_obj = src.get("reply_to")
        if isinstance(reply_to_obj, dict):
            mid = reply_to_obj.get("message_id")
            if mid is not None:
                return str(mid)

    return None


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
        elif isinstance(component, lb_message.AtAll):
            result.append(InboundContent(
                type="at",
                data="all",
            ))
        elif isinstance(component, lb_message.Face):
            result.append(InboundContent(
                type="face",
                data=str(component.face_id) if component.face_id else "",
            ))
        # Forward 和其他复杂类型暂不处理
    return result


def _is_at_bot(message_chain: lb_message.MessageChain | None, bot_ids: set[str]) -> bool:
    """检查 MessageChain 中是否包含 @Bot。"""
    if message_chain is None:
        return False
    for component in message_chain:
        if isinstance(component, lb_message.AtAll):
            return True
        if isinstance(component, lb_message.At):
            target = str(component.target or "")
            if target == "all" or target in bot_ids:
                return True
    return False


def friend_event_to_inbound(
    event: lb_events.FriendMessage,
    *,
    instance_id: str,
    policy: OneBotPolicy,
    bot_ids: set[str],
) -> tuple[Inbound | None, str]:
    """私聊 LangBot Event → canonical Inbound。

    返回 (inbound, reason)：
    - inbound=None 表示被策略过滤，reason 说明原因。
    - inbound!=None 表示通过策略。
    """
    sender = event.sender
    sender_id = str(getattr(sender, "id", "")) if sender else ""

    # bot 自消息过滤
    if policy.is_bot_self(sender_id):
        return None, "bot_self"

    # Owner allowlist
    if not policy.is_owner(sender_id):
        _LOG.debug("Non-owner private message from %s, rejected", sender_id)
        return None, "not_owner"

    # 提取 message_id
    message_id = extract_onebot_message_id(event)
    if not message_id:
        _LOG.warning("FriendMessage from %s has empty message_id", sender_id)
        return None, "empty_message_id"

    # 提取内容
    content = _extract_content(event.message_chain)

    # 提取 reply
    reply_to_id = extract_reply_message_id(event)

    inbound = Inbound(
        channel=private_channel_type(),
        channel_instance_id=instance_id,
        conversation_id=private_platform_conversation_id(sender_id),
        sender_id=sender_id,
        message_id=message_id,
        reply_to_message_id=reply_to_id,
        content=content,
        timestamp=int(getattr(event, "time", 0)),
        metadata={
            "conversation_type": "private",
            "trust_label": "external_untrusted",
            "capability": {"text": True, "image": False, "voice": False},
            "sender_endpoint_ref": private_sender_endpoint_ref(instance_id, sender_id),
            "conversation_endpoint_ref": private_conversation_endpoint_ref(instance_id, sender_id),
            "target_endpoint_ref": private_target_endpoint_ref(instance_id, sender_id),
        },
        route=InboundRoute(
            adapter_id=instance_id,
            channel_type=private_channel_type(),
            conversation_id=private_platform_conversation_id(sender_id),
            source_message_id=message_id,
            raw={
                "adapter_id": instance_id,
                "channel_type": private_channel_type(),
                "conversation_id": private_platform_conversation_id(sender_id),
                "source_message_id": message_id,
            },
        ),
    )
    return inbound, "accepted"


def group_event_to_inbound(
    event: lb_events.GroupMessage,
    *,
    instance_id: str,
    policy: OneBotPolicy,
    bot_ids: set[str],
) -> tuple[Inbound | None, str]:
    """群聊 LangBot Event → canonical Inbound。

    策略：
    - 群必须在 allowed_group_ids 中
    - 必须显式 @Bot（require_mention_in_group=true 时）
    - bot 自消息过滤
    """
    sender = event.sender
    sender_id = str(getattr(sender, "id", "")) if sender else ""
    group_obj = getattr(sender, "group", None) if sender else None
    group_id = str(getattr(group_obj, "id", "")) if group_obj else ""

    # bot 自消息过滤
    if policy.is_bot_self(sender_id):
        return None, "bot_self"

    # 群 allowlist
    if not policy.is_group_allowed(group_id):
        _LOG.debug("Group %s not in allowlist, rejected", group_id)
        return None, "group_not_allowed"

    # @Bot gating
    is_mentioned = _is_at_bot(event.message_chain, bot_ids)
    if policy.require_mention_in_group and not is_mentioned:
        _LOG.debug("Group %s message not @Bot, ignored", group_id)
        return None, "not_at_bot"

    # 提取 message_id
    message_id = extract_onebot_message_id(event)
    if not message_id:
        _LOG.warning("GroupMessage in %s has empty message_id", group_id)
        return None, "empty_message_id"

    # 提取内容
    content = _extract_content(event.message_chain)

    # 提取 reply
    reply_to_id = extract_reply_message_id(event)

    inbound = Inbound(
        channel=private_channel_type(),
        channel_instance_id=instance_id,
        conversation_id=group_platform_conversation_id(group_id),
        sender_id=sender_id,
        message_id=message_id,
        reply_to_message_id=reply_to_id,
        content=content,
        timestamp=int(getattr(event, "time", 0)),
        metadata={
            "conversation_type": "group",
            "trust_label": "external_untrusted",
            "capability": {"text": True, "image": False, "voice": False},
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_endpoint_ref": group_sender_endpoint_ref(instance_id, sender_id),
            "conversation_endpoint_ref": group_conversation_endpoint_ref(instance_id, group_id),
            "target_endpoint_ref": group_target_endpoint_ref(instance_id, group_id),
        },
        route=InboundRoute(
            adapter_id=instance_id,
            channel_type=private_channel_type(),
            conversation_id=group_platform_conversation_id(group_id),
            source_message_id=message_id,
            raw={
                "adapter_id": instance_id,
                "channel_type": private_channel_type(),
                "conversation_id": group_platform_conversation_id(group_id),
                "source_message_id": message_id,
            },
        ),
    )
    return inbound, "accepted"
