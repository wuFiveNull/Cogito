"""IdentityConversationService —— Principal/Endpoint/Conversation/Session 的唯一公开写入口。

SYSTEM-BOUNDARIES / 4:
- Conversation/Session 的唯一写入者是 Identity & Conversation Service。

聚合链：Principal → Endpoint → Conversation → Session。
当前实现：`SqliteIdentityConversationService`（SQLite 后端）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from cogito.domain.conversation import Conversation, ConversationType
from cogito.domain.principal import Endpoint, Principal


@dataclass(frozen=True)
class IdentityResolution:
    """身份解析结果。"""

    principal: Principal
    endpoint: Endpoint
    created_principal: bool = False
    created_endpoint: bool = False


class IdentityConversationService(Protocol):
    """身份与会话生命周期管理接口。

    唯一写入口：所有 Principal/Endpoint/Conversation/Session 的状态变更经此接口。
    """

    def resolve_identity(
        self,
        *,
        channel_type: str,
        channel_instance_id: str,
        platform_account_id: str,
        endpoint_ref: str = "",
        principal_type: str = "user",
    ) -> IdentityResolution:
        """幂等解析：找到或创建 Principal + Endpoint。"""
        ...

    def resolve_conversation(
        self,
        *,
        channel_type: str,
        channel_instance_id: str,
        endpoint_ref: str = "",
        conversation_ref: str = "",
        create: bool = True,
    ) -> tuple[Conversation, bool]:
        """找到或创建 Conversation。返回 (conversation, created)。"""
        ...

    def resolve_session(
        self,
        *,
        conversation_id: str,
        principal_id: str,
        create: bool = True,
    ) -> tuple[Any, bool]:
        """找到当前活跃 Session 或创建新 Session。返回 (session, created)。"""
        ...


class SqliteIdentityConversationService:
    """IdentityConversationService 的 SQLite 实现。"""

    def __init__(self, conn: Any) -> None:
        from cogito.store.repositories import (
            ConversationRepository,
            EndpointRepository,
            PrincipalRepository,
            SessionRepository,
        )

        self._conn = conn
        self._principal_repo = PrincipalRepository(conn)
        self._endpoint_repo = EndpointRepository(conn)
        self._conversation_repo = ConversationRepository(conn)
        self._session_repo = SessionRepository(conn)

    def resolve_identity(
        self,
        *,
        channel_type: str,
        channel_instance_id: str,
        platform_account_id: str,
        endpoint_ref: str = "",
        principal_type: str = "user",
    ) -> IdentityResolution:
        import uuid

        from cogito.domain.principal import (
            EndpointStatus,
            PrincipalStatus,
            PrincipalType,
        )

        # 1. 尝试按 platform 反向查找 Principal
        principal = self._principal_repo.find_by_platform(
            channel_type,
            platform_account_id,
        )
        created_principal = False
        if principal is None:
            principal = Principal(
                principal_id=uuid.uuid4().hex,
                principal_type=PrincipalType(
                    "external_user" if principal_type == "user" else principal_type
                ),
                status=PrincipalStatus.active,
                created_at=datetime.now(UTC),
            )
            principal = self._principal_repo.insert(principal)
            created_principal = True

        # 2. 查找或创建 Endpoint
        endpoint = self._endpoint_repo.find_by_platform(
            channel_instance_id,
            platform_account_id,
        )
        created_endpoint = False
        if endpoint is None:
            endpoint = Endpoint(
                endpoint_id=uuid.uuid4().hex,
                channel_type=channel_type,
                channel_instance_id=channel_instance_id,
                platform_account_id=platform_account_id,
                principal_id=principal.principal_id,
                endpoint_ref=endpoint_ref or uuid.uuid4().hex,
                status=EndpointStatus.active,
            )
            endpoint = self._endpoint_repo.insert(endpoint)
            principal = self._principal_repo.find(endpoint.principal_id) or principal
            created_endpoint = True

        return IdentityResolution(
            principal=principal,
            endpoint=endpoint,
            created_principal=created_principal,
            created_endpoint=created_endpoint,
        )

    def resolve_conversation(
        self,
        *,
        channel_type: str,
        channel_instance_id: str,
        endpoint_ref: str = "",
        conversation_ref: str = "",
        create: bool = True,
    ) -> tuple[Conversation, bool]:
        import uuid

        conversation = None
        if conversation_ref:
            conversation = self._conversation_repo.find_by_endpoint_ref(
                conversation_ref,
            )
        if conversation is None:
            conversation = self._conversation_repo.find_by_platform(
                channel_instance_id,
                endpoint_ref or "",
            )
        if conversation is not None:
            return conversation, False
        if not create:
            return None, False  # type: ignore[return-value]

        conversation = Conversation(
            conversation_id=uuid.uuid4().hex,
            conversation_type=ConversationType.private,
            conversation_endpoint_ref=conversation_ref or endpoint_ref or uuid.uuid4().hex,
            platform_conversation_id=conversation_ref or "",
        )
        self._conversation_repo.insert(conversation)
        return conversation, True

    def resolve_session(
        self,
        *,
        conversation_id: str,
        principal_id: str,
        create: bool = True,
    ) -> tuple[Any, bool]:
        import uuid

        from cogito.domain.conversation import Session, SessionStatus

        active = self._session_repo.find_active(conversation_id, principal_id)
        if active is not None:
            return active, False
        if not create:
            return None, False  # type: ignore[return-value]

        session = Session(
            session_id=uuid.uuid4().hex,
            conversation_id=conversation_id,
            context_partition_key=principal_id or conversation_id,
            status=SessionStatus.active,
            created_at=datetime.now(UTC),
        )
        self._session_repo.insert(session)
        return session, True
