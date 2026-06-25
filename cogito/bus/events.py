"""
cogito.bus.events — 核心消息数据类

消息载荷与信道协议解耦，所有核心类型均为 frozen dataclass。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping, Sequence


# ── 消息载荷 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TextPart:
    """纯文本片段。"""
    text: str


@dataclass(frozen=True)
class AttachmentRef:
    """附件引用，不直接传输二进制数据。"""
    id: str
    content_type: str
    size: int
    sha256: str
    local_path: str | None = None
    remote_refs: Mapping[str, str] = field(default_factory=dict)


MessagePart = TextPart | AttachmentRef


@dataclass(frozen=True)
class MessagePayload:
    """包含零个或多个消息片段。"""
    parts: Sequence[MessagePart]


# ── 入站消息 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InboundMessage:
    """来自外部平台的标准化入站消息。"""
    message_id: str
    external_message_id: str | None

    session_key: str
    channel: str
    target: str

    payload: MessagePayload

    trace_id: str
    received_at: datetime
    occurred_at: datetime | None = None

    reply_to: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundControl:
    """入站控制信号，允许外部请求中断、重置或关闭。"""
    control_id: str
    kind: Literal["interrupt", "reset_session", "shutdown"]
    session_key: str | None
    channel: str
    trace_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


InboundItem = InboundMessage | InboundControl


# ── Turn 上下文 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnContext:
    """单个 Turn 的只读上下文。"""
    turn_id: str
    trace_id: str
    session_key: str
    trigger_message_id: str | None

    origin: Literal["inbound", "proactive", "system"]
    started_at: datetime


# ── 出站请求 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutboundRequest:
    """统一出站请求，三种出站路径均使用此类型。"""
    outbound_id: str

    channel: str
    target: str
    payload: MessagePayload

    origin: Literal["reply", "proactive", "tool"]

    trace_id: str
    session_key: str | None = None
    turn_id: str | None = None

    priority: int = 100
    idempotency_key: str | None = None
    created_at: datetime | None = None

    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryReceipt:
    """出站投递结果。"""
    outbound_id: str
    status: Literal[
        "accepted",
        "delivered",
        "retrying",
        "failed",
        "dead",
    ]

    external_message_id: str | None = None
    attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None


# ── 出站异常 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeliveryError(Exception):
    """Channel 应将平台 SDK 异常转换为统一错误。"""
    code: str
    message: str
    retryable: bool
    retry_after: float | None = None

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"
