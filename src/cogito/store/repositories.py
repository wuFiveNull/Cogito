"""Repository implementations — data access for each aggregate.

每个 Repository 负责一个聚合的持久化，返回领域对象。
所有 Repository 共享同一个 sqlite3.Connection，由 UnitOfWork 管理事务。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from cogito.domain.conversation import (
    ContextPartitionPolicy,
    Conversation,
    ConversationStatus,
    ConversationType,
    Session,
    SessionStatus,
)
from cogito.domain.events import DomainEvent
from cogito.domain.message import ContentPart, Message
from cogito.domain.principal import (
    Endpoint,
    EndpointStatus,
    Principal,
    PrincipalStatus,
    PrincipalType,
)
from cogito.domain.turn import Turn, TurnStatus
from cogito.store.time_utils import epoch_ms

# =============================================================================
# InboxRepository
# =============================================================================


class InboxRecord:
    """inbound_inbox 表记录的值对象。"""

    def __init__(
        self,
        channel_instance_id: str,
        platform_event_id: str,
        status: str = "received",
        message_id: str | None = None,
        received_at: str | None = None,
    ) -> None:
        self.channel_instance_id = channel_instance_id
        self.platform_event_id = platform_event_id
        self.status = status
        self.message_id = message_id
        self.received_at = received_at


class InboxRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, channel_instance_id: str, platform_event_id: str) -> InboxRecord | None:
        row = self._conn.execute(
            "SELECT channel_instance_id, platform_event_id, status, message_id, received_at "
            "FROM inbound_inbox WHERE channel_instance_id=? AND platform_event_id=?",
            (channel_instance_id, platform_event_id),
        ).fetchone()
        if row is None:
            return None
        return InboxRecord(
            channel_instance_id=row["channel_instance_id"],
            platform_event_id=row["platform_event_id"],
            status=row["status"],
            message_id=row["message_id"],
            received_at=row["received_at"],
        )

    def insert(self, record: InboxRecord) -> None:
        self._conn.execute(
            "INSERT INTO inbound_inbox (channel_instance_id, platform_event_id, status, message_id, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (record.channel_instance_id, record.platform_event_id,
             record.status, record.message_id, record.received_at),
        )


# =============================================================================
# PrincipalRepository
# =============================================================================


class PrincipalRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, principal_id: str) -> Principal | None:
        row = self._conn.execute(
            "SELECT principal_id, principal_type, status, created_at, metadata "
            "FROM principals WHERE principal_id=?",
            (principal_id,),
        ).fetchone()
        if row is None:
            return None
        return Principal(
            principal_id=row["principal_id"],
            principal_type=PrincipalType(row["principal_type"]),
            status=PrincipalStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def insert(self, principal: Principal) -> None:
        self._conn.execute(
            "INSERT INTO principals (principal_id, principal_type, status, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (principal.principal_id, principal.principal_type.value,
             principal.status.value, principal.created_at.isoformat(),
             "{}"),
        )

    def find_by_platform(self, channel_type: str, platform_account_id: str) -> Principal | None:
        """通过 Endpoint 反向查找 Principal。"""
        row = self._conn.execute(
            "SELECT p.principal_id, p.principal_type, p.status, p.created_at, p.metadata "
            "FROM principals p "
            "JOIN endpoints e ON e.principal_id = p.principal_id "
            "WHERE e.channel_type=? AND e.platform_account_id=? AND e.status='active'",
            (channel_type, platform_account_id),
        ).fetchone()
        if row is None:
            return None
        return Principal(
            principal_id=row["principal_id"],
            principal_type=PrincipalType(row["principal_type"]),
            status=PrincipalStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# =============================================================================
# EndpointRepository
# =============================================================================


class EndpointRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, endpoint_id: str) -> Endpoint | None:
        row = self._conn.execute(
            "SELECT endpoint_id, channel_type, channel_instance_id, platform_account_id, "
            "principal_id, endpoint_ref, capabilities, status, verified_at "
            "FROM endpoints WHERE endpoint_id=?",
            (endpoint_id,),
        ).fetchone()
        if row is None:
            return None
        return Endpoint(
            endpoint_id=row["endpoint_id"],
            channel_type=row["channel_type"],
            channel_instance_id=row["channel_instance_id"],
            platform_account_id=row["platform_account_id"],
            principal_id=row["principal_id"],
            endpoint_ref=row["endpoint_ref"],
            status=EndpointStatus(row["status"]),
            verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None,
        )

    def insert(self, endpoint: Endpoint) -> None:
        self._conn.execute(
            "INSERT INTO endpoints (endpoint_id, channel_type, channel_instance_id, "
            "platform_account_id, principal_id, endpoint_ref, capabilities, status, verified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (endpoint.endpoint_id, endpoint.channel_type, endpoint.channel_instance_id,
             endpoint.platform_account_id, endpoint.principal_id,
             endpoint.endpoint_ref,
             "[]", endpoint.status.value, None),
        )

    def find_by_platform(
        self, channel_instance_id: str, platform_account_id: str
    ) -> Endpoint | None:
        row = self._conn.execute(
            "SELECT endpoint_id, channel_type, channel_instance_id, platform_account_id, "
            "principal_id, endpoint_ref, capabilities, status, verified_at "
            "FROM endpoints WHERE channel_instance_id=? AND platform_account_id=?",
            (channel_instance_id, platform_account_id),
        ).fetchone()
        if row is None:
            return None
        return Endpoint(
            endpoint_id=row["endpoint_id"],
            channel_type=row["channel_type"],
            channel_instance_id=row["channel_instance_id"],
            platform_account_id=row["platform_account_id"],
            principal_id=row["principal_id"],
            endpoint_ref=row["endpoint_ref"],
            status=EndpointStatus(row["status"]),
            verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None,
        )

    def find_by_ref(self, endpoint_ref: str) -> Endpoint | None:
        """通过 endpoint_ref 查找端点。"""
        if not endpoint_ref:
            return None
        row = self._conn.execute(
            "SELECT endpoint_id, channel_type, channel_instance_id, platform_account_id, "
            "principal_id, endpoint_ref, capabilities, status, verified_at "
            "FROM endpoints WHERE endpoint_ref=?",
            (endpoint_ref,),
        ).fetchone()
        if row is None:
            return None
        return Endpoint(
            endpoint_id=row["endpoint_id"],
            channel_type=row["channel_type"],
            channel_instance_id=row["channel_instance_id"],
            platform_account_id=row["platform_account_id"],
            principal_id=row["principal_id"],
            endpoint_ref=row["endpoint_ref"],
            status=EndpointStatus(row["status"]),
            verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None,
        )


# =============================================================================
# ConversationRepository
# =============================================================================


class ConversationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, conversation_id: str) -> Conversation | None:
        row = self._conn.execute(
            "SELECT conversation_id, conversation_endpoint_id, platform_conversation_id, "
            "conversation_endpoint_ref, conversation_type, principal_scope, context_partition_policy, status "
            "FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return Conversation(
            conversation_id=row["conversation_id"],
            conversation_endpoint_id=row["conversation_endpoint_id"],
            platform_conversation_id=row["platform_conversation_id"],
            conversation_endpoint_ref=row["conversation_endpoint_ref"],
            conversation_type=ConversationType(row["conversation_type"]),
            principal_scope=row["principal_scope"],
            context_partition_policy=ContextPartitionPolicy(row["context_partition_policy"]),
            status=ConversationStatus(row["status"]),
        )

    def insert(self, conversation: Conversation) -> None:
        self._conn.execute(
            "INSERT INTO conversations (conversation_id, conversation_endpoint_id, "
            "platform_conversation_id, conversation_endpoint_ref, conversation_type, principal_scope, "
            "context_partition_policy, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation.conversation_id, conversation.conversation_endpoint_id,
             conversation.platform_conversation_id,
             conversation.conversation_endpoint_ref,
             conversation.conversation_type.value,
             conversation.principal_scope, conversation.context_partition_policy.value,
             conversation.status.value),
        )

    def find_by_platform(
        self, conversation_endpoint_id: str, platform_conversation_id: str
    ) -> Conversation | None:
        row = self._conn.execute(
            "SELECT conversation_id, conversation_endpoint_id, platform_conversation_id, "
            "conversation_endpoint_ref, conversation_type, principal_scope, context_partition_policy, status "
            "FROM conversations "
            "WHERE conversation_endpoint_id=? AND platform_conversation_id=?",
            (conversation_endpoint_id, platform_conversation_id),
        ).fetchone()
        if row is None:
            return None
        return Conversation(
            conversation_id=row["conversation_id"],
            conversation_endpoint_id=row["conversation_endpoint_id"],
            platform_conversation_id=row["platform_conversation_id"],
            conversation_endpoint_ref=row["conversation_endpoint_ref"],
            conversation_type=ConversationType(row["conversation_type"]),
            principal_scope=row["principal_scope"],
            context_partition_policy=ContextPartitionPolicy(row["context_partition_policy"]),
            status=ConversationStatus(row["status"]),
        )

    def find_by_endpoint_ref(self, conversation_endpoint_ref: str) -> Conversation | None:
        """通过 conversation_endpoint_ref 查找对话。"""
        if not conversation_endpoint_ref:
            return None
        row = self._conn.execute(
            "SELECT conversation_id, conversation_endpoint_id, platform_conversation_id, "
            "conversation_endpoint_ref, conversation_type, principal_scope, context_partition_policy, status "
            "FROM conversations WHERE conversation_endpoint_ref=?",
            (conversation_endpoint_ref,),
        ).fetchone()
        if row is None:
            return None
        return Conversation(
            conversation_id=row["conversation_id"],
            conversation_endpoint_id=row["conversation_endpoint_id"],
            platform_conversation_id=row["platform_conversation_id"],
            conversation_endpoint_ref=row["conversation_endpoint_ref"],
            conversation_type=ConversationType(row["conversation_type"]),
            principal_scope=row["principal_scope"],
            context_partition_policy=ContextPartitionPolicy(row["context_partition_policy"]),
            status=ConversationStatus(row["status"]),
        )


# =============================================================================
# SessionRepository
# =============================================================================


class SessionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, session_id: str) -> Session | None:
        row = self._conn.execute(
            "SELECT session_id, conversation_id, context_partition_key, "
            "reset_generation, status, created_at "
            "FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return Session(
            session_id=row["session_id"],
            conversation_id=row["conversation_id"],
            context_partition_key=row["context_partition_key"],
            reset_generation=row["reset_generation"],
            status=SessionStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def insert(self, session: Session) -> None:
        self._conn.execute(
            "INSERT INTO sessions (session_id, conversation_id, context_partition_key, "
            "reset_generation, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session.session_id, session.conversation_id, session.context_partition_key,
             session.reset_generation, session.status.value, session.created_at.isoformat()),
        )

    def find_active(
        self, conversation_id: str, context_partition_key: str
    ) -> Session | None:
        row = self._conn.execute(
            "SELECT session_id, conversation_id, context_partition_key, "
            "reset_generation, status, created_at "
            "FROM sessions "
            "WHERE conversation_id=? AND context_partition_key=? AND status='active' "
            "ORDER BY created_at DESC LIMIT 1",
            (conversation_id, context_partition_key),
        ).fetchone()
        if row is None:
            return None
        return Session(
            session_id=row["session_id"],
            conversation_id=row["conversation_id"],
            context_partition_key=row["context_partition_key"],
            reset_generation=row["reset_generation"],
            status=SessionStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# =============================================================================
# MessageRepository
# =============================================================================


class MessageRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def next_receive_sequence(self, conversation_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(receive_sequence), 0) + 1 "
            "FROM messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return row[0]

    def insert(self, message: Message) -> None:
        self._conn.execute(
            "INSERT INTO messages (message_id, conversation_id, session_id, "
            "sender_principal_id, sender_endpoint_id, role, direction, "
            "reply_to_message_id, platform_message_id, current_revision_no, "
            "receive_sequence, trust_label, raw_payload_ref, "
            "reply_route_json, capability_snapshot_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.message_id, message.conversation_id, message.session_id,
             message.sender_principal_id, message.sender_endpoint_id,
             message.role.value, message.direction.value,
             message.reply_to_message_id, message.platform_message_id,
             message.current_revision_no, message.receive_sequence,
             message.trust_label, message.raw_payload_ref,
             json.dumps(message.reply_route),
             json.dumps(message.capability_snapshot),
             message.created_at.isoformat()),
        )

    def insert_content_part(self, part: ContentPart, message_id: str) -> None:
        self._conn.execute(
            "INSERT INTO content_parts (part_id, message_id, content_type, inline_data, "
            "payload_ref, size, sha256, metadata, trust_label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (part.part_id, message_id, part.content_type, part.inline_data,
             part.payload_ref, part.size, part.sha256,
             str(part.metadata), part.trust_label),
        )


# =============================================================================
# TurnRepository
# =============================================================================


class TurnRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, turn: Turn) -> None:
        self._conn.execute(
            "INSERT INTO turns (turn_id, session_id, input_message_id, status, "
            "priority, version, cancel_requested_at, active_attempt_id, "
            "final_message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (turn.turn_id, turn.session_id, turn.input_message_id,
             turn.status.value, turn.priority, turn.version,
             epoch_ms(turn.cancel_requested_at),
             turn.active_attempt_id, turn.final_message_id,
             epoch_ms(turn.created_at)),
        )

    def update_status(
        self, turn_id: str, new_status: TurnStatus, expected_version: int
    ) -> bool:
        """版本条件更新状态。返回 True 表示更新成功。"""
        cursor = self._conn.execute(
            "UPDATE turns SET status=?, version=version+1 "
            "WHERE turn_id=? AND version=?",
            (new_status.value, turn_id, expected_version),
        )
        return cursor.rowcount > 0


# =============================================================================
# OutboxRepository
# =============================================================================


class OutboxRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, event: DomainEvent) -> None:
        self._conn.execute(
            "INSERT INTO outbox_events (event_id, event_type, aggregate_type, aggregate_id, "
            "aggregate_version, payload_ref, content_hash, schema_version, "
            "correlation_id, causation_id, origin, trust_label, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (event.event_id, event.event_type, event.aggregate_type,
             event.aggregate_id, event.aggregate_version,
             event.payload_ref, event.content_hash, event.schema_version,
             event.correlation_id, event.causation_id, event.origin,
             event.trust_label, epoch_ms(event.occurred_at)),
        )
