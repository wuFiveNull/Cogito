"""Principal and Endpoint entities."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class PrincipalType(StrEnum):
    owner = "owner"
    external_user = "external_user"
    system = "system"


class PrincipalStatus(StrEnum):
    active = "active"
    blocked = "blocked"
    deleted = "deleted"


class EndpointStatus(StrEnum):
    active = "active"
    disabled = "disabled"
    deleted = "deleted"


class Principal:
    """系统认可的主体。"""

    def __init__(
        self,
        principal_id: str | None = None,
        principal_type: PrincipalType = PrincipalType.owner,
        status: PrincipalStatus = PrincipalStatus.active,
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.principal_id = principal_id or uuid.uuid4().hex
        self.principal_type = PrincipalType(principal_type)
        self.status = PrincipalStatus(status)
        self.created_at = created_at or datetime.now(timezone.utc)
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "principal_type": self.principal_type.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Principal:
        return cls(
            principal_id=data["principal_id"],
            principal_type=PrincipalType(data["principal_type"]),
            status=PrincipalStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            metadata=data.get("metadata", {}),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Principal):
            return NotImplemented
        return self.principal_id == other.principal_id

    def __repr__(self) -> str:
        return f"Principal({self.principal_id}, {self.principal_type}, {self.status})"


class Endpoint:
    """外部身份或可投递端点。"""

    def __init__(
        self,
        endpoint_id: str | None = None,
        channel_type: str = "",
        channel_instance_id: str = "",
        platform_account_id: str = "",
        principal_id: str = "",
        capabilities: list[str] | None = None,
        status: EndpointStatus = EndpointStatus.active,
        verified_at: datetime | None = None,
    ) -> None:
        self.endpoint_id = endpoint_id or uuid.uuid4().hex
        self.channel_type = channel_type
        self.channel_instance_id = channel_instance_id
        self.platform_account_id = platform_account_id
        self.principal_id = principal_id
        self.capabilities = capabilities or []
        self.status = EndpointStatus(status)
        self.verified_at = verified_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "channel_type": self.channel_type,
            "channel_instance_id": self.channel_instance_id,
            "platform_account_id": self.platform_account_id,
            "principal_id": self.principal_id,
            "capabilities": self.capabilities,
            "status": self.status.value,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Endpoint:
        return cls(
            endpoint_id=data["endpoint_id"],
            channel_type=data["channel_type"],
            channel_instance_id=data["channel_instance_id"],
            platform_account_id=data["platform_account_id"],
            principal_id=data["principal_id"],
            capabilities=data.get("capabilities", []),
            status=EndpointStatus(data["status"]),
            verified_at=datetime.fromisoformat(data["verified_at"]) if data.get("verified_at") else None,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Endpoint):
            return NotImplemented
        return self.endpoint_id == other.endpoint_id

    def __repr__(self) -> str:
        return f"Endpoint({self.endpoint_id}, {self.channel_type}, {self.status})"
