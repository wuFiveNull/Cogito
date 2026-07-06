"""Task and TaskAttempt entities."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    created = "created"
    scheduled = "scheduled"
    queued = "queued"
    running = "running"
    waiting_user = "waiting_user"
    waiting_external = "waiting_external"
    retry_scheduled = "retry_scheduled"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    expired = "expired"


class TaskAttemptStatus(StrEnum):
    created = "created"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    abandoned = "abandoned"


class Task:
    """可持久恢复的长期工作单位。"""

    def __init__(
        self,
        task_id: str | None = None,
        task_type: str = "",
        payload_ref: str | None = None,
        status: TaskStatus = TaskStatus.created,
        priority: int = 40,
        scheduled_at: datetime | None = None,
        retry_policy: dict[str, Any] | None = None,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
        checkpoint_ref: str | None = None,
        idempotency_key: str = "",
        origin: str = "system",
        created_at: datetime | None = None,
    ) -> None:
        self.task_id = task_id or uuid.uuid4().hex
        self.task_type = task_type
        self.payload_ref = payload_ref
        self.status = TaskStatus(status)
        self.priority = priority
        self.scheduled_at = scheduled_at
        self.retry_policy = retry_policy or {}
        self.lease_owner = lease_owner
        self.lease_expires_at = lease_expires_at
        self.checkpoint_ref = checkpoint_ref
        self.idempotency_key = idempotency_key
        self.origin = origin
        self.created_at = created_at or datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "payload_ref": self.payload_ref,
            "status": self.status.value,
            "priority": self.priority,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "retry_policy": self.retry_policy,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "checkpoint_ref": self.checkpoint_ref,
            "idempotency_key": self.idempotency_key,
            "origin": self.origin,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            task_id=data["task_id"],
            task_type=data.get("task_type", ""),
            payload_ref=data.get("payload_ref"),
            status=TaskStatus(data.get("status", "created")),
            priority=data.get("priority", 40),
            scheduled_at=datetime.fromisoformat(data["scheduled_at"]) if data.get("scheduled_at") else None,
            retry_policy=data.get("retry_policy", {}),
            lease_owner=data.get("lease_owner"),
            lease_expires_at=datetime.fromisoformat(data["lease_expires_at"]) if data.get("lease_expires_at") else None,
            checkpoint_ref=data.get("checkpoint_ref"),
            idempotency_key=data.get("idempotency_key", ""),
            origin=data.get("origin", "system"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Task):
            return NotImplemented
        return self.task_id == other.task_id

    def __repr__(self) -> str:
        return f"Task({self.task_id}, {self.task_type}, {self.status})"


class TaskAttempt:
    """Worker 对 Task 的一次执行占用。"""

    def __init__(
        self,
        task_attempt_id: str | None = None,
        task_id: str = "",
        attempt_no: int = 1,
        status: TaskAttemptStatus = TaskAttemptStatus.created,
        lease_owner: str = "",
        lease_version: int = 1,
        lease_expires_at: datetime | None = None,
        checkpoint_ref: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        self.task_attempt_id = task_attempt_id or uuid.uuid4().hex
        self.task_id = task_id
        self.attempt_no = attempt_no
        self.status = TaskAttemptStatus(status)
        self.lease_owner = lease_owner
        self.lease_version = lease_version
        self.lease_expires_at = lease_expires_at
        self.checkpoint_ref = checkpoint_ref
        self.started_at = started_at
        self.finished_at = finished_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_attempt_id": self.task_attempt_id,
            "task_id": self.task_id,
            "attempt_no": self.attempt_no,
            "status": self.status.value,
            "lease_owner": self.lease_owner,
            "lease_version": self.lease_version,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "checkpoint_ref": self.checkpoint_ref,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskAttempt:
        return cls(
            task_attempt_id=data["task_attempt_id"],
            task_id=data["task_id"],
            attempt_no=data.get("attempt_no", 1),
            status=TaskAttemptStatus(data.get("status", "created")),
            lease_owner=data.get("lease_owner", ""),
            lease_version=data.get("lease_version", 1),
            lease_expires_at=datetime.fromisoformat(data["lease_expires_at"]) if data.get("lease_expires_at") else None,
            checkpoint_ref=data.get("checkpoint_ref"),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            finished_at=datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else None,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TaskAttempt):
            return NotImplemented
        return self.task_attempt_id == other.task_attempt_id

    def __repr__(self) -> str:
        return f"TaskAttempt({self.task_attempt_id}, task={self.task_id}, #{self.attempt_no}, {self.status})"
