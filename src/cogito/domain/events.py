"""DomainEvent entity."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any


class DomainEvent:
    """已经发生的不可变事实。"""

    def __init__(
        self,
        event_id: str | None = None,
        event_type: str = "",
        aggregate_type: str = "",
        aggregate_id: str = "",
        aggregate_version: int = 1,
        payload_ref: str | None = None,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
        ingested_at: datetime | None = None,
        content_hash: str = "",
        trust_label: str = "unverified",
        schema_version: str = "1.0",
        correlation_id: str = "",
        causation_id: str = "",
        origin: str = "system",
    ) -> None:
        self.event_id = event_id or uuid.uuid4().hex
        self.event_type = event_type
        self.aggregate_type = aggregate_type
        self.aggregate_id = aggregate_id
        self.aggregate_version = aggregate_version
        self.payload_ref = payload_ref
        self.payload = payload or {}
        self.occurred_at = occurred_at or datetime.now(UTC)
        self.ingested_at = ingested_at
        self.content_hash = content_hash
        self.trust_label = trust_label
        self.schema_version = schema_version
        self.correlation_id = correlation_id
        self.causation_id = causation_id
        self.origin = origin

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": self.aggregate_version,
            "payload_ref": self.payload_ref,
            "payload": self.payload,
            "occurred_at": self.occurred_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat() if self.ingested_at else None,
            "content_hash": self.content_hash,
            "trust_label": self.trust_label,
            "schema_version": self.schema_version,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DomainEvent:
        return cls(
            event_id=data["event_id"],
            event_type=data.get("event_type", ""),
            aggregate_type=data.get("aggregate_type", ""),
            aggregate_id=data.get("aggregate_id", ""),
            aggregate_version=data.get("aggregate_version", 1),
            payload_ref=data.get("payload_ref"),
            payload=data.get("payload", {}),
            occurred_at=datetime.fromisoformat(data["occurred_at"]) if data.get("occurred_at") else None,
            ingested_at=datetime.fromisoformat(data["ingested_at"]) if data.get("ingested_at") else None,
            content_hash=data.get("content_hash", ""),
            trust_label=data.get("trust_label", "unverified"),
            schema_version=data.get("schema_version", "1.0"),
            correlation_id=data.get("correlation_id", ""),
            causation_id=data.get("causation_id", ""),
            origin=data.get("origin", "system"),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DomainEvent):
            return NotImplemented
        return self.event_id == other.event_id

    def __repr__(self) -> str:
        return f"DomainEvent({self.event_id}, {self.event_type})"
