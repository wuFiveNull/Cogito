"""应用层 DTO —— 请求/响应/查询类型。

这些是服务协议方法的输入输出类型，不属于领域核心但定义在应用层。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cogito.domain.entities import MemoryItem, Message
from cogito.domain.enums import MemoryKind, MemoryScope


# =============================================================================
# Memory DTO
# =============================================================================


@dataclass(frozen=True)
class MemoryQuery:
    principal_id: str = ""
    scope: MemoryScope | None = None
    kind: MemoryKind | None = None
    subject_pattern: str | None = None
    min_confidence: float = 0.0
    limit: int = 10
    include_expired: bool = False


@dataclass(frozen=True)
class MemoryResult:
    items: list[MemoryItem] = field(default_factory=list)
    query_metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryCandidate:
    kind: MemoryKind = MemoryKind.FACT
    subject: str = ""
    predicate: str = ""
    value: object = None
    scope: MemoryScope = MemoryScope.OWNER_GLOBAL
    source_type: str = ""
    source_id: str = ""
    confidence: float = 0.5


# =============================================================================
# Model DTO
# =============================================================================


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, object]] = field(default_factory=list)
    tools: list[dict[str, object]] | None = None
    tool_choice: str | None = None
    max_tokens: int = 8192
    temperature: float | None = None
    stream: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    content_parts: list[object] = field(default_factory=list)
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""
    thinking: str | None = None


@dataclass(frozen=True)
class ModelStreamChunk:
    delta_content: str = ""
    delta_tool_call: dict[str, object] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    max_context_tokens: int = 128_000
    max_output_tokens: int = 4096
    supports_streaming: bool = True
    supports_tool_calls: bool = True
    supports_vision: bool = False
    supports_thinking: bool = False


# =============================================================================
# Channel DTO
# =============================================================================


@dataclass(frozen=True)
class ChannelCapabilities:
    supports_edit: bool = False
    supports_delete: bool = False
    supports_streaming: bool = False
    supports_attachments: bool = False
    content_type_limits: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelRef:
    channel_type: str = ""
    channel_instance_id: str = ""
    platform_conversation_id: str = ""
    thread_id: str | None = None


@dataclass(frozen=True)
class ChannelSendRequest:
    target: object = None  # TargetSnapshot
    content_parts: list[object] = field(default_factory=list)
    reply_mode: str = "direct"
    reply_to_message_id: str | None = None
    idempotency_key: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelSendResult:
    success: bool = False
    platform_message_id: str = ""
    error: str | None = None
    receipt: str | None = None


@dataclass(frozen=True)
class ChannelEditRequest:
    platform_message_id: str = ""
    content_parts: list[object] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelDeleteRequest:
    platform_message_id: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


# =============================================================================
# Session DTO
# =============================================================================


@dataclass(frozen=True)
class SessionRef:
    session_id: str = ""
    context_partition_key: str = ""
    reset_generation: int = 0


# =============================================================================
# Connector DTO
# =============================================================================


@dataclass(frozen=True)
class ConnectorCursor:
    cursor_value: str = ""
    last_polled_at: datetime | None = None


@dataclass(frozen=True)
class ConnectorBatch:
    batch_id: str = ""
    items: list[object] = field(default_factory=list)  # RawItem
    next_cursor: ConnectorCursor | None = None


@dataclass(frozen=True)
class ConnectorCapabilities:
    supports_polling: bool = True
    supports_webhook: bool = False
    supports_streaming: bool = False
    recommended_poll_interval_seconds: float = 300.0


# =============================================================================
# Task DTO
# =============================================================================


@dataclass(frozen=True)
class TaskContext:
    task_id: str = ""
    attempt_id: str = ""
    attempt_no: int = 0
    checkpoint: object | None = None  # Checkpoint
    trace_context: object | None = None  # TraceContext


@dataclass(frozen=True)
class TaskComplete:
    result: object = None


@dataclass(frozen=True)
class TaskRetry:
    error: str = ""
    next_at: datetime | None = None


@dataclass(frozen=True)
class TaskWaitUser:
    approval_id: str = ""


@dataclass(frozen=True)
class TaskWaitExternal:
    condition: str = ""
    next_check_at: datetime | None = None


@dataclass(frozen=True)
class TaskSpawn:
    children: list[dict[str, object]] = field(default_factory=list)
    join_policy: str = "all"


@dataclass(frozen=True)
class TaskCancel:
    reason: str = ""


@dataclass(frozen=True)
class TaskFail:
    error: str = ""


# TaskOutcome —— 联合类型
TaskOutcome = TaskComplete | TaskRetry | TaskWaitUser | TaskWaitExternal | TaskSpawn | TaskCancel | TaskFail


# =============================================================================
# Delivery DTO
# =============================================================================


@dataclass(frozen=True)
class DeliveryRequest:
    content_ref: str = ""
    target_snapshot: object = None  # TargetSnapshot
    idempotency_key: str = ""
    scheduled_at: datetime | None = None
    priority: int = 50


@dataclass(frozen=True)
class DeliveryRef:
    delivery_id: str = ""
    status: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Approval DTO
# =============================================================================


@dataclass(frozen=True)
class ApprovalSubject:
    subject_type: str = ""
    subject_id: str = ""
    action_hash: str = ""
    arguments_snapshot: dict[str, object] = field(default_factory=dict)
    risk_level: str = "low"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool = False
    require_approval: bool = False
    reason: str = ""
    risk_level: str = "low"


# =============================================================================
# 通用 DTO
# =============================================================================


@dataclass(frozen=True)
class HealthStatus:
    healthy: bool = True
    latency_ms: float = 0.0
    last_error: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnAccepted:
    turn_id: str = ""
    status: str = "queued"
    accepted_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class ResumeCommand:
    approval_id: str | None = None
    external_result: dict[str, object] = field(default_factory=dict)
    resume_reason: str = ""


@dataclass(frozen=True)
class CapabilityPolicy:
    allowed_tools: list[str] = field(default_factory=list)
    allowed_permissions: list[str] = field(default_factory=list)
    require_approval_for: list[str] = field(default_factory=list)
