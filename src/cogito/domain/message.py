"""Message and ContentPart entities."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class MessageRole(StrEnum):
    user = "user"
    assistant = "assistant"
    tool = "tool"
    system = "system"


class MessageDirection(StrEnum):
    inbound = "inbound"
    outbound = "outbound"
    internal = "internal"


class ContentPart:
    """统一内容片段。"""

    def __init__(
        self,
        part_id: str | None = None,
        content_type: str = "text",
        inline_data: str = "",
        payload_ref: str | None = None,
        size: int = 0,
        sha256: str = "",
        metadata: dict[str, Any] | None = None,
        trust_label: str = "unverified",
        ordinal: int = 0,
    ) -> None:
        self.part_id = part_id or uuid.uuid4().hex
        self.content_type = content_type
        self.inline_data = inline_data
        self.payload_ref = payload_ref
        self.size = size
        self.sha256 = sha256
        self.metadata = metadata or {}
        self.trust_label = trust_label
        self.ordinal = ordinal

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_id": self.part_id,
            "content_type": self.content_type,
            "inline_data": self.inline_data,
            "payload_ref": self.payload_ref,
            "size": self.size,
            "sha256": self.sha256,
            "metadata": self.metadata,
            "trust_label": self.trust_label,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContentPart:
        return cls(
            part_id=data["part_id"],
            content_type=data["content_type"],
            inline_data=data.get("inline_data", ""),
            payload_ref=data.get("payload_ref"),
            size=data.get("size", 0),
            sha256=data.get("sha256", ""),
            metadata=data.get("metadata", {}),
            trust_label=data.get("trust_label", "unverified"),
            ordinal=data.get("ordinal", 0),
        )

    def __repr__(self) -> str:
        return f"ContentPart({self.part_id}, {self.content_type})"


class Message:
    """不可变的标准化消息事实。"""

    def __init__(
        self,
        message_id: str | None = None,
        conversation_id: str = "",
        session_id: str = "",
        sender_principal_id: str = "",
        sender_endpoint_id: str = "",
        role: MessageRole = MessageRole.user,
        direction: MessageDirection = MessageDirection.inbound,
        content_parts: list[ContentPart] | None = None,
        reply_to_message_id: str | None = None,
        platform_message_id: str | None = None,
        current_revision_no: int = 1,
        receive_sequence: int = 0,
        trust_label: str = "unverified",
        raw_payload_ref: str | None = None,
        reply_route: dict[str, Any] | None = None,
        capability_snapshot: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        deleted_at: datetime | None = None,
    ) -> None:
        self.message_id = message_id or uuid.uuid4().hex
        self.conversation_id = conversation_id
        self.session_id = session_id
        self.sender_principal_id = sender_principal_id
        self.sender_endpoint_id = sender_endpoint_id
        self.role = MessageRole(role)
        self.direction = MessageDirection(direction)
        self.content_parts = content_parts or []
        self.reply_to_message_id = reply_to_message_id
        self.platform_message_id = platform_message_id
        self.current_revision_no = current_revision_no
        self.receive_sequence = receive_sequence
        self.trust_label = trust_label
        self.raw_payload_ref = raw_payload_ref
        self.reply_route = reply_route or {}
        self.capability_snapshot = capability_snapshot or {}
        self.created_at = created_at or datetime.now(UTC)
        self.deleted_at = deleted_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "sender_principal_id": self.sender_principal_id,
            "sender_endpoint_id": self.sender_endpoint_id,
            "role": self.role.value,
            "direction": self.direction.value,
            "content_parts": [p.to_dict() for p in self.content_parts],
            "reply_to_message_id": self.reply_to_message_id,
            "platform_message_id": self.platform_message_id,
            "current_revision_no": self.current_revision_no,
            "receive_sequence": self.receive_sequence,
            "trust_label": self.trust_label,
            "raw_payload_ref": self.raw_payload_ref,
            "reply_route": self.reply_route,
            "capability_snapshot": self.capability_snapshot,
            "created_at": self.created_at.isoformat(),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            message_id=data["message_id"],
            conversation_id=data["conversation_id"],
            session_id=data.get("session_id", ""),
            sender_principal_id=data.get("sender_principal_id", ""),
            sender_endpoint_id=data.get("sender_endpoint_id", ""),
            role=MessageRole(data["role"]),
            direction=MessageDirection(data.get("direction", "inbound")),
            content_parts=[ContentPart.from_dict(p) for p in data.get("content_parts", [])],
            reply_to_message_id=data.get("reply_to_message_id"),
            platform_message_id=data.get("platform_message_id"),
            current_revision_no=data.get("current_revision_no", 1),
            receive_sequence=data.get("receive_sequence", 0),
            trust_label=data.get("trust_label", "unverified"),
            raw_payload_ref=data.get("raw_payload_ref"),
            reply_route=data.get("reply_route"),
            capability_snapshot=data.get("capability_snapshot"),
            created_at=datetime.fromisoformat(data["created_at"]),
            deleted_at=datetime.fromisoformat(data["deleted_at"]) if data.get("deleted_at") else None,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Message):
            return NotImplemented
        return self.message_id == other.message_id

    def __repr__(self) -> str:
        return f"Message({self.message_id}, {self.role}, seq={self.receive_sequence})"


class MessageRevision:
    """平台编辑事件产生的消息修订版本。

    同一消息的 revision_no 单调递增。
    迟到旧编辑可保存为历史，但不回退 current_revision_no。
    """

    def __init__(
        self,
        message_id: str = "",
        revision_no: int = 1,
        platform_edit_id: str = "",
        platform_revision: int = 0,
        edited_at: datetime | None = None,
        observed_at: datetime | None = None,
        editor_endpoint_id: str = "",
        content_parts: list[ContentPart] | None = None,
        raw_payload_ref: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.message_id = message_id
        self.revision_no = revision_no
        self.platform_edit_id = platform_edit_id
        self.platform_revision = platform_revision
        self.edited_at = edited_at
        self.observed_at = observed_at
        self.editor_endpoint_id = editor_endpoint_id
        self.content_parts = content_parts or []
        self.raw_payload_ref = raw_payload_ref
        self.created_at = created_at or datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "revision_no": self.revision_no,
            "platform_edit_id": self.platform_edit_id,
            "platform_revision": self.platform_revision,
            "edited_at": self.edited_at.isoformat() if self.edited_at else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "editor_endpoint_id": self.editor_endpoint_id,
            "content_parts": [p.to_dict() for p in self.content_parts],
            "raw_payload_ref": self.raw_payload_ref,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageRevision:
        return cls(
            message_id=data["message_id"],
            revision_no=data["revision_no"],
            platform_edit_id=data.get("platform_edit_id", ""),
            platform_revision=data.get("platform_revision", 0),
            edited_at=datetime.fromisoformat(data["edited_at"]) if data.get("edited_at") else None,
            observed_at=datetime.fromisoformat(data["observed_at"]) if data.get("observed_at") else None,
            editor_endpoint_id=data.get("editor_endpoint_id", ""),
            content_parts=[ContentPart.from_dict(p) for p in data.get("content_parts", [])],
            raw_payload_ref=data.get("raw_payload_ref"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )

    def __repr__(self) -> str:
        return f"MessageRevision({self.message_id}, rev={self.revision_no})"
