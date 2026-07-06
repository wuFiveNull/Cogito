"""领域枚举定义 —— 所有状态枚举、角色枚举和常量。

本模块定义系统中所有稳定状态的枚举值，是领域模型的基石。
"""

from enum import StrEnum


# =============================================================================
# Principal（主体）相关
# =============================================================================


class PrincipalType(StrEnum):
    """主体类型。即使单用户系统也必须显式建模。"""

    OWNER = "owner"
    EXTERNAL_USER = "external_user"
    SYSTEM = "system"


class PrincipalStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    DELETED = "deleted"


# =============================================================================
# Conversation（对话）相关
# =============================================================================


class ConversationType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    THREAD = "thread"
    WEB = "web"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"


# =============================================================================
# Message（消息）相关
# =============================================================================


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


# =============================================================================
# Turn 与 RunAttempt（执行对象）相关
# =============================================================================


class TurnStatus(StrEnum):
    """Turn 状态机。

    accepted → queued → running → completed
                          ├→ waiting_user → queued
                          ├→ waiting_external → queued
                          ├→ failed（需 RetryTurn 回到 queued）
                          ├→ cancelled
                          └→ expired
    """

    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_EXTERNAL = "waiting_external"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class RunAttemptStatus(StrEnum):
    """RunAttempt 状态机。

    created → running → succeeded | failed | cancelled | abandoned
    """

    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


# =============================================================================
# Task（任务）相关
# =============================================================================


class TaskStatus(StrEnum):
    """Task 状态机。

    created → queued/scheduled → running → completed
                                ├→ waiting_user → queued
                                ├→ waiting_external → queued
                                ├→ failed
                                ├→ cancelled
                                └→ expired
    运行中可通过 retry 回到 queued。
    """

    CREATED = "created"
    QUEUED = "queued"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_EXTERNAL = "waiting_external"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class TaskAttemptStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class ScheduleType(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"
    CALENDAR = "calendar"
    CONDITION = "condition"


# =============================================================================
# Delivery（投递）相关
# =============================================================================


class DeliveryStatus(StrEnum):
    """Delivery 状态机。

    pending → scheduled → sending → sent
                       ├→ streaming → finalizing → sent
                       ├→ partially_sent
                       ├→ interrupted/unknown
                       ├→ retry_scheduled
                       ├→ failed
                       └→ cancelled
    """

    PENDING = "pending"
    SCHEDULED = "scheduled"
    SENDING = "sending"
    STREAMING = "streaming"
    FINALIZING = "finalizing"
    SENT = "sent"
    PARTIALLY_SENT = "partially_sent"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReplyMode(StrEnum):
    """投递回复模式。"""

    DIRECT = "direct"
    PLACEHOLDER_THEN_APPEND = "placeholder_then_append"
    STREAMING = "streaming"


# =============================================================================
# Memory（记忆）相关
# =============================================================================


class MemoryKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    EPISODE = "episode"
    GOAL = "goal"
    CONSTRAINT = "constraint"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MemoryScope(StrEnum):
    OWNER_GLOBAL = "owner_global"
    PRINCIPAL_GLOBAL = "principal_global"
    CHANNEL_SCOPED = "channel_scoped"
    CONVERSATION_SCOPED = "conversation_scoped"
    SESSION_SCOPED = "session_scoped"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# =============================================================================
# Approval（审批）相关
# =============================================================================


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalSubjectType(StrEnum):
    TURN = "turn"
    TASK = "task"
    TOOL_CALL = "tool_call"
    COMMAND = "command"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# =============================================================================
# Connector（连接器）相关
# =============================================================================


class ConnectorStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    DISABLED = "disabled"


# =============================================================================
# 内容与信任
# =============================================================================


class ContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"
    LOCATION = "location"
    QUOTE = "quote"
    BUTTON_ACTION = "button_action"
    STRUCTURED = "structured"


class TrustLabel(StrEnum):
    TRUSTED = "trusted"
    UNVERIFIED = "unverified"
    SUSPICIOUS = "suspicious"
    SPOOFED = "spoofed"


# =============================================================================
# Tool（工具）相关
# =============================================================================


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    WRITE = "write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


# =============================================================================
# 主动推送
# =============================================================================


class ProactiveDecision(StrEnum):
    SEND_NOW = "send_now"
    SEND_LATER = "send_later"
    ADD_TO_DIGEST = "add_to_digest"
    SILENT_PROCESS = "silent_process"
    CREATE_TASK = "create_task"
    ASK_PERMISSION = "ask_permission"
    DISCARD = "discard"
