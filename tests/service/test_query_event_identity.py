"""Public identity/session query views are reconstructed from Event streams."""

from __future__ import annotations

from cogito.config import Config
from cogito.domain.conversation import Conversation, Session
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.message import ContentPart, Message, MessageDirection, MessageRole
from cogito.domain.principal import Endpoint, Principal, PrincipalType
from cogito.domain.turn import Turn, TurnStatus
from cogito.infrastructure.payload_store import PayloadStore
from cogito.service.api.query_service import QueryService
from cogito.store.event_store import EventStore
from cogito.store.repositories import (
    ConversationRepository,
    EndpointRepository,
    MessageRepository,
    PrincipalRepository,
    SessionRepository,
    TurnRepository,
)


def test_identity_and_session_public_views_replay_events(in_memory_db, tmp_path):
    config = Config()
    config.workspace_path = str(tmp_path)
    payload_store = PayloadStore(config.resolve_payload_dir(), in_memory_db)
    principal = Principal(principal_id="principal-query-1", principal_type=PrincipalType.external_user)
    endpoint = Endpoint(
        endpoint_id="endpoint-query-1",
        channel_type="web",
        channel_instance_id="web-1",
        platform_account_id="account-1",
        principal_id=principal.principal_id,
    )
    conversation = Conversation(
        conversation_id="conversation-query-1",
        conversation_endpoint_id=endpoint.endpoint_id,
        platform_conversation_id="platform-conversation-1",
        principal_scope=principal.principal_id,
    )
    session = Session(
        session_id="session-query-1",
        conversation_id=conversation.conversation_id,
        context_partition_key="partition-query-1",
    )
    PrincipalRepository(in_memory_db).insert(principal)
    EndpointRepository(in_memory_db).insert(endpoint)
    ConversationRepository(in_memory_db).insert(conversation)
    SessionRepository(in_memory_db).insert(session)
    message = Message(
        message_id="message-query-1",
        conversation_id=conversation.conversation_id,
        session_id=session.session_id,
        sender_principal_id=principal.principal_id,
        role=MessageRole.user,
        direction=MessageDirection.inbound,
        receive_sequence=1,
        content_parts=[ContentPart(content_type="text", inline_data="event query input")],
    )
    MessageRepository(in_memory_db, payload_store=payload_store).insert(message)
    TurnRepository(in_memory_db).insert(
        Turn(
            turn_id="turn-query-1",
            session_id=session.session_id,
            input_message_id=message.message_id,
            status=TurnStatus.accepted,
        )
    )

    service = QueryService(in_memory_db, config)
    counts = service.status()["counts"]
    assert counts["conversations"] == 1
    assert counts["sessions"] == 1
    assert counts["endpoints"] == 1
    assert service.list_channels()["items"] == [{"channel_type": "web", "count": 1}]
    assert service.list_conversations()["items"][0]["conversation_id"] == conversation.conversation_id
    assert service.list_sessions()["items"][0]["name"] == "event query input"
    history = service.get_conversation_messages("platform-conversation-1")
    assert history["items"][0]["text"] == "event query input"
    assert service.get_session_trace(session.session_id)["messages"][0]["text"] == "event query input"
    assert service.trace_conversation(conversation.conversation_id)["turns"][0]["turn_id"] == "turn-query-1"

    EventStore(in_memory_db).append(
        Event(
            event_type="runtime.session.completed",
            stream_type="session",
            stream_id=session.session_id,
            producer="test",
            event_class=EventClass.DOMAIN,
            context=EventContext(
                conversation_id=conversation.conversation_id,
                session_id=session.session_id,
            ),
            outcome="completed",
        ),
        expected_version=1,
    )
    assert service.list_sessions()["items"] == []
    assert service.list_conversations()["items"] == []
