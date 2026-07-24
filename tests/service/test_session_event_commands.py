"""Session commands operate on canonical Event streams for new aggregates."""

from __future__ import annotations

from cogito.config import Config
from cogito.contracts.models import DeleteSessionPayload, DeleteSessionsByConvPayload
from cogito.domain.conversation import Conversation, Session
from cogito.service.api.command_handlers import delete_session, delete_sessions_by_conversation
from cogito.service.api.deps import CommandDeps
from cogito.store.event_replay import replay_session
from cogito.store.event_store import EventStore
from cogito.store.repositories import ConversationRepository, SessionRepository


def test_delete_session_appends_completion_without_session_row(in_memory_db):
    conversation = Conversation(conversation_id="conversation-event-command-1")
    session = Session(
        session_id="session-event-command-1",
        conversation_id=conversation.conversation_id,
        context_partition_key="partition-1",
    )
    ConversationRepository(in_memory_db).insert(conversation)
    SessionRepository(in_memory_db).insert(session)

    response = delete_session(
        DeleteSessionPayload(session_id=session.session_id),
        CommandDeps(conn=in_memory_db, config=Config(), recovery_counts={}),
    )

    assert response.status == "ok"
    assert in_memory_db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    state = replay_session(
        EventStore(in_memory_db).read_stream("session", session.session_id), session.session_id
    )
    assert state is not None
    assert state.status == "closed"
    assert delete_session(
        DeleteSessionPayload(session_id=session.session_id),
        CommandDeps(conn=in_memory_db, config=Config(), recovery_counts={}),
    ).status == "failed"


def test_delete_sessions_by_conversation_appends_each_completion(in_memory_db):
    conversation = Conversation(conversation_id="conversation-event-command-batch")
    sessions = [
        Session(
            session_id=f"session-event-command-batch-{number}",
            conversation_id=conversation.conversation_id,
            context_partition_key=f"partition-{number}",
        )
        for number in (1, 2)
    ]
    ConversationRepository(in_memory_db).insert(conversation)
    repository = SessionRepository(in_memory_db)
    for session in sessions:
        repository.insert(session)

    response = delete_sessions_by_conversation(
        DeleteSessionsByConvPayload(conversation_id=conversation.conversation_id),
        CommandDeps(conn=in_memory_db, config=Config(), recovery_counts={}),
    )

    assert response.status == "ok"
    assert response.details["deleted_count"] == 2
    for session in sessions:
        state = replay_session(
            EventStore(in_memory_db).read_stream("session", session.session_id), session.session_id
        )
        assert state is not None and state.status == "closed"
