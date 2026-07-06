"""领域值对象 —— 不可变的复合数据类型。

值对象没有独立身份，通过其值来识别。所有值对象使用 frozen=True。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cogito.domain.enums import ContentType, TrustLabel


# =============================================================================
# ContentPart —— 统一内容类型
# =============================================================================


@dataclass(frozen=True)
class TextPart:
    part_id: str
    text: str
    content_type: ContentType = ContentType.TEXT
    metadata: dict[str, object] = field(default_factory=dict)
    trust_label: TrustLabel = TrustLabel.UNVERIFIED


@dataclass(frozen=True)
class ImagePart:
    part_id: str
    url: str | None = None
    alt_text: str | None = None
    payload_ref: str | None = None
    size: int = 0
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    content_type: ContentType = ContentType.IMAGE
    metadata: dict[str, object] = field(default_factory=dict)
    trust_label: TrustLabel = TrustLabel.UNVERIFIED


@dataclass(frozen=True)
class FilePart:
    part_id: str
    filename: str
    mime_type: str
    payload_ref: str
    size: int = 0
    sha256: str | None = None
    content_type: ContentType = ContentType.FILE
    metadata: dict[str, object] = field(default_factory=dict)
    trust_label: TrustLabel = TrustLabel.UNVERIFIED


@dataclass(frozen=True)
class AudioPart:
    part_id: str
    payload_ref: str
    duration_seconds: float | None = None
    mime_type: str = "audio/mpeg"
    size: int = 0
    content_type: ContentType = ContentType.AUDIO
    metadata: dict[str, object] = field(default_factory=dict)
    trust_label: TrustLabel = TrustLabel.UNVERIFIED


@dataclass(frozen=True)
class VideoPart:
    part_id: str
    payload_ref: str
    duration_seconds: float | None = None
    mime_type: str = "video/mp4"
    size: int = 0
    width: int | None = None
    height: int | None = None
    content_type: ContentType = ContentType.VIDEO
    metadata: dict[str, object] = field(default_factory=dict)
    trust_label: TrustLabel = TrustLabel.UNVERIFIED


@dataclass(frozen=True)
class LocationPart:
    part_id: str
    latitude: float
    longitude: float
    label: str | None = None
    content_type: ContentType = ContentType.LOCATION
    metadata: dict[str, object] = field(default_factory=dict)
    trust_label: TrustLabel = TrustLabel.UNVERIFIED


@dataclass(frozen=True)
class QuotePart:
    part_id: str
    quoted_message_id: str
    quoted_text: str
    quoted_sender_id: str | None = None
    content_type: ContentType = ContentType.QUOTE
    metadata: dict[str, object] = field(default_factory=dict)
    trust_label: TrustLabel = TrustLabel.UNVERIFIED


@dataclass(frozen=True)
class ButtonActionPart:
    part_id: str
    button_id: str
    label: str
    action_data: dict[str, object] = field(default_factory=dict)
    content_type: ContentType = ContentType.BUTTON_ACTION
    metadata: dict[str, object] = field(default_factory=dict)
    trust_label: TrustLabel = TrustLabel.UNVERIFIED


@dataclass(frozen=True)
class StructuredPart:
    part_id: str
    schema_name: str
    data: dict[str, object] = field(default_factory=dict)
    content_type: ContentType = ContentType.STRUCTURED
    metadata: dict[str, object] = field(default_factory=dict)
    trust_label: TrustLabel = TrustLabel.UNVERIFIED


# 联合类型别名：所有 Part 的联合
ContentPart = (
    TextPart
    | ImagePart
    | FilePart
    | AudioPart
    | VideoPart
    | LocationPart
    | QuotePart
    | ButtonActionPart
    | StructuredPart
)


# =============================================================================
# TraceContext —— 端到端因果链
# =============================================================================


@dataclass(frozen=True)
class TraceContext:
    """跨进程传播的因果上下文。

    字段允许为空，但一旦设置，不允许普通 Middleware 修改。
    """

    trace_id: str = ""
    span_id: str | None = None
    parent_span_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    principal_id: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    attempt_id: str | None = None
    task_id: str | None = None


# =============================================================================
# ReplyRoute —— 回复路由快照
# =============================================================================


@dataclass(frozen=True)
class ReplyRoute:
    """创建 Delivery 时固定的回执路由快照。

    Token 过期后返回 route_expired，拒绝猜测新目标。
    """

    channel_type: str
    channel_instance_id: str
    platform_conversation_id: str
    thread_id: str | None = None
    reply_to_platform_message_id: str | None = None
    reply_token: str | None = None
    reply_token_expires_at: datetime | None = None


# =============================================================================
# TargetSnapshot —— 投递目标快照
# =============================================================================


@dataclass(frozen=True)
class TargetSnapshot:
    """投递创建时固定的目标快照，避免 Endpoint 配置变化导致发送到意外目标。"""

    endpoint_id: str
    channel_type: str
    channel_instance_id: str
    platform_account_id: str
    platform_conversation_id: str
    thread_id: str | None = None
    snapshot_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# ContextSnapshot —— Agent 执行时的上下文快照
# =============================================================================


@dataclass(frozen=True)
class ContextSnapshot:
    """模型调用时的上下文不可变记录。"""

    snapshot_id: str
    turn_id: str
    session_id: str
    conversation_version: int
    message_upper_bound: int
    summary_id: str = ""
    selected_memory_ids: tuple[str, ...] = field(default_factory=tuple)
    selection_policy_version: str = ""
    token_estimate: int = 0
    assembled_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Lease —— 分布式执行权
# =============================================================================


@dataclass(frozen=True)
class Lease:
    """Worker 持有的执行权。

    所有条件更新必须同时验证 lease_owner 和 lease_version。
    """

    owner: str
    version: int
    expires_at: datetime
    acquired_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# PayloadRef —— 大对象引用
# =============================================================================


@dataclass(frozen=True)
class PayloadRef:
    """Payload Store 中大对象的引用。

    Payload 写入协议：临时文件 → 计算 hash/size → fsync → 原子 rename
    → SQLite 事务中写入 metadata 和业务引用。
    """

    ref: str
    sha256: str
    content_type: str
    size: int
    is_inline: bool = False
    inline_data: bytes | None = None


# =============================================================================
# 辅助值对象
# =============================================================================


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略配置。"""

    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 300.0
    jitter: float = 0.1


@dataclass(frozen=True)
class ResourceUsage:
    """一次执行尝试的资源消耗统计。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    tool_calls: int = 0
    iterations: int = 0


@dataclass(frozen=True)
class ResourceBudget:
    """单次执行的资源预算限制。"""

    max_iterations: int = 10
    max_tool_calls: int = 20
    max_total_tokens: int = 100_000
    max_runtime_seconds: float = 300.0


@dataclass(frozen=True)
class Checkpoint:
    """执行可恢复的最小状态。"""

    checkpoint_id: str
    attempt_id: str
    checkpoint_no: int
    state_ref: str
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class SideEffectReceipt:
    """外部副作用的执行凭证。"""

    receipt_id: str
    tool_call_id: str
    effect_type: str
    target: str
    result: str
    occurred_at: datetime = field(default_factory=datetime.utcnow)
