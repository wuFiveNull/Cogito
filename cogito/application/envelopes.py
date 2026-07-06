"""跨进程信封类型。

所有跨进程通信使用不可变的信封包装。通用字段：
- schema_version
- message/request/event id
- created_at / occurred_at
- trace_context
- origin
- metadata

大型内容使用 inline_small_payload 或 payload_ref + sha256 + content_type + size。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cogito.domain.value_objects import (
    ContentPart,
    PayloadRef,
    ReplyRoute,
    SideEffectReceipt,
    TraceContext,
)


# =============================================================================
# ChannelEnvelope —— 入站消息标准化
# =============================================================================


@dataclass(frozen=True)
class ChannelEnvelope:
    """从任何平台入站消息的标准格式。

    Endpoint Ref 是 Channel Driver 提供的稳定不透明字符串，Core 不解释内部结构。
    """

    schema_version: str = "1.0"
    message_id: str = ""
    channel_type: str = ""
    channel_instance_id: str = ""
    platform_sender_id: str = ""
    sender_endpoint_ref: str = ""
    conversation_endpoint_ref: str = ""
    platform_conversation_id: str = ""
    thread_id: str | None = None
    content_parts: tuple[ContentPart, ...] = field(default_factory=tuple)
    platform_message_id: str = ""
    reply_route: ReplyRoute | None = None
    received_at: datetime = field(default_factory=datetime.utcnow)
    trust_label: str = "unverified"
    trace_context: TraceContext | None = None
    metadata: dict[str, object] = field(default_factory=dict)


# =============================================================================
# AgentRequest / AgentReply —— Core 内部请求
# =============================================================================


@dataclass(frozen=True)
class AgentRequest:
    """Agent Core 内部推理请求。"""

    turn_id: str = ""
    principal: object = None  # Principal
    conversation: object = None  # Conversation
    context_snapshot: object = None  # ContextSnapshot
    input_message: object = None  # Message
    capability_policy: object = None  # CapabilityPolicy
    resource_budget: object = None  # ResourceBudget
    trace_context: TraceContext | None = None


@dataclass(frozen=True)
class AgentReply:
    """Agent Core 推理结果。

    不包含平台 SDK 对象。
    """

    turn_id: str = ""
    content_parts: tuple[ContentPart, ...] = field(default_factory=tuple)
    reply_mode: str = "direct"
    render_hints: dict[str, object] = field(default_factory=dict)
    memory_candidates: list[object] = field(default_factory=list)  # MemoryCandidate
    suggested_tasks: list[dict[str, object]] = field(default_factory=list)
    status_summary: str = ""
    trace_context: TraceContext | None = None


# =============================================================================
# ToolRequest / ToolResult —— 工具调用
# =============================================================================


@dataclass(frozen=True)
class ToolRequest:
    """工具调用请求。"""

    tool_call_id: str = ""
    tool_name: str = ""
    tool_version: str = ""
    arguments: dict[str, object] = field(default_factory=dict)
    requested_permissions: list[str] = field(default_factory=list)
    idempotency_key: str = ""
    timeout: float = 30.0
    risk_context: dict[str, object] = field(default_factory=dict)
    trace_context: TraceContext | None = None


@dataclass(frozen=True)
class ToolResult:
    """工具调用结果。"""

    tool_call_id: str = ""
    status: str = "succeeded"
    structured_output: dict[str, object] | None = None
    output_ref: str = ""
    error: str | None = None
    side_effect_receipt: SideEffectReceipt | None = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# EventEnvelope —— 已发生事实公告
# =============================================================================


@dataclass(frozen=True)
class EventEnvelope:
    """跨进程传播的领域事件包装器。

    Consumer 必须幂等，不可伪装同步返回值。
    """

    event_id: str = ""
    event_type: str = ""
    source: str = ""
    aggregate_type: str = ""
    aggregate_id: str = ""
    aggregate_version: int = 0
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    ingested_at: datetime = field(default_factory=datetime.utcnow)
    payload_ref: str = ""
    content_hash: str = ""
    trust_label: str = "trusted"
    schema_version: str = "1.0"
    origin: str = ""
    correlation_id: str = ""
    causation_id: str = ""
    trace_context: TraceContext | None = None


# =============================================================================
# ErrorEnvelope —— 结构化错误
# =============================================================================


@dataclass(frozen=True)
class ErrorEnvelope:
    """跨进程错误响应。"""

    error_id: str = ""
    error_code: str = ""
    message: str = ""
    details: dict[str, object] = field(default_factory=dict)
    retryable: bool = False
    suggested_action: str = ""
    correlation_id: str = ""
    trace_context: TraceContext | None = None
