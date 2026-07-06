"""LangBot compatibility: Event types (FriendMessage, GroupMessage, etc.)

Minimal stub that lets copied LangBot adapters run inside Cogito.
Replaces `langbot_plugin.api.entities.builtin.platform.events`.
"""
from __future__ import annotations

from typing import Any

from .entities import Friend, GroupMember
from .message import MessageChain


class Event:
    """事件基类。"""

    source_platform_object: Any | None = None

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class MessageEvent(Event):
    """消息事件基类。"""

    sender: Any | None = None
    message_chain: MessageChain | None = None
    time: float = 0.0

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class FriendMessage(MessageEvent):
    """好友/私聊消息事件。"""

    sender: Friend | None = None

    def __init__(
        self,
        sender: Friend | None = None,
        message_chain: MessageChain | None = None,
        time: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            sender=sender,
            message_chain=message_chain,
            time=time,
            **kwargs,
        )


class GroupMessage(MessageEvent):
    """群聊消息事件。"""

    sender: GroupMember | None = None

    def __init__(
        self,
        sender: GroupMember | None = None,
        message_chain: MessageChain | None = None,
        time: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            sender=sender,
            message_chain=message_chain,
            time=time,
            **kwargs,
        )


class GroupRecallEvent(Event):
    """群消息撤回事件。"""
    ...


class FriendRecallEvent(Event):
    """好友消息撤回事件。"""
    ...
