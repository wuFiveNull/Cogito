"""Turn and RunAttempt entities."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TurnStatus(StrEnum):
    accepted = "accepted"
    queued = "queued"
    running = "running"
    waiting_user = "waiting_user"
    waiting_external = "waiting_external"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"
    expired = "expired"


class RunAttemptStatus(StrEnum):
    created = "created"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    abandoned = "abandoned"


class Turn:
    """一次用户意图的逻辑生命周期。"""

    def __init__(
        self,
        turn_id: str | None = None,
        session_id: str = "",
        input_message_id: str = "",
        status: TurnStatus = TurnStatus.accepted,
        priority: int = 80,
        version: int = 1,
        cancel_requested_at: datetime | None = None,
        active_attempt_id: str | None = None,
        final_message_id: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.turn_id = turn_id or uuid.uuid4().hex
        self.session_id = session_id
        self.input_message_id = input_message_id
        self.status = TurnStatus(status)
        self.priority = priority
        self.version = version
        self.cancel_requested_at = cancel_requested_at
        self.active_attempt_id = active_attempt_id
        self.final_message_id = final_message_id
        self.created_at = created_at or datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "input_message_id": self.input_message_id,
            "status": self.status.value,
            "priority": self.priority,
            "version": self.version,
            "cancel_requested_at": self.cancel_requested_at.isoformat() if self.cancel_requested_at else None,
            "active_attempt_id": self.active_attempt_id,
            "final_message_id": self.final_message_id,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Turn:
        return cls(
            turn_id=data["turn_id"],
            session_id=data.get("session_id", ""),
            input_message_id=data.get("input_message_id", ""),
            status=TurnStatus(data.get("status", "accepted")),
            priority=data.get("priority", 80),
            version=data.get("version", 1),
            cancel_requested_at=datetime.fromisoformat(data["cancel_requested_at"]) if data.get("cancel_requested_at") else None,
            active_attempt_id=data.get("active_attempt_id"),
            final_message_id=data.get("final_message_id"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Turn):
            return NotImplemented
        return self.turn_id == other.turn_id

    def __repr__(self) -> str:
        return f"Turn({self.turn_id}, {self.status}, session={self.session_id})"


class RunAttempt:
    """Turn 的一次实际执行尝试。"""

    def __init__(
        self,
        attempt_id: str | None = None,
        turn_id: str = "",
        attempt_no: int = 1,
        status: RunAttemptStatus = RunAttemptStatus.created,
        checkpoint_ref: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        self.attempt_id = attempt_id or uuid.uuid4().hex
        self.turn_id = turn_id
        self.attempt_no = attempt_no
        self.status = RunAttemptStatus(status)
        self.checkpoint_ref = checkpoint_ref
        self.started_at = started_at
        self.finished_at = finished_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "turn_id": self.turn_id,
            "attempt_no": self.attempt_no,
            "status": self.status.value,
            "checkpoint_ref": self.checkpoint_ref,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunAttempt:
        return cls(
            attempt_id=data["attempt_id"],
            turn_id=data["turn_id"],
            attempt_no=data.get("attempt_no", 1),
            status=RunAttemptStatus(data.get("status", "created")),
            checkpoint_ref=data.get("checkpoint_ref"),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            finished_at=datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else None,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RunAttempt):
            return NotImplemented
        return self.attempt_id == other.attempt_id

    def __repr__(self) -> str:
        return f"RunAttempt({self.attempt_id}, turn={self.turn_id}, #{self.attempt_no}, {self.status})"
