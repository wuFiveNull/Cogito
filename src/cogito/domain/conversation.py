"""Conversation and Session entities."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class ConversationType(StrEnum):
    private = "private"
    group = "group"
    thread = "thread"
    web = "web"


class ConversationStatus(StrEnum):
    active = "active"
    archived = "archived"
    deleted = "deleted"


class ContextPartitionPolicy(StrEnum):
    isolated = "isolated"
    shared_profile = "shared_profile"


class SessionStatus(StrEnum):
    active = "active"
    expired = "expired"
    closed = "closed"


class Conversation:
    """平台对话容器。"""

    def __init__(
        self,
        conversation_id: str | None = None,
        conversation_endpoint_id: str = "",
        platform_conversation_id: str = "",
        conversation_type: ConversationType = ConversationType.private,
        principal_scope: str = "",
        context_partition_policy: ContextPartitionPolicy = ContextPartitionPolicy.isolated,
        status: ConversationStatus = ConversationStatus.active,
    ) -> None:
        self.conversation_id = conversation_id or uuid.uuid4().hex
        self.conversation_endpoint_id = conversation_endpoint_id
        self.platform_conversation_id = platform_conversation_id
        self.conversation_type = ConversationType(conversation_type)
        self.principal_scope = principal_scope
        self.context_partition_policy = ContextPartitionPolicy(context_partition_policy)
        self.status = ConversationStatus(status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "conversation_endpoint_id": self.conversation_endpoint_id,
            "platform_conversation_id": self.platform_conversation_id,
            "conversation_type": self.conversation_type.value,
            "principal_scope": self.principal_scope,
            "context_partition_policy": self.context_partition_policy.value,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Conversation:
        return cls(
            conversation_id=data["conversation_id"],
            conversation_endpoint_id=data["conversation_endpoint_id"],
            platform_conversation_id=data["platform_conversation_id"],
            conversation_type=ConversationType(data["conversation_type"]),
            principal_scope=data.get("principal_scope", ""),
            context_partition_policy=ContextPartitionPolicy(data.get("context_partition_policy", "isolated")),
            status=ConversationStatus(data["status"]),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Conversation):
            return NotImplemented
        return self.conversation_id == other.conversation_id

    def __repr__(self) -> str:
        return f"Conversation({self.conversation_id}, {self.conversation_type}, {self.status})"


class Session:
    """短期 Agent 上下文边界。"""

    def __init__(
        self,
        session_id: str | None = None,
        conversation_id: str = "",
        context_partition_key: str = "",
        reset_generation: int = 0,
        status: SessionStatus = SessionStatus.active,
        created_at: datetime | None = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self.conversation_id = conversation_id
        self.context_partition_key = context_partition_key or conversation_id
        self.reset_generation = reset_generation
        self.status = SessionStatus(status)
        self.created_at = created_at or datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "context_partition_key": self.context_partition_key,
            "reset_generation": self.reset_generation,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            session_id=data["session_id"],
            conversation_id=data["conversation_id"],
            context_partition_key=data.get("context_partition_key", data["conversation_id"]),
            reset_generation=data.get("reset_generation", 0),
            status=SessionStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Session):
            return NotImplemented
        return self.session_id == other.session_id

    def __repr__(self) -> str:
        return f"Session({self.session_id}, conv={self.conversation_id}, {self.status})"
