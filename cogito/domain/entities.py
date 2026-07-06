"""核心领域实体。

使用 dataclass 定义所有领域实体。不可变实体使用 frozen=True（如 Message、Event），
可变状态实体（如 Turn、Task、Delivery）保持可变以支持状态转移。

依赖方向：domain 层不导入任何外部模块。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cogito.domain.enums import (
    ApprovalStatus,
    ApprovalSubjectType,
    ConnectorStatus,
    ConversationType,
    DeliveryStatus,
    GoalStatus,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MessageDirection,
    MessageRole,
    PrincipalStatus,
    PrincipalType,
    RiskLevel,
    RunAttemptStatus,
    ScheduleType,
    SessionStatus,
    TaskAttemptStatus,
    TaskStatus,
    TrustLabel,
    TurnStatus,
)
from cogito.domain.value_objects import (
    Checkpoint,
    ContentPart,
    Lease,
    PayloadRef,
    ResourceBudget,
    ResourceUsage,
    RetryPolicy,
    SideEffectReceipt,
    TargetSnapshot,
)


# =============================================================================
# Principal（主体）
# =============================================================================


@dataclass(frozen=True)
class Principal:
    """系统识别的主体。Owner 只有一个也不能省略。

    是权限、记忆 Scope、审计和身份绑定的根。
    """

    principal_id: str
    principal_type: PrincipalType = PrincipalType.EXTERNAL_USER
    status: PrincipalStatus = PrincipalStatus.ACTIVE
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, object] = field(default_factory=dict)


# =============================================================================
# Endpoint（端点）
# =============================================================================


@dataclass(frozen=True)
class Endpoint:
    """某个平台上的外部身份或可投递端点。

    一个 Principal 可以绑定多个 Endpoint。
    Endpoint 变更或解绑不得删除历史 Message 的来源证据。
    """

    endpoint_id: str
    channel_type: str
    channel_instance_id: str
    platform_account_id: str
    principal_id: str
    capabilities: list[str] = field(default_factory=list)
    status: str = "active"
    verified_at: datetime | None = None


# =============================================================================
# Conversation（对话容器）
# =============================================================================


@dataclass(frozen=True)
class Conversation:
    """平台对话容器。

    群聊中的 principal_scope 与消息发送者 Principal 必须分开。
    """

    conversation_id: str
    conversation_endpoint_id: str
    platform_conversation_id: str
    conversation_type: ConversationType = ConversationType.PRIVATE
    principal_scope: str = ""
    context_partition_policy: str = ""
    status: str = "active"
    created_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Session（短期上下文）
# =============================================================================


@dataclass(frozen=True)
class Session:
    """Agent 短期上下文边界，不等同于平台 Conversation。

    不同 Channel 不共享短期 Session。
    """

    session_id: str
    conversation_id: str
    context_partition_key: str
    reset_generation: int = 0
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Message（消息）
# =============================================================================


@dataclass(frozen=True)
class Message:
    """标准化消息。创建后内容不可原地覆盖；编辑使用版本或新 MessageRevision。"""

    message_id: str
    conversation_id: str
    session_id: str
    sender_principal_id: str
    sender_endpoint_id: str
    role: MessageRole = MessageRole.USER
    direction: MessageDirection = MessageDirection.INBOUND
    content_parts: tuple[ContentPart, ...] = field(default_factory=tuple)
    reply_to_message_id: str | None = None
    platform_message_id: str = ""
    current_revision_no: int = 1
    receive_sequence: int = 0
    trust_label: TrustLabel = TrustLabel.UNVERIFIED
    raw_payload_ref: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Turn（用户意图逻辑生命周期）
# =============================================================================


@dataclass
class Turn:
    """一次用户意图的逻辑生命周期。

    Turn 回答：用户的这次意图最终是否完成、正在等待什么、最终回复是什么。
    系统不设置 Run 层；RunAttempt 是 Turn 下唯一的执行尝试对象。
    """

    turn_id: str
    input_message_id: str
    conversation_id: str
    session_id: str
    status: TurnStatus = TurnStatus.ACCEPTED
    priority: int = 80
    active_attempt_id: str | None = None
    final_message_id: str | None = None
    cancel_requested_at: datetime | None = None
    next_attempt_at: datetime | None = None
    version: int = 1
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None


# =============================================================================
# RunAttempt（执行尝试）
# =============================================================================


@dataclass(frozen=True)
class RunAttempt:
    """Turn 的一次实际执行尝试。

    唯一约束：(turn_id, attempt_no)。
    失败、等待后恢复或显式重试都创建新的 RunAttempt，不复活旧 Attempt。
    """

    attempt_id: str
    turn_id: str
    attempt_no: int
    status: RunAttemptStatus = RunAttemptStatus.CREATED
    worker_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    checkpoint_id: str | None = None
    error_ref: str | None = None
    resource_usage: ResourceUsage | None = None
    trace_id: str | None = None


# =============================================================================
# Task（持久化任务）
# =============================================================================


@dataclass
class Task:
    """可持久化、可恢复的长期工作。

    Lease 获取和状态检查必须在同一事务中完成。
    """

    task_id: str
    task_type: str
    payload_ref: str | None = None
    status: TaskStatus = TaskStatus.CREATED
    priority: int = 50
    scheduled_at: datetime | None = None
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    checkpoint_ref: str | None = None
    idempotency_key: str = ""
    origin: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class TaskAttempt:
    """Task 的一次执行占用。"""

    task_attempt_id: str
    task_id: str
    attempt_no: int
    status: TaskAttemptStatus = TaskAttemptStatus.CREATED
    lease_owner: str | None = None
    lease_version: int | None = None
    lease_expires_at: datetime | None = None
    checkpoint_ref: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class Schedule:
    """Task 的调度配置。"""

    schedule_id: str
    schedule_type: ScheduleType
    expression: str
    timezone: str = "UTC"
    misfire_policy: str = "skip"
    enabled: bool = True
    next_fire_at: datetime | None = None
    last_fire_at: datetime | None = None


# =============================================================================
# Event（领域事件）
# =============================================================================


@dataclass(frozen=True)
class Event:
    """已经发生的不可变事实。

    Event 修正通过新 Event（如 MemoryCorrectionApplied），不修改旧 Event。
    """

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    payload_ref: str | None = None
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    ingested_at: datetime = field(default_factory=datetime.utcnow)
    content_hash: str | None = None
    trust_label: TrustLabel = TrustLabel.TRUSTED
    schema_version: str = "1.0"
    origin: str = ""
    correlation_id: str = ""
    causation_id: str = ""


# =============================================================================
# Delivery（投递）
# =============================================================================


@dataclass
class Delivery:
    """向某个目标发送内容的独立生命周期。

    与 Turn/RunAttempt 解耦，发送失败不回滚推理结果。
    Target 使用快照，避免 Endpoint 配置变化。
    """

    delivery_id: str
    target_snapshot: TargetSnapshot
    content_ref: str
    status: DeliveryStatus = DeliveryStatus.PENDING
    idempotency_key: str = ""
    scheduled_at: datetime | None = None
    platform_message_id: str | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# MemoryItem（长期记忆）
# =============================================================================


@dataclass
class MemoryItem:
    """长期认知事实，带来源、Scope、置信度和有效期。

    MemoryItem 必须保留来源，不能只保存模型总结后的无来源文本。
    """

    memory_id: str
    kind: MemoryKind = MemoryKind.FACT
    subject: str = ""
    predicate: str = ""
    value: object = None
    scope: MemoryScope = MemoryScope.OWNER_GLOBAL
    source_type: str = ""
    source_id: str = ""
    confidence: float = 0.5
    status: MemoryStatus = MemoryStatus.CANDIDATE
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes_id: str | None = None
    # 仅 kind=goal 使用
    goal_status: GoalStatus | None = None
    goal_priority: int | None = None
    goal_deadline: datetime | None = None
    goal_progress: float | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Approval（审批）
# =============================================================================


@dataclass
class Approval:
    """审批对象。审批消费在同一事务中验证 pending、授权和 action_hash 一致性。"""

    approval_id: str
    subject_type: ApprovalSubjectType
    subject_id: str
    action_hash: str
    arguments_snapshot_ref: str
    risk_level: RiskLevel = RiskLevel.LOW
    status: ApprovalStatus = ApprovalStatus.PENDING
    expires_at: datetime = field(default_factory=datetime.utcnow)
    allowed_responder_principal_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Connector（数据连接器）
# =============================================================================


@dataclass(frozen=True)
class Connector:
    """外部数据源连接器。"""

    connector_id: str
    connector_type: str
    status: ConnectorStatus = ConnectorStatus.ACTIVE
    config_ref: str = ""
    last_cursor: str | None = None
    last_polled_at: datetime | None = None


@dataclass(frozen=True)
class RawItem:
    """Connector 拉取的原始数据条目。"""

    item_id: str
    connector_id: str
    raw_content: object
    ingested_at: datetime = field(default_factory=datetime.utcnow)
    content_hash: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


# =============================================================================
# Proactive（主动推送）
# =============================================================================


@dataclass(frozen=True)
class ProactiveCandidate:
    """经过标准化和去重后的主动推送候选条目。"""

    candidate_id: str
    stream_type: str  # alert | content | context
    title: str
    summary: str = ""
    source_connector_id: str = ""
    source_item_id: str = ""
    urgency_score: float = 0.0
    relevance_score: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Trace（可观察性）
# =============================================================================


@dataclass(frozen=True)
class Trace:
    """端到端因果链。"""

    trace_id: str
    principal_id: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    attempt_id: str | None = None
    task_id: str | None = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Span:
    """Trace 中的局部执行步骤。"""

    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    name: str = ""
    kind: str = ""
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Audit:
    """谁在何时以何权限改变了什么。"""

    audit_id: str
    principal_id: str
    action: str
    target_type: str
    target_id: str
    old_state_ref: str | None = None
    new_state_ref: str | None = None
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    reason: str = ""
