"""领域事件类型定义。

事件是已经发生的不可变事实（全用过去式命名），用于 EventBus 传播。
每个事件类型是一个 frozen dataclass，包含 event_id、occurred_at 等通用字段。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class BaseEvent:
    """所有领域事件的基类。"""

    event_id: str
    occurred_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Turn 生命周期事件
# =============================================================================


@dataclass(frozen=True)
class TurnAccepted(BaseEvent):
    turn_id: str = ""
    session_id: str = ""
    conversation_id: str = ""


@dataclass(frozen=True)
class TurnQueued(BaseEvent):
    turn_id: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class TurnStarted(BaseEvent):
    turn_id: str = ""
    attempt_id: str = ""
    attempt_no: int = 0


@dataclass(frozen=True)
class TurnCompleted(BaseEvent):
    turn_id: str = ""
    attempt_id: str = ""
    final_message_id: str = ""


@dataclass(frozen=True)
class TurnFailed(BaseEvent):
    turn_id: str = ""
    attempt_id: str = ""
    error_ref: str = ""


@dataclass(frozen=True)
class TurnCancelled(BaseEvent):
    turn_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class TurnExpired(BaseEvent):
    turn_id: str = ""


@dataclass(frozen=True)
class TurnWaitingUser(BaseEvent):
    turn_id: str = ""
    approval_id: str = ""


@dataclass(frozen=True)
class TurnWaitingExternal(BaseEvent):
    turn_id: str = ""
    condition: str = ""


@dataclass(frozen=True)
class TurnResumed(BaseEvent):
    turn_id: str = ""
    new_attempt_id: str = ""


# =============================================================================
# RunAttempt 事件
# =============================================================================


@dataclass(frozen=True)
class RunAttemptStarted(BaseEvent):
    attempt_id: str = ""
    turn_id: str = ""
    attempt_no: int = 0
    worker_id: str = ""


@dataclass(frozen=True)
class RunAttemptSucceeded(BaseEvent):
    attempt_id: str = ""
    turn_id: str = ""


@dataclass(frozen=True)
class RunAttemptFailed(BaseEvent):
    attempt_id: str = ""
    turn_id: str = ""
    error_ref: str = ""


@dataclass(frozen=True)
class RunAttemptAbandoned(BaseEvent):
    attempt_id: str = ""
    turn_id: str = ""
    worker_id: str = ""


# =============================================================================
# Task 生命周期事件
# =============================================================================


@dataclass(frozen=True)
class TaskCreated(BaseEvent):
    task_id: str = ""
    task_type: str = ""
    origin: str = ""


@dataclass(frozen=True)
class TaskQueued(BaseEvent):
    task_id: str = ""


@dataclass(frozen=True)
class TaskStarted(BaseEvent):
    task_id: str = ""
    attempt_id: str = ""
    attempt_no: int = 0


@dataclass(frozen=True)
class TaskCompleted(BaseEvent):
    task_id: str = ""
    attempt_id: str = ""


@dataclass(frozen=True)
class TaskFailed(BaseEvent):
    task_id: str = ""
    attempt_id: str = ""
    error_ref: str = ""


@dataclass(frozen=True)
class TaskCancelled(BaseEvent):
    task_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class TaskWaitingUser(BaseEvent):
    task_id: str = ""
    approval_id: str = ""


# =============================================================================
# Message 与 Delivery 事件
# =============================================================================


@dataclass(frozen=True)
class MessageReceived(BaseEvent):
    message_id: str = ""
    conversation_id: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class MessageDelivered(BaseEvent):
    message_id: str = ""
    delivery_id: str = ""


@dataclass(frozen=True)
class DeliveryScheduled(BaseEvent):
    delivery_id: str = ""
    target_endpoint_id: str = ""


@dataclass(frozen=True)
class DeliverySent(BaseEvent):
    delivery_id: str = ""
    platform_message_id: str = ""


@dataclass(frozen=True)
class DeliveryFailed(BaseEvent):
    delivery_id: str = ""
    reason: str = ""
    retryable: bool = True


# =============================================================================
# Memory 生命周期事件
# =============================================================================


@dataclass(frozen=True)
class MemoryProposed(BaseEvent):
    memory_id: str = ""
    kind: str = ""
    source_type: str = ""
    source_id: str = ""


@dataclass(frozen=True)
class MemoryConfirmed(BaseEvent):
    memory_id: str = ""


@dataclass(frozen=True)
class MemoryRejected(BaseEvent):
    memory_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class MemorySuperseded(BaseEvent):
    old_memory_id: str = ""
    new_memory_id: str = ""


# =============================================================================
# Approval 事件
# =============================================================================


@dataclass(frozen=True)
class ApprovalRequested(BaseEvent):
    approval_id: str = ""
    subject_type: str = ""
    subject_id: str = ""
    risk_level: str = ""


@dataclass(frozen=True)
class ApprovalGranted(BaseEvent):
    approval_id: str = ""
    responder_principal_id: str = ""


@dataclass(frozen=True)
class ApprovalDenied(BaseEvent):
    approval_id: str = ""
    responder_principal_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ApprovalExpiredEvent(BaseEvent):
    approval_id: str = ""


# =============================================================================
# Session 事件
# =============================================================================


@dataclass(frozen=True)
class SessionCreated(BaseEvent):
    session_id: str = ""
    conversation_id: str = ""


@dataclass(frozen=True)
class SessionClosed(BaseEvent):
    session_id: str = ""


@dataclass(frozen=True)
class SessionExpired(BaseEvent):
    session_id: str = ""


# =============================================================================
# Endpoint 事件
# =============================================================================


@dataclass(frozen=True)
class EndpointLinked(BaseEvent):
    endpoint_id: str = ""
    principal_id: str = ""


@dataclass(frozen=True)
class EndpointUnlinked(BaseEvent):
    endpoint_id: str = ""
    principal_id: str = ""


# =============================================================================
# Connector 与 Proactive 事件
# =============================================================================


@dataclass(frozen=True)
class ConnectorBatchIngested(BaseEvent):
    connector_id: str = ""
    batch_id: str = ""
    item_count: int = 0


@dataclass(frozen=True)
class ProactiveCandidateScored(BaseEvent):
    candidate_id: str = ""
    urgency_score: float = 0.0
    relevance_score: float = 0.0


@dataclass(frozen=True)
class ProactiveDecisionMade(BaseEvent):
    candidate_id: str = ""
    decision: str = ""
    reason: str = ""


# =============================================================================
# SideEffect 事件
# =============================================================================


@dataclass(frozen=True)
class SideEffectExecuted(BaseEvent):
    receipt_id: str = ""
    tool_call_id: str = ""
    effect_type: str = ""
    target: str = ""
