"""Repository implementations — data access for each aggregate.

每个 Repository 负责一个聚合的持久化，返回领域对象。
所有 Repository 共享同一个 sqlite3.Connection，由 UnitOfWork 管理事务。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from cogito.contracts.clock import Clock, ProductionClock, epoch_ms, from_epoch_ms
from cogito.domain.conversation import (
    ContextPartitionPolicy,
    Conversation,
    ConversationStatus,
    ConversationType,
    Session,
    SessionStatus,
)
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.message import ContentPart, Message
from cogito.domain.principal import (
    Endpoint,
    EndpointStatus,
    Principal,
    PrincipalStatus,
    PrincipalType,
)
from cogito.domain.turn import RunAttempt, RunAttemptStatus, Turn, TurnStatus
from cogito.store.event_store import EventStore
from cogito.store.event_replay import (
    replay_conversation,
    replay_endpoint,
    replay_principal,
    replay_run_attempt,
    replay_session,
    replay_turn,
)


def _append_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    stream_type: str,
    stream_id: str,
    event_class: EventClass,
    context: EventContext,
    summary: str,
    attributes: dict[str, Any] | None = None,
    payload_ref: str | None = None,
    outcome: str = "",
    error_category: str = "",
    occurred_at: int | None = None,
    idempotency_key: str = "",
) -> Event:
    """Append a safe canonical fact in the caller's existing transaction."""
    return EventStore(conn).append(
        Event(
            event_type=event_type,
            stream_type=stream_type,
            stream_id=stream_id,
            producer="repository-projection",
            event_class=event_class,
            context=context,
            summary=summary,
            attributes=attributes or {},
            payload_ref=payload_ref,
            outcome=outcome,
            error_category=error_category,
            occurred_at=occurred_at or epoch_ms(ProductionClock().now()),
            idempotency_key=idempotency_key,
        )
    )


_SAFE_CONTENT_PART_METADATA_KEYS = frozenset(
    {"mime", "name", "width", "height", "duration_ms", "asset_id", "asset_error"}
)


def _safe_content_part_descriptor(part: ContentPart) -> dict[str, Any]:
    """Return Event-safe content metadata without retaining user supplied text."""
    metadata = {
        key: value
        for key, value in part.metadata.items()
        if key in _SAFE_CONTENT_PART_METADATA_KEYS
        and isinstance(value, str | int | float | bool)
    }
    return {
        "content_type": part.content_type,
        "payload_ref": part.payload_ref,
        "size": part.size,
        "sha256": part.sha256,
        "metadata": metadata,
        "trust_label": part.trust_label,
        "ordinal": part.ordinal,
    }


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
            (
                record.channel_instance_id,
                record.platform_event_id,
                record.status,
                record.message_id,
                record.received_at,
            ),
        )


# =============================================================================
# PrincipalRepository
# =============================================================================


class PrincipalRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, principal_id: str) -> Principal | None:
        state = replay_principal(
            EventStore(self._conn).read_stream("principal", principal_id), principal_id
        )
        return self._projection_to_principal(state) if state is not None else None

    def insert(self, principal: Principal) -> Principal:
        EventStore(self._conn).append(
            Event(
                event_type="interaction.principal.created",
                stream_type="principal",
                stream_id=principal.principal_id,
                producer="repository-projection",
                event_class=EventClass.DOMAIN,
                context=EventContext(principal_id=principal.principal_id),
                summary="Principal created",
                attributes={"principal_type": principal.principal_type.value},
                outcome=principal.status.value,
                occurred_at=epoch_ms(principal.created_at),
                idempotency_key=f"principal:{principal.principal_id}:created",
            ),
            expected_version=0,
        )
        return principal

    def find_by_platform(self, channel_type: str, platform_account_id: str) -> Principal | None:
        endpoint_events = EventStore(self._conn).read_stream_type("endpoint")
        for endpoint_id in {event.stream_id for event in endpoint_events}:
            endpoint = replay_endpoint(endpoint_events, endpoint_id)
            if (
                endpoint is not None
                and endpoint.channel_type == channel_type
                and endpoint.platform_account_id == platform_account_id
                and endpoint.status == EndpointStatus.active.value
            ):
                return self.find(endpoint.principal_id)
        return None

    @staticmethod
    def _projection_to_principal(state: Any) -> Principal:
        return Principal(
            principal_id=state.principal_id,
            principal_type=PrincipalType(state.principal_type),
            status=PrincipalStatus(state.status),
            created_at=from_epoch_ms(state.created_at),
        )


# =============================================================================
# EndpointRepository
# =============================================================================


class EndpointRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, endpoint_id: str) -> Endpoint | None:
        state = replay_endpoint(
            EventStore(self._conn).read_stream("endpoint", endpoint_id), endpoint_id
        )
        return self._projection_to_endpoint(state) if state is not None else None

    def insert(self, endpoint: Endpoint) -> Endpoint:
        event = EventStore(self._conn).append(
            Event(
                event_type="interaction.endpoint.created",
                stream_type="endpoint",
                stream_id=endpoint.endpoint_id,
                producer="repository-projection",
                event_class=EventClass.DOMAIN,
                context=EventContext(principal_id=endpoint.principal_id),
                summary="Endpoint created",
                attributes={
                    "channel_type": endpoint.channel_type,
                    "channel_instance_id": endpoint.channel_instance_id,
                    "platform_account_id": endpoint.platform_account_id,
                    "endpoint_ref": endpoint.endpoint_ref,
                    "capabilities": list(endpoint.capabilities),
                    "verified_at": epoch_ms(endpoint.verified_at),
                },
                outcome=endpoint.status.value,
                idempotency_key=(
                    f"endpoint:{endpoint.channel_instance_id}:"
                    f"{endpoint.platform_account_id}:created"
                ),
            ),
            expected_version=0,
        )
        if event.stream_id == endpoint.endpoint_id:
            return endpoint
        state = replay_endpoint(
            EventStore(self._conn).read_stream("endpoint", event.stream_id), event.stream_id
        )
        return self._projection_to_endpoint(state) if state is not None else endpoint

    def find_by_platform(
        self, channel_instance_id: str, platform_account_id: str
    ) -> Endpoint | None:
        return next(
            (
                endpoint
                for endpoint in self._event_endpoints()
                if endpoint.channel_instance_id == channel_instance_id
                and endpoint.platform_account_id == platform_account_id
            ),
            None,
        )

    def find_by_ref(self, endpoint_ref: str) -> Endpoint | None:
        if not endpoint_ref:
            return None
        return next(
            (
                endpoint
                for endpoint in self._event_endpoints()
                if endpoint.endpoint_ref == endpoint_ref
            ),
            None,
        )

    def _event_endpoints(self) -> list[Endpoint]:
        events = EventStore(self._conn).read_stream_type("endpoint")
        result: list[Endpoint] = []
        for endpoint_id in {event.stream_id for event in events}:
            state = replay_endpoint(events, endpoint_id)
            if state is not None:
                result.append(self._projection_to_endpoint(state))
        return result

    @staticmethod
    def _projection_to_endpoint(state: Any) -> Endpoint:
        return Endpoint(
            endpoint_id=state.endpoint_id,
            channel_type=state.channel_type,
            channel_instance_id=state.channel_instance_id,
            platform_account_id=state.platform_account_id,
            principal_id=state.principal_id,
            endpoint_ref=state.endpoint_ref,
            capabilities=list(state.capabilities),
            status=EndpointStatus(state.status),
            verified_at=from_epoch_ms(state.verified_at),
        )


# =============================================================================
# ConversationRepository
# =============================================================================


class ConversationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, conversation_id: str) -> Conversation | None:
        state = replay_conversation(
            EventStore(self._conn).read_stream("conversation", conversation_id),
            conversation_id,
        )
        return self._projection_to_conversation(state) if state is not None else None

    def insert(self, conversation: Conversation) -> None:
        EventStore(self._conn).append(
            Event(
                event_type="interaction.conversation.created",
                stream_type="conversation",
                stream_id=conversation.conversation_id,
                producer="repository-projection",
                event_class=EventClass.DOMAIN,
                context=EventContext(conversation_id=conversation.conversation_id),
                summary="Conversation created",
                attributes={
                    "conversation_endpoint_id": conversation.conversation_endpoint_id,
                    "platform_conversation_id": conversation.platform_conversation_id,
                    "conversation_endpoint_ref": conversation.conversation_endpoint_ref,
                    "conversation_type": conversation.conversation_type.value,
                    "principal_scope": conversation.principal_scope,
                    "context_partition_policy": conversation.context_partition_policy.value,
                },
                outcome=conversation.status.value,
                idempotency_key=f"conversation:{conversation.conversation_id}:created",
            ),
            expected_version=0,
        )

    def find_by_platform(
        self, conversation_endpoint_id: str, platform_conversation_id: str
    ) -> Conversation | None:
        for conversation in self._event_conversations():
            if (
                conversation.conversation_endpoint_id == conversation_endpoint_id
                and conversation.platform_conversation_id == platform_conversation_id
            ):
                return conversation
        return None

    def find_by_endpoint_ref(
        self, conversation_endpoint_ref: str
    ) -> Conversation | None:
        if not conversation_endpoint_ref:
            return None
        for conversation in self._event_conversations():
            if conversation.conversation_endpoint_ref == conversation_endpoint_ref:
                return conversation
        return None

    def _event_conversations(self) -> list[Conversation]:
        events = EventStore(self._conn).read_stream_type("conversation")
        result: list[Conversation] = []
        for cid in {event.stream_id for event in events}:
            state = replay_conversation(events, cid)
            if state is not None:
                result.append(self._projection_to_conversation(state))
        return result

    @staticmethod
    def _projection_to_conversation(state: Any) -> Conversation:
        return Conversation(
            conversation_id=state.conversation_id,
            conversation_endpoint_id=state.conversation_endpoint_id,
            platform_conversation_id=state.platform_conversation_id,
            conversation_endpoint_ref=state.conversation_endpoint_ref,
            conversation_type=ConversationType(state.conversation_type),
            principal_scope=state.principal_scope,
            context_partition_policy=ContextPartitionPolicy(state.context_partition_policy),
            status=ConversationStatus(state.status),
        )


# =============================================================================
# SessionRepository
# =============================================================================


class SessionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def find(self, session_id: str) -> Session | None:
        state = replay_session(
            EventStore(self._conn).read_stream("session", session_id),
            session_id,
        )
        return self._projection_to_session(state) if state is not None else None

    def insert(self, session: Session) -> None:
        EventStore(self._conn).append(
            Event(
                event_type="interaction.session.created",
                stream_type="session",
                stream_id=session.session_id,
                producer="repository-projection",
                event_class=EventClass.DOMAIN,
                context=EventContext(
                    conversation_id=session.conversation_id,
                    session_id=session.session_id,
                ),
                summary="Session created",
                attributes={
                    "context_partition_key": session.context_partition_key,
                    "reset_generation": session.reset_generation,
                },
                outcome=session.status.value,
                occurred_at=epoch_ms(session.created_at),
                idempotency_key=f"session:{session.session_id}:created",
            ),
            expected_version=0,
        )

    def find_active(self, conversation_id: str, context_partition_key: str) -> Session | None:
        active = [
            session
            for session in self._event_sessions()
            if session.conversation_id == conversation_id
            and session.context_partition_key == context_partition_key
            and session.status == SessionStatus.active
        ]
        return max(active, key=lambda session: session.created_at) if active else None

    def list_by_conversation(
        self, conversation_id: str, *, active_only: bool = False
    ) -> list[Session]:
        sessions = [
            session
            for session in self._event_sessions()
            if session.conversation_id == conversation_id
            and (not active_only or session.status == SessionStatus.active)
        ]
        return sorted(sessions, key=lambda session: (session.created_at, session.session_id))

    def _event_sessions(self) -> list[Session]:
        events = EventStore(self._conn).read_stream_type("session")
        result: list[Session] = []
        for session_id in {event.stream_id for event in events}:
            state = replay_session(events, session_id)
            if state is not None:
                result.append(self._projection_to_session(state))
        return result

    @staticmethod
    def _projection_to_session(state: Any) -> Session:
        return Session(
            session_id=state.session_id,
            conversation_id=state.conversation_id,
            context_partition_key=state.context_partition_key,
            reset_generation=state.reset_generation,
            status=SessionStatus(state.status),
            created_at=from_epoch_ms(state.created_at),
        )


# =============================================================================
# MessageRepository
# =============================================================================


class MessageRepository:
    def __init__(self, conn: sqlite3.Connection, *, payload_store: Any | None = None) -> None:
        self._conn = conn
        self._payload_store = payload_store

    def next_receive_sequence(self, conversation_id: str) -> int:
        sequences = []
        events = EventStore(self._conn).read_stream_type("message")
        for message_id in {event.stream_id for event in events}:
            from cogito.store.event_replay import replay_message

            state = replay_message(events, message_id)
            if state is not None and state.conversation_id == conversation_id:
                sequences.append(state.receive_sequence)
        return max(sequences, default=0) + 1

    def insert(self, message: Message) -> None:
        if self._payload_store is not None:
            self._store_event_message(message)
            return
        # Auto-create a PayloadStore for tests/callers without one.
        from cogito.infrastructure.payload_store import PayloadStore
        import tempfile

        self._payload_store = PayloadStore(tempfile.mkdtemp(prefix="cogito-msg-"), self._conn)
        self._store_event_message(message)

    def insert_content_part(self, part: ContentPart, message_id: str) -> None:
        # The immutable message envelope already contains every part; no
        # second mutable ContentPart projection is kept for new messages.
        return

    def _store_event_message(self, message: Message) -> None:
        """Persist raw message data only in PayloadStore and append safe metadata."""
        envelope = json.dumps(
            {"schema": "message-envelope.v1", "message": message.to_dict()},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        payload = self._payload_store.put(envelope, content_type="application/vnd.cogito.message+json")
        message.raw_payload_ref = payload.payload_id
        event = _append_event(
            self._conn,
            event_type="interaction.message.recorded",
            stream_type="message",
            stream_id=message.message_id,
            event_class=EventClass.DOMAIN,
            context=EventContext(
                conversation_id=message.conversation_id,
                session_id=message.session_id,
                principal_id=message.sender_principal_id,
            ),
            summary=f"{message.direction.value} {message.role.value} message recorded",
            attributes={
                "direction": message.direction.value,
                "role": message.role.value,
                "receive_sequence": message.receive_sequence,
                "trust_label": message.trust_label,
                "sender_endpoint_id": message.sender_endpoint_id,
                "reply_to_message_id": message.reply_to_message_id or "",
                "platform_message_id": message.platform_message_id or "",
                "part_descriptors": [_safe_content_part_descriptor(part) for part in message.content_parts],
            },
            payload_ref=payload.payload_id,
            occurred_at=epoch_ms(message.created_at),
            idempotency_key=f"message:{message.message_id}:recorded",
        )
        # Idempotent retries retain the original immutable payload reference.
        if event.payload_ref != payload.payload_id:
            message.raw_payload_ref = event.payload_ref


# =============================================================================
# TurnRepository
# =============================================================================


class TurnRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(
        self,
        turn: Turn,
        *,
        event_context: EventContext | None = None,
        event_producer: str = "repository-projection",
    ) -> Event:
        event_type, event_class = self._event_type_for_status(turn.status)
        return EventStore(self._conn).append(
            Event(
                event_type=event_type,
                stream_type="turn",
                stream_id=turn.turn_id,
                producer=event_producer,
                event_class=event_class,
                context=event_context
                or EventContext(session_id=turn.session_id, turn_id=turn.turn_id),
                summary=f"Turn {turn.status.value}",
                attributes={"priority": turn.priority, "input_message_id": turn.input_message_id},
                outcome=turn.status.value,
                occurred_at=epoch_ms(turn.created_at),
                idempotency_key=f"turn:{turn.turn_id}:{turn.status.value}:1",
            ),
            expected_version=0,
        )

    def update_status(
        self,
        turn_id: str,
        new_status: TurnStatus,
        expected_version: int,
        *,
        event_context: EventContext | None = None,
        event_producer: str = "repository-projection",
    ) -> bool:
        """版本条件更新状态。返回 True 表示更新成功。"""
        stream = EventStore(self._conn).read_stream("turn", turn_id)
        state = replay_turn(stream, turn_id)
        if state is None:
            return False
        if state.stream_version != expected_version:
            return False
        event_type, event_class = self._event_type_for_status(new_status)
        source = stream[-1]
        context = event_context or EventContext(
            trace_id=source.context.trace_id,
            correlation_id=source.context.correlation_id,
            causation_id=source.event_id,
            actor_id=source.context.actor_id,
            principal_id=source.context.principal_id,
            conversation_id=source.context.conversation_id,
            session_id=source.context.session_id,
            turn_id=turn_id,
            attempt_id=source.context.attempt_id,
        )
        if not context.causation_id:
            context = EventContext(
                trace_id=context.trace_id,
                span_id=context.span_id,
                parent_span_id=context.parent_span_id,
                correlation_id=context.correlation_id,
                causation_id=source.event_id,
                actor_id=context.actor_id,
                principal_id=context.principal_id,
                conversation_id=context.conversation_id,
                session_id=context.session_id,
                turn_id=context.turn_id or turn_id,
                attempt_id=context.attempt_id,
                task_id=context.task_id,
            )
        try:
            EventStore(self._conn).append(
                Event(
                    event_type=event_type,
                    stream_type="turn",
                    stream_id=turn_id,
                    producer=event_producer,
                    event_class=event_class,
                    context=context,
                    summary=f"Turn {new_status.value}",
                    outcome=new_status.value,
                    idempotency_key=f"turn:{turn_id}:{new_status.value}:{expected_version + 1}",
                ),
                expected_version=expected_version,
            )
        except Exception:
            return False
        return True

    @staticmethod
    def _event_type_for_status(status: TurnStatus) -> tuple[str, EventClass]:
        mapping = {
            TurnStatus.accepted: ("runtime.turn.accepted", EventClass.DOMAIN),
            TurnStatus.queued: ("runtime.turn.queued", EventClass.DOMAIN),
            TurnStatus.running: ("runtime.turn.started", EventClass.OPERATION),
            TurnStatus.waiting_user: ("runtime.turn.waiting_user", EventClass.OPERATION),
            TurnStatus.waiting_external: ("runtime.turn.waiting_external", EventClass.OPERATION),
            TurnStatus.completed: ("runtime.turn.completed", EventClass.DOMAIN),
            TurnStatus.cancelled: ("runtime.turn.cancelled", EventClass.DOMAIN),
            TurnStatus.failed: ("runtime.turn.failed", EventClass.DOMAIN),
            TurnStatus.expired: ("runtime.turn.failed", EventClass.DOMAIN),
        }
        return mapping[status]

    def list_by_session(self, session_id: str) -> list[Turn]:
        """列出某个 Session 的全部 Turn（按 created_at 升序）。"""
        return sorted(
            (turn for turn in self._event_turns() if turn.session_id == session_id),
            key=lambda turn: epoch_ms(turn.created_at) or 0,
        )

    def list_(
        self,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Turn]:
        """按状态过滤列出 Turn（None 表示全部）。"""
        filtered = [
            turn for turn in self._event_turns() if status is None or turn.status.value == status
        ]
        return sorted(
            filtered,
            key=lambda turn: epoch_ms(turn.created_at) or 0,
            reverse=True,
        )[offset : offset + limit]

    def get(self, turn_id: str) -> Turn | None:
        stream = EventStore(self._conn).read_stream("turn", turn_id)
        state = replay_turn(stream, turn_id)
        return self._projection_to_turn(state) if state is not None else None

    def list_attempts(self, turn_id: str) -> list[RunAttempt]:
        """列出某个 Turn 的全部 RunAttempt。"""
        grouped: dict[str, list[Event]] = {}
        for event in EventStore(self._conn).read_stream_type("run_attempt"):
            if event.context.turn_id == turn_id:
                grouped.setdefault(event.stream_id, []).append(event)
        return sorted(
            (
                self._projection_to_attempt(state)
                for attempt_id, stream in grouped.items()
                if (state := replay_run_attempt(stream, attempt_id)) is not None
            ),
            key=lambda attempt: attempt.attempt_no,
        )

    def count(self, status: str | None = None) -> int:
        """统计 Turn 数量（按状态或全部）。"""
        event_turns = self._event_turns()
        return sum(1 for turn in event_turns if status is None or turn.status.value == status)

    def _event_turns(self) -> list[Turn]:
        grouped: dict[str, list[Event]] = {}
        for event in EventStore(self._conn).read_stream_type("turn"):
            grouped.setdefault(event.stream_id, []).append(event)
        return [
            self._projection_to_turn(state)
            for turn_id, stream in grouped.items()
            if (state := replay_turn(stream, turn_id)) is not None
        ]

    @staticmethod
    def _projection_to_turn(state: Any) -> Turn:
        return Turn(
            turn_id=state.turn_id,
            session_id=state.session_id,
            input_message_id=state.input_message_id,
            status=TurnStatus(state.status),
            priority=state.priority or 80,
            version=state.stream_version,
            cancel_requested_at=from_epoch_ms(state.cancel_requested_at),
            active_attempt_id=state.active_attempt_id or None,
            final_message_id=state.final_message_id or None,
            created_at=from_epoch_ms(state.created_at),
            completed_at=from_epoch_ms(state.completed_at),
        )

    @staticmethod
    def _projection_to_attempt(state: Any) -> RunAttempt:
        return RunAttempt(
            attempt_id=state.attempt_id,
            turn_id=state.turn_id,
            attempt_no=state.attempt_no,
            status=RunAttemptStatus(state.status),
            checkpoint_ref=state.checkpoint_ref,
            started_at=from_epoch_ms(state.started_at),
            finished_at=from_epoch_ms(state.finished_at),
            worker_id=state.worker_id,
            lease_version=state.lease_version,
            lease_expires_at=from_epoch_ms(state.lease_expires_at),
            error_ref=state.error_ref or "",
        )

