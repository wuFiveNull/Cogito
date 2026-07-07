"""Channel Adapter 基础接口。

Phase 1: LangBot 适配器通过兼容层实现此接口。
Phase 2: 适配器直接实现此接口，不经过 LangBot 类型。

QQ-ONEBOT-E2E-01 / PR 2: 新增结构化 ChannelSendRequest/ChannelSendResult DTO。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from cogito.inbound.models import InboundHandler


class AdapterStatus(StrEnum):
    """适配器运行状态。"""
    created = "created"
    starting = "starting"
    running = "running"
    stopped = "stopped"
    error = "error"


@dataclass(frozen=True)
class ChannelSendRequest:
    """Delivery 发送请求 —— 从 DeliveryWorker 到 Adapter 的结构化契约。"""
    delivery_id: str
    attempt_id: str
    idempotency_key: str
    channel_instance_id: str
    target_endpoint_ref: str
    platform_conversation_id: str
    reply_to_platform_message_id: str | None
    text: str


@dataclass(frozen=True)
class ChannelSendResult:
    """Adapter 发送结果 —— 结构化、可区分 temporary/permanent/unknown。"""
    status: Literal["sent", "temporary", "permanent", "unknown"]
    platform_message_id: str | None = None
    error_code: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ChannelCapabilities:
    """渠道能力声明。"""
    supports_streaming: bool = False
    supports_edit: bool = False
    supports_buttons: bool = False
    supports_threads: bool = False
    supports_files: bool = False
    supports_delete: bool = False
    max_message_length: int = 4000


def _default_capabilities() -> ChannelCapabilities:
    return ChannelCapabilities()


@runtime_checkable
class ChannelAdapter(Protocol):
    """Channel Adapter 协议。

    所有平台适配器 (Telegram、Discord、Slack 等) 必须实现此接口。

    QQ-ONEBOT-E2E-01: 新增 send_request() 和 capabilities() 为可选协议方法。
    实现 send() 即视为满足协议；send_request() 默认委托 send()。
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
        """发送消息到平台（遗留签名 —— 返回 dict 保持向后兼容）。

        新代码应使用 send_request() 获取结构化结果。
        """
        ...
