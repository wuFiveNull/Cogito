"""Channel Adapter 基础接口。

Phase 1: LangBot 适配器通过兼容层实现此接口。
Phase 2: 适配器直接实现此接口，不经过 LangBot 类型。
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cogito.inbound.models import InboundHandler


class AdapterStatus(StrEnum):
    """适配器运行状态。"""
    created = "created"
    starting = "starting"
    running = "running"
    stopped = "stopped"
    error = "error"


@runtime_checkable
class ChannelAdapter(Protocol):
    """Channel Adapter 协议。

    所有平台适配器 (Telegram、Discord、Slack 等) 必须实现此接口。
    """

    adapter_id: str
    channel_type: str
    status: AdapterStatus

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        """设置入站消息处理器。

        Adapter 接收到平台消息后，必须转换成 Cogito Inbound
        并通过此 handler 交给 inbound dispatcher。
        """
        ...

    async def start(self) -> None:
        """启动适配器（连接平台、开始监听）。"""
        ...

    async def stop(self) -> None:
        """停止适配器（断开连接、清理资源）。"""
        ...

    async def send(
        self,
        conversation_id: str,
        message: str,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """发送消息到平台。

        Args:
            conversation_id: 平台会话 ID。
            message: 文本消息内容。
            reply_to_message_id: 可选，引用的平台消息 ID。

        Returns:
            平台响应 (至少包含 platform_message_id)。
        """
        ...
