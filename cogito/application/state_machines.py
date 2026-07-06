"""状态转移验证 —— 纯函数，不执行数据库操作。

声明式定义每种状态机允许的转移，提供 can_transition 和 validate_transition
两个纯函数用于验证。非法转移抛出 InvalidStateTransitionError。
"""

from cogito.domain.enums import (
    ApprovalStatus,
    DeliveryStatus,
    MemoryStatus,
    RunAttemptStatus,
    TaskAttemptStatus,
    TaskStatus,
    TurnStatus,
)
from cogito.domain.errors import InvalidStateTransitionError


# =============================================================================
# Turn 状态转移表
# =============================================================================

TURN_TRANSITIONS: dict[TurnStatus, frozenset[TurnStatus]] = {
    TurnStatus.ACCEPTED: frozenset({TurnStatus.QUEUED}),
    TurnStatus.QUEUED: frozenset({TurnStatus.RUNNING, TurnStatus.CANCELLED, TurnStatus.EXPIRED}),
    TurnStatus.RUNNING: frozenset({
        TurnStatus.COMPLETED,
        TurnStatus.WAITING_USER,
        TurnStatus.WAITING_EXTERNAL,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
        TurnStatus.QUEUED,  # 内部重试
        TurnStatus.EXPIRED,
    }),
    TurnStatus.WAITING_USER: frozenset({TurnStatus.QUEUED, TurnStatus.CANCELLED, TurnStatus.EXPIRED}),
    TurnStatus.WAITING_EXTERNAL: frozenset({TurnStatus.QUEUED, TurnStatus.CANCELLED, TurnStatus.EXPIRED}),
    TurnStatus.FAILED: frozenset({TurnStatus.QUEUED}),  # 仅通过 RetryTurn Command
    TurnStatus.COMPLETED: frozenset(),  # 终态
    TurnStatus.CANCELLED: frozenset(),  # 终态
    TurnStatus.EXPIRED: frozenset(),  # 终态
}

TERMINAL_TURN_STATUSES: frozenset[TurnStatus] = frozenset({
    TurnStatus.COMPLETED,
    TurnStatus.CANCELLED,
    TurnStatus.EXPIRED,
})

ACTIVE_TURN_STATUSES: frozenset[TurnStatus] = frozenset({
    TurnStatus.QUEUED,
    TurnStatus.RUNNING,
    TurnStatus.WAITING_USER,
    TurnStatus.WAITING_EXTERNAL,
})


def can_transition_turn(current: TurnStatus, target: TurnStatus) -> bool:
    return target in TURN_TRANSITIONS.get(current, frozenset())


def validate_transition_turn(entity_id: str, current: TurnStatus, target: TurnStatus) -> None:
    if not can_transition_turn(current, target):
        raise InvalidStateTransitionError("Turn", entity_id, current.value, target.value)


def is_terminal_turn(status: TurnStatus) -> bool:
    return status in TERMINAL_TURN_STATUSES


def is_active_turn(status: TurnStatus) -> bool:
    return status in ACTIVE_TURN_STATUSES


# =============================================================================
# RunAttempt 状态转移表
# =============================================================================

RUN_ATTEMPT_TRANSITIONS: dict[RunAttemptStatus, frozenset[RunAttemptStatus]] = {
    RunAttemptStatus.CREATED: frozenset({RunAttemptStatus.RUNNING}),
    RunAttemptStatus.RUNNING: frozenset({
        RunAttemptStatus.SUCCEEDED,
        RunAttemptStatus.FAILED,
        RunAttemptStatus.CANCELLED,
        RunAttemptStatus.ABANDONED,
    }),
    RunAttemptStatus.SUCCEEDED: frozenset(),
    RunAttemptStatus.FAILED: frozenset(),
    RunAttemptStatus.CANCELLED: frozenset(),
    RunAttemptStatus.ABANDONED: frozenset(),
}


def can_transition_attempt(current: RunAttemptStatus, target: RunAttemptStatus) -> bool:
    return target in RUN_ATTEMPT_TRANSITIONS.get(current, frozenset())


def validate_transition_attempt(entity_id: str, current: RunAttemptStatus, target: RunAttemptStatus) -> None:
    if not can_transition_attempt(current, target):
        raise InvalidStateTransitionError("RunAttempt", entity_id, current.value, target.value)


# =============================================================================
# Task 状态转移表
# =============================================================================

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.QUEUED, TaskStatus.SCHEDULED}),
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.EXPIRED}),
    TaskStatus.SCHEDULED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.COMPLETED,
        TaskStatus.QUEUED,  # retry
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_EXTERNAL,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.EXPIRED,
    }),
    TaskStatus.WAITING_USER: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}),
    TaskStatus.WAITING_EXTERNAL: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED}),  # 仅通过 RetryTask
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.EXPIRED: frozenset(),
}

TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.CANCELLED,
    TaskStatus.EXPIRED,
})


def can_transition_task(current: TaskStatus, target: TaskStatus) -> bool:
    return target in TASK_TRANSITIONS.get(current, frozenset())


def validate_transition_task(entity_id: str, current: TaskStatus, target: TaskStatus) -> None:
    if not can_transition_task(current, target):
        raise InvalidStateTransitionError("Task", entity_id, current.value, target.value)


def is_terminal_task(status: TaskStatus) -> bool:
    return status in TERMINAL_TASK_STATUSES


# =============================================================================
# TaskAttempt 状态转移表
# =============================================================================

TASK_ATTEMPT_TRANSITIONS: dict[TaskAttemptStatus, frozenset[TaskAttemptStatus]] = {
    TaskAttemptStatus.CREATED: frozenset({TaskAttemptStatus.RUNNING}),
    TaskAttemptStatus.RUNNING: frozenset({
        TaskAttemptStatus.SUCCEEDED,
        TaskAttemptStatus.FAILED,
        TaskAttemptStatus.CANCELLED,
        TaskAttemptStatus.ABANDONED,
    }),
    TaskAttemptStatus.SUCCEEDED: frozenset(),
    TaskAttemptStatus.FAILED: frozenset(),
    TaskAttemptStatus.CANCELLED: frozenset(),
    TaskAttemptStatus.ABANDONED: frozenset(),
}


def can_transition_task_attempt(current: TaskAttemptStatus, target: TaskAttemptStatus) -> bool:
    return target in TASK_ATTEMPT_TRANSITIONS.get(current, frozenset())


def validate_transition_task_attempt(
    entity_id: str, current: TaskAttemptStatus, target: TaskAttemptStatus
) -> None:
    if not can_transition_task_attempt(current, target):
        raise InvalidStateTransitionError("TaskAttempt", entity_id, current.value, target.value)


# =============================================================================
# Delivery 状态转移表
# =============================================================================

DELIVERY_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.PENDING: frozenset({DeliveryStatus.SCHEDULED, DeliveryStatus.CANCELLED}),
    DeliveryStatus.SCHEDULED: frozenset({DeliveryStatus.SENDING, DeliveryStatus.CANCELLED}),
    DeliveryStatus.SENDING: frozenset({
        DeliveryStatus.STREAMING,
        DeliveryStatus.FINALIZING,
        DeliveryStatus.SENT,
        DeliveryStatus.PARTIALLY_SENT,
        DeliveryStatus.INTERRUPTED,
        DeliveryStatus.UNKNOWN,
        DeliveryStatus.FAILED,
    }),
    DeliveryStatus.STREAMING: frozenset({
        DeliveryStatus.FINALIZING,
        DeliveryStatus.INTERRUPTED,
        DeliveryStatus.FAILED,
    }),
    DeliveryStatus.FINALIZING: frozenset({
        DeliveryStatus.SENT,
        DeliveryStatus.PARTIALLY_SENT,
        DeliveryStatus.FAILED,
    }),
    DeliveryStatus.PARTIALLY_SENT: frozenset({DeliveryStatus.RETRY_SCHEDULED, DeliveryStatus.FAILED}),
    DeliveryStatus.INTERRUPTED: frozenset({DeliveryStatus.RETRY_SCHEDULED, DeliveryStatus.FAILED}),
    DeliveryStatus.UNKNOWN: frozenset({DeliveryStatus.RETRY_SCHEDULED, DeliveryStatus.SENT, DeliveryStatus.FAILED}),
    DeliveryStatus.RETRY_SCHEDULED: frozenset({DeliveryStatus.SENDING, DeliveryStatus.CANCELLED}),
    DeliveryStatus.FAILED: frozenset(),
    DeliveryStatus.SENT: frozenset(),
    DeliveryStatus.CANCELLED: frozenset(),
}

TERMINAL_DELIVERY_STATUSES: frozenset[DeliveryStatus] = frozenset({
    DeliveryStatus.SENT,
    DeliveryStatus.FAILED,
    DeliveryStatus.CANCELLED,
})


def can_transition_delivery(current: DeliveryStatus, target: DeliveryStatus) -> bool:
    return target in DELIVERY_TRANSITIONS.get(current, frozenset())


def validate_transition_delivery(entity_id: str, current: DeliveryStatus, target: DeliveryStatus) -> None:
    if not can_transition_delivery(current, target):
        raise InvalidStateTransitionError("Delivery", entity_id, current.value, target.value)


def is_terminal_delivery(status: DeliveryStatus) -> bool:
    return status in TERMINAL_DELIVERY_STATUSES


# =============================================================================
# Approval 状态转移表
# =============================================================================

APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset({
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
    }),
    ApprovalStatus.APPROVED: frozenset(),
    ApprovalStatus.REJECTED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
}


def can_transition_approval(current: ApprovalStatus, target: ApprovalStatus) -> bool:
    return target in APPROVAL_TRANSITIONS.get(current, frozenset())


def validate_transition_approval(entity_id: str, current: ApprovalStatus, target: ApprovalStatus) -> None:
    if not can_transition_approval(current, target):
        raise InvalidStateTransitionError("Approval", entity_id, current.value, target.value)


# =============================================================================
# Memory 状态转移表
# =============================================================================

MEMORY_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.CANDIDATE: frozenset({
        MemoryStatus.CONFIRMED,
        MemoryStatus.REJECTED,
        MemoryStatus.EXPIRED,
    }),
    MemoryStatus.CONFIRMED: frozenset({MemoryStatus.EXPIRED}),
    MemoryStatus.REJECTED: frozenset(),
    MemoryStatus.EXPIRED: frozenset(),
}


def can_transition_memory(current: MemoryStatus, target: MemoryStatus) -> bool:
    return target in MEMORY_TRANSITIONS.get(current, frozenset())


def validate_transition_memory(entity_id: str, current: MemoryStatus, target: MemoryStatus) -> None:
    if not can_transition_memory(current, target):
        raise InvalidStateTransitionError("MemoryItem", entity_id, current.value, target.value)
