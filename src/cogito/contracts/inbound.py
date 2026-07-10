"""Contracts — 统一入站消息契约（PLAN-10 M2：从 cogito.inbound 解耦）。

本模块定义跨模块引用的 Inbound 数据契约和 InboundHandler 端口：
- ``Inbound``：Adapter 向 Core 传递的统一入站消息
- ``InboundContent``：Inbound 内容块（text / image / voice / file / at / face 等）
- ``InboundRoute``：入站路由信息（adapter、channel、来源 message_id 等）
- ``InboundHandler``：Adapter → Core 的入站处理端口（Protocol）

所有 Channel Adapter 和 Core 模块通过本模块引用上述类型，
不再依赖 ``cogito.inbound.models`` 或 ``cogito.inbound.dispatcher``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── InboundContent ──────────────────────────────────────────────────────────


@dataclass
class InboundContent:
    """Inbound 内容块。

    ``type`` 取值：text / image / voice / file / at / face / video / location。
    ``data`` 的具体语义随 ``type`` 变化（text 时为文本，其他类型通常为
    base64 / file_id / url 等）。
    """

    type: str
    data: str = ""
    mime: str | None = None
    name: str | None = None
    # Cross-process and persistence invariant: unknown/inline content has size=0.
    # ``None`` is not used because content_parts.size is a NOT NULL column.
    size: int = 0


# ── InboundRoute ─────────────────────────────────────────────────────────────


@dataclass
class InboundRoute:
    """入站路由信息 —— 追踪消息在 Adapter → Core 路径中的元数据。"""

    adapter_id: str
    channel_type: str
    conversation_id: str
    source_message_id: str
    raw: dict[str, Any] = field(default_factory=dict)


# ── Inbound ──────────────────────────────────────────────────────────────────


@dataclass
class Inbound:
    """统一入站消息 —— 所有 Adapter 构造此类型并交给 InboundHandler。"""

    channel: str
    channel_instance_id: str
    conversation_id: str
    sender_id: str
    content: list[InboundContent]
    route: InboundRoute
    message_id: str = ""
    reply_to_message_id: str | None = None
    timestamp: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ── InboundHandler Port ─────────────────────────────────────────────────────


@runtime_checkable
class InboundHandler(Protocol):
    """Adapter → Core 的入站处理端口。

    Adapter 收到平台消息后，构造 :class:`Inbound` 并调用此端口；
    Core 侧实现 :meth:`dispatch` 将其转换为 :class:`ChannelEnvelope`
    并交给 :class:`~cogito.service.inbound_service.InboundService`。

    实现类：
    - :class:`cogito.inbound.dispatcher.InboundDispatcher`（主路径）
    """

    async def dispatch(self, inbound: Inbound) -> None:
        """分发一条 Inbound 消息到 Agent Core。"""
        ...


__all__ = [
    "Inbound",
    "InboundContent",
    "InboundRoute",
    "InboundHandler",
]
