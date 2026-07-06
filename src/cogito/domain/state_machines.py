"""状态转移验证 —— 纯函数，不执行数据库操作。

声明式定义每种状态机允许的转移，提供 can_transition 和 validate_transition
两个纯函数用于验证。
"""

from cogito.domain.delivery import DeliveryStatus
from cogito.domain.errors import InvalidStateTransitionError
from cogito.domain.memory import MemoryStatus
from cogito.domain.task import TaskAttemptStatus, TaskStatus
from cogito.domain.turn import RunAttemptStatus, TurnStatus

# =============================================================================
# Turn 状态转移
# =============================================================================

TURN_TRANSITIONS: dict[TurnStatus, frozenset[TurnStatus]] = {
    TurnStatus.accepted: frozenset({TurnStatus.queued}),
    TurnStatus.queued: frozenset({TurnStatus.running, TurnStatus.cancelled}),
    TurnStatus.running: frozenset({
        TurnStatus.completed,
        TurnStatus.waiting_user,
        TurnStatus.waiting_external,
        TurnStatus.failed,
        TurnStatus.cancelled,
        TurnStatus.queued,  # 内部重试
    }),
    TurnStatus.waiting_user: frozenset({TurnStatus.queued, TurnStatus.cancelled}),
    TurnStatus.waiting_external: frozenset({TurnStatus.queued, TurnStatus.cancelled}),
    TurnStatus.failed: frozenset({TurnStatus.queued}),  # 仅通过 RetryTurn Command
    TurnStatus.completed: frozenset(),
    TurnStatus.cancelled: frozenset(),
}

TERMINAL_TURN_STATUSES: frozenset[TurnStatus] = frozenset({
    TurnStatus.completed,
    TurnStatus.cancelled,
})

ACTIVE_TURN_STATUSES: frozenset[TurnStatus] = frozenset({
    TurnStatus.queued,
    TurnStatus.running,
    TurnStatus.waiting_user,
    TurnStatus.waiting_external,
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
# RunAttempt 状态转移
# =============================================================================

RUN_ATTEMPT_TRANSITIONS: dict[RunAttemptStatus, frozenset[RunAttemptStatus]] = {
    RunAttemptStatus.created: frozenset({RunAttemptStatus.running}),
    RunAttemptStatus.running: frozenset({
        RunAttemptStatus.succeeded,
        RunAttemptStatus.failed,
        RunAttemptStatus.cancelled,
        RunAttemptStatus.abandoned,
    }),
    RunAttemptStatus.succeeded: frozenset(),
    RunAttemptStatus.failed: frozenset(),
    RunAttemptStatus.cancelled: frozenset(),
    RunAttemptStatus.abandoned: frozenset(),
}


def can_transition_attempt(current: RunAttemptStatus, target: RunAttemptStatus) -> bool:
    return target in RUN_ATTEMPT_TRANSITIONS.get(current, frozenset())


def validate_transition_attempt(entity_id: str, current: RunAttemptStatus, target: RunAttemptStatus) -> None:
    if not can_transition_attempt(current, target):
        raise InvalidStateTransitionError("RunAttempt", entity_id, current.value, target.value)


# =============================================================================
# Task 状态转移
# =============================================================================

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.created: frozenset({TaskStatus.queued, TaskStatus.scheduled}),
    TaskStatus.queued: frozenset({TaskStatus.running, TaskStatus.cancelled}),
    TaskStatus.scheduled: frozenset({TaskStatus.queued, TaskStatus.cancelled}),
    TaskStatus.running: frozenset({
        TaskStatus.completed,
        TaskStatus.waiting_user,
        TaskStatus.waiting_external,
        TaskStatus.failed,
        TaskStatus.cancelled,
        TaskStatus.queued,  # retry
    }),
    TaskStatus.waiting_user: frozenset({TaskStatus.queued, TaskStatus.cancelled}),
    TaskStatus.waiting_external: frozenset({TaskStatus.queued, TaskStatus.cancelled}),
    TaskStatus.failed: frozenset({TaskStatus.queued}),  # 通过 RetryTask
    TaskStatus.completed: frozenset(),
    TaskStatus.cancelled: frozenset(),
}

TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.completed,
    TaskStatus.cancelled,
})


def can_transition_task(current: TaskStatus, target: TaskStatus) -> bool:
    return target in TASK_TRANSITIONS.get(current, frozenset())


def validate_transition_task(entity_id: str, current: TaskStatus, target: TaskStatus) -> None:
    if not can_transition_task(current, target):
        raise InvalidStateTransitionError("Task", entity_id, current.value, target.value)


def is_terminal_task(status: TaskStatus) -> bool:
    return status in TERMINAL_TASK_STATUSES


# =============================================================================
# TaskAttempt 状态转移
# =============================================================================

TASK_ATTEMPT_TRANSITIONS: dict[TaskAttemptStatus, frozenset[TaskAttemptStatus]] = {
    TaskAttemptStatus.created: frozenset({TaskAttemptStatus.running}),
    TaskAttemptStatus.running: frozenset({
        TaskAttemptStatus.succeeded,
        TaskAttemptStatus.failed,
        TaskAttemptStatus.cancelled,
        TaskAttemptStatus.abandoned,
    }),
    TaskAttemptStatus.succeeded: frozenset(),
    TaskAttemptStatus.failed: frozenset(),
    TaskAttemptStatus.cancelled: frozenset(),
    TaskAttemptStatus.abandoned: frozenset(),
}


def can_transition_task_attempt(current: TaskAttemptStatus, target: TaskAttemptStatus) -> bool:
    return target in TASK_ATTEMPT_TRANSITIONS.get(current, frozenset())


def validate_transition_task_attempt(
    entity_id: str, current: TaskAttemptStatus, target: TaskAttemptStatus
) -> None:
    if not can_transition_task_attempt(current, target):
        raise InvalidStateTransitionError("TaskAttempt", entity_id, current.value, target.value)


# =============================================================================
# Delivery 状态转移
# =============================================================================

DELIVERY_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.pending: frozenset({DeliveryStatus.scheduled, DeliveryStatus.cancelled}),
    DeliveryStatus.scheduled: frozenset({DeliveryStatus.sending, DeliveryStatus.cancelled}),
    DeliveryStatus.sending: frozenset({
        DeliveryStatus.streaming,
        DeliveryStatus.finalizing,
        DeliveryStatus.sent,
        DeliveryStatus.partially_sent,
        DeliveryStatus.interrupted,
        DeliveryStatus.unknown,
        DeliveryStatus.failed,
    }),
    DeliveryStatus.streaming: frozenset({
        DeliveryStatus.finalizing,
        DeliveryStatus.interrupted,
        DeliveryStatus.failed,
    }),
    DeliveryStatus.finalizing: frozenset({
        DeliveryStatus.sent,
        DeliveryStatus.partially_sent,
        DeliveryStatus.failed,
    }),
    DeliveryStatus.partially_sent: frozenset({DeliveryStatus.retry_scheduled, DeliveryStatus.failed}),
    DeliveryStatus.interrupted: frozenset({DeliveryStatus.retry_scheduled, DeliveryStatus.failed}),
    DeliveryStatus.unknown: frozenset({DeliveryStatus.retry_scheduled, DeliveryStatus.sent, DeliveryStatus.failed}),
    DeliveryStatus.retry_scheduled: frozenset({DeliveryStatus.sending, DeliveryStatus.cancelled}),
    DeliveryStatus.failed: frozenset(),
    DeliveryStatus.sent: frozenset(),
    DeliveryStatus.cancelled: frozenset(),
}

TERMINAL_DELIVERY_STATUSES: frozenset[DeliveryStatus] = frozenset({
    DeliveryStatus.sent,
    DeliveryStatus.failed,
    DeliveryStatus.cancelled,
})


def can_transition_delivery(current: DeliveryStatus, target: DeliveryStatus) -> bool:
    return target in DELIVERY_TRANSITIONS.get(current, frozenset())


def validate_transition_delivery(entity_id: str, current: DeliveryStatus, target: DeliveryStatus) -> None:
    if not can_transition_delivery(current, target):
        raise InvalidStateTransitionError("Delivery", entity_id, current.value, target.value)


def is_terminal_delivery(status: DeliveryStatus) -> bool:
    return status in TERMINAL_DELIVERY_STATUSES


# =============================================================================
# Memory 状态转移
# =============================================================================

MEMORY_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.candidate: frozenset({
        MemoryStatus.confirmed,
        MemoryStatus.rejected,
        MemoryStatus.expired,
    }),
    MemoryStatus.confirmed: frozenset({MemoryStatus.expired}),
    MemoryStatus.rejected: frozenset(),
    MemoryStatus.expired: frozenset(),
}


def can_transition_memory(current: MemoryStatus, target: MemoryStatus) -> bool:
    return target in MEMORY_TRANSITIONS.get(current, frozenset())


def validate_transition_memory(entity_id: str, current: MemoryStatus, target: MemoryStatus) -> None:
    if not can_transition_memory(current, target):
        raise InvalidStateTransitionError("MemoryItem", entity_id, current.value, target.value)
