"""MemoryItem entity."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class MemoryKind(StrEnum):
    fact = "fact"
    preference = "preference"
    episode = "episode"
    goal = "goal"
    constraint = "constraint"


class MemoryStatus(StrEnum):
    candidate = "candidate"
    confirmed = "confirmed"
    rejected = "rejected"
    expired = "expired"


class GoalStatus(StrEnum):
    active = "active"
    paused = "paused"
    completed = "completed"
    cancelled = "cancelled"
    expired = "expired"


class MemoryItem:
    """带来源、置信度和生命周期的长期认知事实。"""

    def __init__(
        self,
        memory_id: str | None = None,
        kind: MemoryKind = MemoryKind.fact,
        subject: str = "",
        predicate: str = "",
        value: str = "",
        scope: str = "",
        source_type: str = "",
        source_id: str = "",
        confidence: float = 1.0,
        status: MemoryStatus = MemoryStatus.candidate,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        supersedes_id: str | None = None,
        # Goal-specific fields
        goal_status: GoalStatus | None = None,
        goal_priority: int | None = None,
        goal_deadline: datetime | None = None,
        goal_progress: float | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.memory_id = memory_id or uuid.uuid4().hex
        self.kind = MemoryKind(kind)
        self.subject = subject
        self.predicate = predicate
        self.value = value
        self.scope = scope
        self.source_type = source_type
        self.source_id = source_id
        self.confidence = confidence
        self.status = MemoryStatus(status)
        self.valid_from = valid_from
        self.valid_to = valid_to
        self.supersedes_id = supersedes_id
        self.goal_status = GoalStatus(goal_status) if goal_status else None
        self.goal_priority = goal_priority
        self.goal_deadline = goal_deadline
        self.goal_progress = goal_progress
        self.created_at = created_at or datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "memory_id": self.memory_id,
            "kind": self.kind.value,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "scope": self.scope,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "confidence": self.confidence,
            "status": self.status.value,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "supersedes_id": self.supersedes_id,
            "created_at": self.created_at.isoformat(),
        }
        if self.kind == MemoryKind.goal:
            d["goal_status"] = self.goal_status.value if self.goal_status else None
            d["goal_priority"] = self.goal_priority
            d["goal_deadline"] = self.goal_deadline.isoformat() if self.goal_deadline else None
            d["goal_progress"] = self.goal_progress
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryItem:
        kind = MemoryKind(data.get("kind", "fact"))
        return cls(
            memory_id=data["memory_id"],
            kind=kind,
            subject=data.get("subject", ""),
            predicate=data.get("predicate", ""),
            value=data.get("value", ""),
            scope=data.get("scope", ""),
            source_type=data.get("source_type", ""),
            source_id=data.get("source_id", ""),
            confidence=data.get("confidence", 1.0),
            status=MemoryStatus(data.get("status", "candidate")),
            valid_from=datetime.fromisoformat(data["valid_from"]) if data.get("valid_from") else None,
            valid_to=datetime.fromisoformat(data["valid_to"]) if data.get("valid_to") else None,
            supersedes_id=data.get("supersedes_id"),
            goal_status=GoalStatus(data["goal_status"]) if kind == MemoryKind.goal and data.get("goal_status") else None,
            goal_priority=data.get("goal_priority") if kind == MemoryKind.goal else None,
            goal_deadline=datetime.fromisoformat(data["goal_deadline"]) if kind == MemoryKind.goal and data.get("goal_deadline") else None,
            goal_progress=data.get("goal_progress") if kind == MemoryKind.goal else None,
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MemoryItem):
            return NotImplemented
        return self.memory_id == other.memory_id

    def __repr__(self) -> str:
        return f"MemoryItem({self.memory_id}, {self.kind}, {self.status})"
