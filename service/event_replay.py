"""Deterministic in-memory aggregate replayers for canonical Event streams.

These projections are deliberately pure functions: no SQLite writes, caches or
legacy-table reads.  They are the runtime replacement used before a destructive
contract migration removes legacy state tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from cogito.domain.event import Event


@dataclass
class TaskState:
    task_id: str
    status: str = "created"
    task_type: str = ""
    priority: int = 0
    origin: str = ""
    lease_owner: str = ""
    lease_expires_at: int | None = None
    result_ref: str | None = None


@dataclass
class TurnState:
    turn_id: str
    status: str = "accepted"
    session_id: str = ""
    active_attempt_id: str = ""


@dataclass
class DeliveryState:
    delivery_id: str
    status: str = "pending"
    attempt_id: str = ""
    content_ref: str | None = None
    error_category: str = ""


def replay_task(events: Iterable[Event], task_id: str) -> TaskState | None:
    state: TaskState | None = None
    for event in events:
        if event.stream_type != "task" or event.stream_id != task_id:
            continue
        if event.event_type == "task.created":
            state = TaskState(
                task_id=task_id,
                status=event.outcome or "created",
                task_type=str(event.attributes.get("task_type", "")),
                priority=int(event.attributes.get("priority", 0)),
                origin=str(event.attributes.get("origin", "")),
            )
        elif state is not None and event.event_type == "task.scheduled":
            state.status = "scheduled"
        elif state is not None and event.event_type == "task.leased":
            state.status = "running"
            state.lease_owner = str(event.attributes.get("worker_id", ""))
            expiry = event.attributes.get("lease_expires_at")
            state.lease_expires_at = int(expiry) if isinstance(expiry, int) else None
        elif state is not None and event.event_type == "task.completed":
            state.status = "completed"
            state.result_ref = event.payload_ref
            state.lease_owner = ""
            state.lease_expires_at = None
        elif state is not None and event.event_type in {"task.failed", "task.cancelled"}:
            state.status = event.outcome or event.event_type.rsplit(".", 1)[-1]
            state.lease_owner = ""
            state.lease_expires_at = None
    return state


def replay_turn(events: Iterable[Event], turn_id: str) -> TurnState | None:
    state: TurnState | None = None
    for event in events:
        if event.stream_type != "turn" or event.stream_id != turn_id:
            continue
        if event.event_type == "runtime.turn.queued":
            state = TurnState(turn_id=turn_id, status="queued", session_id=event.context.session_id)
        elif state is not None and event.event_type == "runtime.turn.started":
            state.status = "running"
            state.active_attempt_id = event.context.attempt_id
        elif state is not None and event.event_type.startswith("runtime.turn."):
            terminal = event.event_type.rsplit(".", 1)[-1]
            if terminal in {"completed", "failed", "cancelled"}:
                state.status = terminal
                state.active_attempt_id = ""
    return state


def replay_delivery(events: Iterable[Event], delivery_id: str) -> DeliveryState | None:
    state: DeliveryState | None = None
    for event in events:
        if event.stream_type != "delivery" or event.stream_id != delivery_id:
            continue
        if event.event_type == "delivery.requested":
            state = DeliveryState(
                delivery_id=delivery_id,
                status="pending",
                attempt_id=event.context.attempt_id,
                content_ref=event.payload_ref,
            )
        elif state is not None and event.event_type == "delivery.started":
            state.status = "sending"
            state.attempt_id = event.context.attempt_id or state.attempt_id
        elif state is not None and event.event_type == "delivery.completed":
            state.status = "sent"
        elif state is not None and event.event_type in {"delivery.failed", "delivery.unknown"}:
            state.status = event.event_type.rsplit(".", 1)[-1]
            state.error_category = event.error_category
    return state
