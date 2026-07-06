"""命令类型定义。

命令描述变更意图，不描述数据库操作。
正确：CancelTurn(turn_id, reason)
错误：UpdateRunStatusToCancelled

所有命令是 frozen dataclass。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cogito.domain.enums import (
    ApprovalSubjectType,
    RiskLevel,
)


@dataclass(frozen=True)
class BaseCommand:
    """所有命令的基类。"""

    idempotency_key: str = ""


# =============================================================================
# Turn 命令
# =============================================================================


@dataclass(frozen=True)
class AcceptTurn(BaseCommand):
    """接受入站消息，创建 Turn。"""

    session_id: str = ""
    input_message_id: str = ""
    channel_envelope: object = None  # ChannelEnvelope（避免循环导入，使用 object）


@dataclass(frozen=True)
class CancelTurn(BaseCommand):
    """取消正在排队或运行的 Turn。"""

    turn_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RetryTurn(BaseCommand):
    """显式重试已失败的 Turn（终态 failed → queued 的唯一入口）。"""

    turn_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ResumeTurn(BaseCommand):
    """从 waiting 状态恢复 Turn。"""

    turn_id: str = ""
    approval_id: str | None = None
    external_result: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RunTurn(BaseCommand):
    """Worker 领取 Turn 执行。"""

    turn_id: str = ""
    worker_id: str = ""


# =============================================================================
# Task 命令
# =============================================================================


@dataclass(frozen=True)
class CreateTask(BaseCommand):
    """创建持久化后台任务。"""

    task_type: str = ""
    payload_ref: str | None = None
    priority: int = 50
    scheduled_at: datetime | None = None
    retry_policy: dict[str, object] = field(default_factory=dict)
    origin: str | None = None


@dataclass(frozen=True)
class CancelTask(BaseCommand):
    """取消任务。"""

    task_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RetryTask(BaseCommand):
    """重试任务。"""

    task_id: str = ""
    next_at: datetime | None = None


# =============================================================================
# Delivery 命令
# =============================================================================


@dataclass(frozen=True)
class EnqueueDelivery(BaseCommand):
    """入队投递请求。"""

    content_ref: str = ""
    target: object = None  # TargetSnapshot
    scheduled_at: datetime | None = None


@dataclass(frozen=True)
class CancelDelivery(BaseCommand):
    """取消投递。"""

    delivery_id: str = ""


@dataclass(frozen=True)
class RetryDelivery(BaseCommand):
    """重试投递。"""

    delivery_id: str = ""


# =============================================================================
# Memory 命令
# =============================================================================


@dataclass(frozen=True)
class ProposeMemory(BaseCommand):
    """提议新的记忆候选项。"""

    candidates: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class ConfirmMemory(BaseCommand):
    """确认记忆候选项。"""

    memory_id: str = ""


@dataclass(frozen=True)
class RejectMemory(BaseCommand):
    """拒绝记忆候选项。"""

    memory_id: str = ""
    reason: str = ""


# =============================================================================
# Approval 命令
# =============================================================================


@dataclass(frozen=True)
class RequestApproval(BaseCommand):
    """请求审批。"""

    subject_type: ApprovalSubjectType = ApprovalSubjectType.TOOL_CALL
    subject_id: str = ""
    action_hash: str = ""
    arguments_snapshot_ref: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    expires_at: datetime | None = None
    allowed_responder_principal_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RespondToApproval(BaseCommand):
    """响应审批请求。"""

    approval_id: str = ""
    decision: str = ""  # "approved" | "rejected"
    responder_principal_id: str = ""


# =============================================================================
# Endpoint 命令
# =============================================================================


@dataclass(frozen=True)
class LinkEndpoint(BaseCommand):
    """绑定 Endpoint 到 Principal。"""

    endpoint_id: str = ""
    principal_id: str = ""


@dataclass(frozen=True)
class UnlinkEndpoint(BaseCommand):
    """解除 Endpoint 绑定。"""

    endpoint_id: str = ""


# =============================================================================
# Connector 命令
# =============================================================================


@dataclass(frozen=True)
class IngestConnectorBatch(BaseCommand):
    """摄取连接器批量数据。"""

    connector_id: str = ""
    raw_items: list[dict[str, object]] = field(default_factory=list)


# =============================================================================
# Command Envelope（命令包装器）
# =============================================================================


@dataclass(frozen=True)
class CommandEnvelope:
    """命令通用包装器，添加元数据和追踪上下文。

    调用方幂等键唯一约束：(actor_principal_id, command_type, idempotency_key)。
    """

    command_id: str
    command_type: str
    idempotency_key: str
    actor_principal_id: str
    target_type: str = ""
    target_id: str = ""
    expected_version: int | None = None
    payload: BaseCommand | None = None
    origin: str = ""
    trace_context: object = None  # TraceContext
    created_at: datetime = field(default_factory=datetime.utcnow)
