"""MemoryItem entity — 长期记忆领域实体。

DOMAIN-CONTRACTS / 1.13 MemoryItem
MEMORY-LIFECYCLE / 1-14
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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


class ScopeType(StrEnum):
    global_ = "global"
    user = "user"
    conversation = "conversation"
    session = "session"
    task = "task"


class Explicitness(StrEnum):
    explicit_user_statement = "explicit_user_statement"
    confirmed_inference = "confirmed_inference"
    model_inference = "model_inference"
    external_source = "external_source"
    system_generated = "system_generated"


class MemoryItem:
    """带来源、置信度和生命周期的长期认知事实。

    DOMAIN-CONTRACTS / 1.13 MemoryItem：
    - 每个 MemoryItem 绑定 principal 和 scope
    - canonical_key 用于稳定去重和覆盖
    - version 支持乐观锁
    - 来源必须可追溯到消息或外部源
    """

    def __init__(
        self,
        memory_id: str | None = None,
        kind: MemoryKind = MemoryKind.fact,
        subject: str = "",
        predicate: str = "",
        value: str = "",
        # Scope
        principal_id: str = "",
        scope_type: str = "",
        scope_id: str = "",
        scope: str = "",  # 向后兼容
        # 规范键
        canonical_key: str = "",
        # 来源
        source_type: str = "",
        source_id: str = "",
        # 可信度
        explicitness: str = "",
        confidence: float = 1.0,
        importance: float = 0.5,
        # 生命周期治理（G1）
        reinforcement: int = 0,
        exposure_count: int = 0,
        emotional_weight: float = 0.5,
        last_retrieved_at: datetime | None = None,
        retrieval_count: int = 0,
        retrieval_weight: float = 1.0,
        decay_rate: float = 1.0,
        embedding_model: str = "",
        embedding_version: str = "",
        half_life_days: float = 365.0,
        last_weight_update: datetime | None = None,
        # 确认信息
        confirmation_method: str = "",
        confirmed_by: str = "",
        confirmed_at: datetime | None = None,
        # 生命周期
        status: MemoryStatus = MemoryStatus.candidate,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        supersedes_id: str | None = None,
        # 乐观锁
        version: int = 1,
        # 软删除
        deleted_at: datetime | None = None,
        # Goal-specific fields
        goal_status: GoalStatus | None = None,
        goal_priority: int | None = None,
        goal_deadline: datetime | None = None,
        goal_progress: float | None = None,
        # Audit
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        self.memory_id = memory_id or uuid.uuid4().hex
        self.kind = MemoryKind(kind)
        self.subject = subject
        self.predicate = predicate
        self.value = value
        self.principal_id = principal_id
        self.scope_type = scope_type
        self.scope_id = scope_id
        self.scope = scope
        self.canonical_key = canonical_key
        self.source_type = source_type
        self.source_id = source_id
        self.explicitness = explicitness
        self.confidence = self._validate_confidence(confidence)
        self.importance = self._validate_importance(importance)
        # G1: 生命周期治理字段
        self.reinforcement = reinforcement
        self.exposure_count = exposure_count
        self.emotional_weight = emotional_weight
        self.last_retrieved_at = last_retrieved_at
        self.retrieval_count = retrieval_count
        self.retrieval_weight = retrieval_weight
        self.decay_rate = decay_rate
        self.embedding_model = embedding_model
        self.embedding_version = embedding_version
        self.half_life_days = half_life_days
        self.last_weight_update = last_weight_update
        self.confirmation_method = confirmation_method
        self.confirmed_by = confirmed_by
        self.confirmed_at = confirmed_at
        self.status = MemoryStatus(status)
        self.valid_from = valid_from
        self.valid_to = valid_to
        self.supersedes_id = supersedes_id
        self.version = version
        self.deleted_at = deleted_at
        self.goal_status = GoalStatus(goal_status) if goal_status else None
        self.goal_priority = goal_priority
        self.goal_deadline = goal_deadline
        self.goal_progress = goal_progress
        self.created_at = created_at or datetime.now(UTC)
        self.updated_at = updated_at

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "memory_id": self.memory_id,
            "kind": self.kind.value,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "principal_id": self.principal_id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "scope": self.scope,
            "canonical_key": self.canonical_key,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "explicitness": self.explicitness,
            "confidence": self.confidence,
            "importance": self.importance,
            "confirmation_method": self.confirmation_method,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "status": self.status.value,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "supersedes_id": self.supersedes_id,
            "version": self.version,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
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
            memory_id=data.get("memory_id", ""),
            kind=kind,
            subject=data.get("subject", ""),
            predicate=data.get("predicate", ""),
            value=data.get("value", ""),
            principal_id=data.get("principal_id", ""),
            scope_type=data.get("scope_type", ""),
            scope_id=data.get("scope_id", ""),
            scope=data.get("scope", ""),
            canonical_key=data.get("canonical_key", ""),
            source_type=data.get("source_type", ""),
            source_id=data.get("source_id", ""),
            explicitness=data.get("explicitness", ""),
            confidence=data.get("confidence", 1.0),
            importance=data.get("importance", 0.5),
            confirmation_method=data.get("confirmation_method", ""),
            confirmed_by=data.get("confirmed_by", ""),
            confirmed_at=(
                _parse_dt(data["confirmed_at"]) if data.get("confirmed_at") else None
            ),
            status=MemoryStatus(data.get("status", "candidate")),
            valid_from=_parse_dt(data["valid_from"]) if data.get("valid_from") else None,
            valid_to=_parse_dt(data["valid_to"]) if data.get("valid_to") else None,
            supersedes_id=data.get("supersedes_id"),
            version=data.get("version", 1),
            deleted_at=_parse_dt(data["deleted_at"]) if data.get("deleted_at") else None,
            goal_status=(
                GoalStatus(data["goal_status"])
                if kind == MemoryKind.goal and data.get("goal_status")
                else None
            ),
            goal_priority=data.get("goal_priority") if kind == MemoryKind.goal else None,
            goal_deadline=(
                _parse_dt(data["goal_deadline"])
                if kind == MemoryKind.goal and data.get("goal_deadline")
                else None
            ),
            goal_progress=data.get("goal_progress") if kind == MemoryKind.goal else None,
            created_at=_parse_dt(data["created_at"]) if data.get("created_at") else None,
            updated_at=_parse_dt(data["updated_at"]) if data.get("updated_at") else None,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MemoryItem):
            return NotImplemented
        return self.memory_id == other.memory_id

    def __repr__(self) -> str:
        return (
            f"MemoryItem({self.memory_id}, {self.kind}, {self.status}, "
            f"key={self.canonical_key!r})"
        )

    @staticmethod
    def _validate_confidence(value: float) -> float:
        """校验并裁剪 confidence 范围 [0.0, 1.0]。
        MEMORY-LIFECYCLE — 置信度必须在有效范围。
        """
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value

    @staticmethod
    def _validate_importance(value: float) -> float:
        """校验并裁剪 importance 范围 [0.0, 1.0]。
        MEMORY-LIFECYCLE — 重要性必须在有效范围。
        """
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value


def _parse_dt(s: str) -> datetime | None:
    """安全解析 datetime 字符串，支持 ISO 格式。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
