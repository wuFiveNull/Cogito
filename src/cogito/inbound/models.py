"""Cogito Inbound — Channel Adapter 到 Core 的统一消息格式。

所有平台消息必须转换为 Inbound 后才能进入 Cogito Core。
Core 不接收平台原生消息类型。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class InboundContent:
    """入站消息内容片段。"""
    type: str = "text"           # "text" | "image" | "file" | "voice" | "at"
    data: str = ""               # 文本内容或 base64 编码的媒体数据
    mime: str | None = None      # MIME 类型 (媒体)
    name: str | None = None      # 文件名 (文件)
    size: int = 0                # 文件大小


@dataclass
class InboundRoute:
    """回复路由信息 —— 保存将回复发回平台所需的信息。"""
    adapter_id: str = ""         # 用于回复的 Adapter 标识
    channel_type: str = ""       # 平台类型 ("telegram", "discord" 等)
    conversation_id: str = ""     # 平台会话 ID
    source_message_id: str = ""  # 平台消息 ID (用于引用回复)
    raw: dict = field(default_factory=dict)  # Adapter 专属路由数据


@dataclass
class Inbound:
    """统一入站消息。

    由 channel/bridge.py 或 adapter 直接创建，交给 inbound/dispatcher.py。
    """
    channel: str = ""                 # "telegram"
    channel_instance_id: str = ""     # Bot 实例标识
    conversation_id: str = ""         # 平台会话 ID
    sender_id: str = ""               # 平台发送者 ID
    message_id: str = ""              # 平台消息 ID
    reply_to_message_id: str | None = None
    content: list[InboundContent] = field(default_factory=list)
    timestamp: int = 0
    metadata: dict = field(default_factory=dict)
    route: InboundRoute = field(default_factory=InboundRoute)


# ── InboundHandler 协议 ──

InboundHandler = Callable[[Inbound], Awaitable[None]]
"""Inbound 处理器类型。Dispatcher 将 Inbound 传给此 handler。"""
