"""Delivery and DeliveryAttempt entities."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class DeliveryStatus(StrEnum):
    pending = "pending"
    scheduled = "scheduled"
    sending = "sending"
    sent = "sent"
    partially_sent = "partially_sent"
    streaming = "streaming"
    finalizing = "finalizing"
    interrupted = "interrupted"
    unknown = "unknown"
    retry_scheduled = "retry_scheduled"
    failed = "failed"
    cancelled = "cancelled"


class DeliveryAttemptStatus(StrEnum):
    created = "created"
    sending = "sending"
    succeeded = "succeeded"
    failed = "failed"


class Delivery:
    """向某个目标发送内容的独立生命周期。"""

    def __init__(
        self,
        delivery_id: str | None = None,
        target_snapshot: dict[str, Any] | None = None,
        content_ref: str | None = None,
        status: DeliveryStatus = DeliveryStatus.pending,
        idempotency_key: str = "",
        scheduled_at: datetime | None = None,
        platform_message_id: str | None = None,
        last_error: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.delivery_id = delivery_id or uuid.uuid4().hex
        self.target_snapshot = target_snapshot or {}
        self.content_ref = content_ref
        self.status = DeliveryStatus(status)
        self.idempotency_key = idempotency_key
        self.scheduled_at = scheduled_at
        self.platform_message_id = platform_message_id
        self.last_error = last_error
        self.created_at = created_at or datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "target_snapshot": self.target_snapshot,
            "content_ref": self.content_ref,
            "status": self.status.value,
            "idempotency_key": self.idempotency_key,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "platform_message_id": self.platform_message_id,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Delivery:
        return cls(
            delivery_id=data["delivery_id"],
            target_snapshot=data.get("target_snapshot", {}),
            content_ref=data.get("content_ref"),
            status=DeliveryStatus(data.get("status", "pending")),
            idempotency_key=data.get("idempotency_key", ""),
            scheduled_at=datetime.fromisoformat(data["scheduled_at"]) if data.get("scheduled_at") else None,
            platform_message_id=data.get("platform_message_id"),
            last_error=data.get("last_error"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Delivery):
            return NotImplemented
        return self.delivery_id == other.delivery_id

    def __repr__(self) -> str:
        return f"Delivery({self.delivery_id}, {self.status})"


class DeliveryAttempt:
    """Delivery 的一次发送尝试。"""

    def __init__(
        self,
        attempt_id: str | None = None,
        delivery_id: str = "",
        attempt_no: int = 1,
        status: DeliveryAttemptStatus = DeliveryAttemptStatus.created,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        platform_receipt: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.attempt_id = attempt_id or uuid.uuid4().hex
        self.delivery_id = delivery_id
        self.attempt_no = attempt_no
        self.status = DeliveryAttemptStatus(status)
        self.started_at = started_at
        self.finished_at = finished_at
        self.platform_receipt = platform_receipt or {}
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "delivery_id": self.delivery_id,
            "attempt_no": self.attempt_no,
            "status": self.status.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "platform_receipt": self.platform_receipt,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeliveryAttempt:
        return cls(
            attempt_id=data["attempt_id"],
            delivery_id=data["delivery_id"],
            attempt_no=data.get("attempt_no", 1),
            status=DeliveryAttemptStatus(data.get("status", "created")),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            finished_at=datetime.fromisoformat(data["finished_at"]) if data.get("finished_at") else None,
            platform_receipt=data.get("platform_receipt", {}),
            error=data.get("error"),
        )

    def __repr__(self) -> str:
        return f"DeliveryAttempt({self.attempt_id}, delivery={self.delivery_id}, #{self.attempt_no}, {self.status})"
