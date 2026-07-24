"""Execution aggregate projections can be reconstructed from event streams."""

from __future__ import annotations

from cogito.domain.event import Event, EventClass, EventContext
from cogito.contracts.context import ContextBuilder
from cogito.config import Config
from cogito.contracts.envelope import ChannelEnvelope
from cogito.infrastructure.payload_store import PayloadStore
from cogito.service.api.query_service import QueryService
from cogito.service.canonical_delivery_effect_executor import CanonicalDeliveryEffectExecutor
from cogito.service.delivery_effect_payload import (
    DeliveryEffectPayload,
    load_delivery_effect_payload,
    store_delivery_effect_payload,
)
from cogito.service.delivery_service import DeliveryRequest
from cogito.domain.conversation import Conversation, Session
from cogito.domain.message import ContentPart, Message, MessageDirection, MessageRole
from cogito.domain.principal import Endpoint, Principal, PrincipalType
from cogito.domain.turn import Turn, TurnStatus
from cogito.store.event_replay import (
    replay_delivery,
    replay_message,
    replay_run_attempt,
    replay_task,
    replay_turn,
)
from cogito.store.event_replay import replay_approval
from cogito.store.event_replay import replay_connector_source, replay_memory
from cogito.store.event_replay import replay_knowledge_resource
from cogito.store.event_replay import replay_proactive_candidate
from cogito.store.event_store import EventStore
from cogito.store.event_message_reader import EventMessageReader
from cogito.store.repositories import (
    ConversationRepository,
    EndpointRepository,
    MessageRepository,
    PrincipalRepository,
    SessionRepository,
    TurnRepository,
)
from cogito.store.proactive_repo import (
    ProactiveCandidate,
    ProactiveCandidateRepository,
    ProactiveDecision,
    ProactiveDecisionRepository,
)
from cogito.service.approval_service import SqliteApprovalService
from cogito.service.completion import TurnCompletionService
from cogito.service.dispatcher import Dispatcher
from cogito.service.inbound_service import InboundService
from cogito.service.knowledge.service import KnowledgeService
from cogito.service.memory_service import SqliteMemoryService
from cogito.service.sqlite_delivery_service import SqliteDeliveryService
from cogito.service.streaming_delivery_event_store import StreamingDeliveryEventStore
from cogito.service.event_effect_recovery import EventEffectRecoveryPlanner
from cogito.service.event_effect_worker import CanonicalEffectWorker, EffectOutcome
from cogito.service.gateway_client import GatewayResult


def _append(store: EventStore, **kwargs: object) -> Event:
    return store.append(Event(producer="test", **kwargs))  # type: ignore[arg-type]


def test_task_replay_recovers_terminal_state_without_tasks_table(in_memory_db):
    store = EventStore(in_memory_db)
    _append(
        store,
        event_type="task.created",
        stream_type="task",
        stream_id="task-1",
        event_class=EventClass.DOMAIN,
        context=EventContext(task_id="task-1"),
        attributes={"task_type": "digest", "priority": 30, "origin": "connector"},
        outcome="queued",
    )
    _append(
        store,
        event_type="task.leased",
        stream_type="task",
        stream_id="task-1",
        event_class=EventClass.OPERATION,
        context=EventContext(task_id="task-1"),
        attributes={"worker_id": "worker-a", "lease_expires_at": 2_000},
        outcome="running",
    )
    _append(
        store,
        event_type="task.completed",
        stream_type="task",
        stream_id="task-1",
        event_class=EventClass.DOMAIN,
        context=EventContext(task_id="task-1"),
        payload_ref="payload://result/1",
        outcome="completed",
    )

    state = replay_task(store.read_stream("task", "task-1"), "task-1")
    assert state is not None
    assert state.status == "completed"
    assert state.task_type == "digest"
    assert state.result_ref == "payload://result/1"
    assert state.lease_owner == ""
    assert state.stream_version == 3


def test_turn_replay_recovers_wait_then_completion(in_memory_db):
    store = EventStore(in_memory_db)
    context = EventContext(session_id="session-1", turn_id="turn-1")
    _append(
        store,
        event_type="runtime.turn.accepted",
        stream_type="turn",
        stream_id="turn-1",
        event_class=EventClass.DOMAIN,
        context=context,
        attributes={"priority": 80, "input_message_id": "message-1"},
        outcome="accepted",
    )
    _append(
        store,
        event_type="runtime.turn.started",
        stream_type="turn",
        stream_id="turn-1",
        event_class=EventClass.OPERATION,
        context=context,
    )
    _append(
        store,
        event_type="runtime.turn.waiting_external",
        stream_type="turn",
        stream_id="turn-1",
        event_class=EventClass.OPERATION,
        context=context,
    )
    _append(
        store,
        event_type="runtime.turn.completed",
        stream_type="turn",
        stream_id="turn-1",
        event_class=EventClass.DOMAIN,
        context=context,
        outcome="completed",
    )

    state = replay_turn(store.read_stream("turn", "turn-1"), "turn-1")
    assert state is not None
    assert state.status == "completed"
    assert state.session_id == "session-1"
    assert state.input_message_id == "message-1"
    assert state.stream_version == 4


def test_run_attempt_and_message_replay_keep_only_safe_payload_references(in_memory_db):
    store = EventStore(in_memory_db)
    context = EventContext(
        trace_id="trace-1",
        conversation_id="conversation-1",
        session_id="session-1",
        principal_id="principal-1",
        turn_id="turn-1",
        attempt_id="attempt-1",
    )
    _append(
        store,
        event_type="runtime.attempt.started",
        stream_type="run_attempt",
        stream_id="attempt-1",
        event_class=EventClass.OPERATION,
        context=context,
        attributes={"attempt_no": 2, "worker_id": "worker-1", "lease_version": 1,
                    "lease_expires_at": 9_000},
        payload_ref="payload://checkpoint/1",
        outcome="running",
    )
    _append(
        store,
        event_type="runtime.attempt.completed",
        stream_type="run_attempt",
        stream_id="attempt-1",
        event_class=EventClass.OPERATION,
        context=context,
        outcome="succeeded",
    )
    _append(
        store,
        event_type="interaction.message.recorded",
        stream_type="message",
        stream_id="message-1",
        event_class=EventClass.DOMAIN,
        context=context,
        attributes={
            "direction": "inbound",
            "role": "user",
            "receive_sequence": 7,
            "trust_label": "verified",
            "sender_endpoint_id": "endpoint-1",
            "part_descriptors": [{"content_type": "text", "payload_ref": "payload://body/1", "ordinal": 0}],
        },
        payload_ref="payload://raw/1",
        outcome="recorded",
    )

    attempt = replay_run_attempt(store.read_stream("run_attempt", "attempt-1"), "attempt-1")
    message = replay_message(store.read_stream("message", "message-1"), "message-1")
    assert attempt is not None
    assert attempt.status == "succeeded"
    assert attempt.attempt_no == 2
    assert attempt.checkpoint_ref == "payload://checkpoint/1"
    assert message is not None
    assert message.receive_sequence == 7
    assert message.raw_payload_ref == "payload://raw/1"
    assert message.part_descriptors[0]["payload_ref"] == "payload://body/1"
    assert "inline_data" not in message.part_descriptors[0]


def test_message_recorded_event_redacts_content_and_unapproved_part_metadata(in_memory_db):
    in_memory_db.execute("INSERT INTO conversations (conversation_id) VALUES (?)", ("conversation-1",))
    message = Message(
        message_id="message-safe-1",
        conversation_id="conversation-1",
        session_id="session-1",
        sender_principal_id="principal-1",
        sender_endpoint_id="endpoint-1",
        role=MessageRole.user,
        direction=MessageDirection.inbound,
        content_parts=[
            ContentPart(
                content_type="text",
                inline_data="this must never enter event_log",
                payload_ref="payload://body/1",
                metadata={"name": "note.txt", "untrusted_text": "do not store"},
            )
        ],
    )
    MessageRepository(in_memory_db).insert(message)

    event = EventStore(in_memory_db).read_stream("message", message.message_id)[-1]
    descriptor = event.attributes["part_descriptors"][0]
    assert "inline_data" not in descriptor
    assert descriptor["metadata"] == {"name": "note.txt"}
    assert "this must never enter event_log" not in str(event.attributes)


def test_event_backed_message_replays_restricted_payload_without_message_rows(in_memory_db, tmp_path):
    payload_store = PayloadStore(tmp_path / "payloads", in_memory_db)
    message = Message(
        message_id="message-event-only-1",
        conversation_id="conversation-1",
        session_id="session-1",
        sender_principal_id="principal-1",
        sender_endpoint_id="endpoint-1",
        role=MessageRole.user,
        direction=MessageDirection.inbound,
        receive_sequence=1,
        content_parts=[ContentPart(content_type="text", inline_data="restricted input text")],
    )

    MessageRepository(in_memory_db, payload_store=payload_store).insert(message)

    assert in_memory_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    event = EventStore(in_memory_db).read_stream("message", message.message_id)[0]
    assert event.payload_ref
    assert "restricted input text" not in str(event.attributes)

    reader = EventMessageReader(in_memory_db, payload_store)
    assert reader.get(message.message_id)["content_parts"][0]["inline_data"] == "restricted input text"
    snapshot = ContextBuilder(in_memory_db, message_reader=reader).build(
        turn_id="turn-1",
        session_id="session-1",
        input_message_id=message.message_id,
    )
    assert any(item.content == "restricted input text" for item in snapshot.items)


def test_event_backed_inbound_and_reply_never_write_message_projection(in_memory_db, tmp_path):
    payload_store = PayloadStore(tmp_path / "payloads", in_memory_db)
    accepted = InboundService(in_memory_db, payload_store=payload_store).accept(
        ChannelEnvelope(
            channel_type="test",
            channel_instance_id="channel-1",
            platform_sender_id="user-1",
            platform_conversation_id="conversation-1",
            platform_message_id="platform-message-1",
            content_parts=[{"content_type": "text", "inline_data": "event-only input"}],
        )
    )
    claimed = Dispatcher(in_memory_db).claim_next("worker-1")
    assert claimed is not None

    final_message_id = TurnCompletionService(
        in_memory_db,
        effect_payload_store=payload_store,
    ).complete_reply(claimed.turn, claimed.attempt, "event-only response")

    assert final_message_id
    assert in_memory_db.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 0
    assert in_memory_db.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 0
    assert in_memory_db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert in_memory_db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert in_memory_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert in_memory_db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    assert in_memory_db.execute("SELECT COUNT(*) FROM run_attempts").fetchone()[0] == 0
    reader = EventMessageReader(in_memory_db, payload_store)
    assert reader.get(accepted.message_id)["content_parts"][0]["inline_data"] == "event-only input"
    assert reader.get(final_message_id)["content_parts"][0]["inline_data"] == "event-only response"
    assert replay_turn(EventStore(in_memory_db).read_stream("turn", accepted.turn_id), accepted.turn_id).status == "completed"


def test_event_backed_conversation_and_session_replay_without_state_rows(in_memory_db):
    conversation = Conversation(
        conversation_id="conversation-event-only-1",
        conversation_endpoint_id="endpoint-1",
        platform_conversation_id="platform-conversation-1",
        conversation_endpoint_ref="conversation-ref-1",
    )
    session = Session(
        session_id="session-event-only-1",
        conversation_id=conversation.conversation_id,
        context_partition_key="partition-1",
        reset_generation=2,
    )
    conversations = ConversationRepository(in_memory_db)
    sessions = SessionRepository(in_memory_db)

    conversations.insert(conversation)
    sessions.insert(session)

    assert in_memory_db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert in_memory_db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert conversations.find_by_platform("endpoint-1", "platform-conversation-1") == conversation
    assert conversations.find_by_endpoint_ref("conversation-ref-1") == conversation
    assert sessions.find_active(conversation.conversation_id, "partition-1") == session

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
    assert sessions.find_active(conversation.conversation_id, "partition-1") is None


def test_event_backed_principal_and_endpoint_replay_without_state_rows(in_memory_db):
    principal = Principal(
        principal_id="principal-event-only-1",
        principal_type=PrincipalType.external_user,
    )
    endpoint = Endpoint(
        endpoint_id="endpoint-event-only-1",
        channel_type="test",
        channel_instance_id="channel-1",
        platform_account_id="account-1",
        principal_id=principal.principal_id,
        endpoint_ref="endpoint-ref-1",
        capabilities=["reply"],
    )
    principals = PrincipalRepository(in_memory_db)
    endpoints = EndpointRepository(in_memory_db)

    principals.insert(principal)
    endpoints.insert(endpoint)

    assert in_memory_db.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 0
    assert in_memory_db.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 0
    assert principals.find(principal.principal_id) == principal
    assert principals.find_by_platform("test", "account-1") == principal
    assert endpoints.find_by_platform("channel-1", "account-1") == endpoint
    assert endpoints.find_by_ref("endpoint-ref-1") == endpoint

    duplicate = endpoints.insert(
        Endpoint(
            endpoint_id="endpoint-event-only-duplicate",
            channel_type="test",
            channel_instance_id="channel-1",
            platform_account_id="account-1",
            principal_id="principal-should-not-win",
        )
    )
    assert duplicate == endpoint
    assert len(EventStore(in_memory_db).read_stream_type("endpoint")) == 1


def test_delivery_replay_recovers_interrupted_streaming_delivery(in_memory_db):
    store = EventStore(in_memory_db)
    _append(
        store,
        event_type="delivery.requested",
        stream_type="delivery",
        stream_id="delivery-1",
        event_class=EventClass.DOMAIN,
        context=EventContext(turn_id="turn-1", attempt_id="attempt-1"),
        payload_ref="payload://response/1",
        outcome="streaming",
    )
    _append(
        store,
        event_type="delivery.started",
        stream_type="delivery",
        stream_id="delivery-1",
        event_class=EventClass.OPERATION,
        context=EventContext(attempt_id="attempt-1"),
    )
    _append(
        store,
        event_type="delivery.failed",
        stream_type="delivery",
        stream_id="delivery-1",
        event_class=EventClass.DOMAIN,
        context=EventContext(attempt_id="attempt-1"),
        outcome="interrupted",
        error_category="cancelled",
    )

    state = replay_delivery(store.read_stream("delivery", "delivery-1"), "delivery-1")
    assert state is not None
    assert state.status == "interrupted"
    assert state.attempt_id == "attempt-1"
    assert state.content_ref == "payload://response/1"
    assert state.error_category == "cancelled"
    assert state.stream_version == 3


def test_turn_repository_emits_a_replayable_lifecycle(in_memory_db):
    ConversationRepository(in_memory_db).insert(
        Conversation(conversation_id="conversation-1")
    )
    SessionRepository(in_memory_db).insert(
        Session(session_id="session-1", conversation_id="conversation-1")
    )
    repository = TurnRepository(in_memory_db)
    repository.insert(
        Turn(
            turn_id="turn-1",
            session_id="session-1",
            input_message_id="message-1",
            status=TurnStatus.accepted,
        )
    )
    assert repository.update_status("turn-1", TurnStatus.running, expected_version=1)
    assert repository.update_status("turn-1", TurnStatus.completed, expected_version=2)

    stream = EventStore(in_memory_db).read_stream("turn", "turn-1")
    assert [event.event_type for event in stream] == [
        "runtime.turn.accepted",
        "runtime.turn.started",
        "runtime.turn.completed",
    ]
    state = replay_turn(stream, "turn-1")
    assert state is not None
    assert state.status == "completed"
    assert state.session_id == "session-1"


def test_streaming_event_store_emits_a_replayable_streaming_lifecycle(in_memory_db):
    repository = StreamingDeliveryEventStore(in_memory_db)
    repository.create_streaming_delivery(
        delivery_id="delivery-1",
        attempt_id="attempt-1",
        target={"channel": "web"},
        content_ref="payload://response/1",
        degradation_mode="edit_placeholder",
        idempotency_key="delivery-key-1",
        policy={},
        turn_id="",
    )
    repository.mark_placeholder("delivery-1", "attempt-1", "platform-message-1")
    repository.withdraw("delivery-1", "attempt-1", reason="network")

    stream = EventStore(in_memory_db).read_stream("delivery", "delivery-1")
    assert [event.event_type for event in stream] == [
        "delivery.requested",
        "delivery.started",
        "delivery.failed",
    ]
    assert stream[0].attributes["delivery_mode"] == "streaming"
    assert stream[0].attributes["content_mode"] == "provisional"
    assert stream[1].attributes["platform_message_id"] == "platform-message-1"
    assert stream[2].outcome == "failed"
    assert stream[2].error_category == "network"
    assert EventEffectRecoveryPlanner(EventStore(in_memory_db)).pending_effects() == []
    state = replay_delivery(stream, "delivery-1")
    assert state is not None
    assert state.status == "failed"
    assert state.error_category == "network"


def test_streaming_delivery_cancellation_is_a_terminal_event(in_memory_db):
    repository = StreamingDeliveryEventStore(in_memory_db)
    repository.create_streaming_delivery(
        delivery_id="delivery-cancelled",
        attempt_id="attempt-1",
        target={"channel": "web"},
        content_ref="payload://response/1",
        degradation_mode="edit_placeholder",
        idempotency_key="delivery-key-cancelled",
        policy={},
        turn_id="",
    )
    repository.withdraw("delivery-cancelled", "attempt-1", reason="cancelled")

    stream = EventStore(in_memory_db).read_stream("delivery", "delivery-cancelled")
    assert [event.event_type for event in stream] == [
        "delivery.requested",
        "delivery.cancelled",
    ]
    assert stream[-1].outcome == "cancelled"
    assert EventEffectRecoveryPlanner(EventStore(in_memory_db)).pending_effects() == []


def test_approval_service_emits_a_replayable_tool_approval_lifecycle(in_memory_db):
    service = SqliteApprovalService(in_memory_db)
    approval = service.create(
        turn_id="turn-1",
        request={
            "kind": "tool_call",
            "tool_call_id": "tool-call-1",
            "attempt_id": "attempt-1",
            "capability_id": "calendar",
            "tool_version": "1",
            "arguments_hash": "safe-hash",
            "arguments_snapshot_ref": "payload://tool-args/1",
            "risk_level": "high",
        },
    )
    decision = service.approve(approval.approval_id, "owner", expected_version=1)
    assert decision.status == "approved"
    assert service.consume_approved_tool_call(approval.approval_id, expected_version=2)

    stream = EventStore(in_memory_db).read_stream("approval", approval.approval_id)
    assert [event.event_type for event in stream] == [
        "approval.requested",
        "approval.responded",
        "approval.consumed",
    ]
    assert "request" not in stream[0].attributes
    state = replay_approval(stream, approval.approval_id)
    assert state is not None
    assert state.status == "approved"
    assert state.consumed is True
    assert state.turn_id == "turn-1"
    assert in_memory_db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_knowledge_service_events_replay_resource_ingestion(in_memory_db):
    service = KnowledgeService(in_memory_db)
    resource = service.register_resource(
        source_uri_hash="source-hash-1",
        principal_id="owner",
        content_hash="content-hash-1",
    )
    document, segments = service.ingest(resource.resource_id, "# Title\n\nA short paragraph.")

    stream = EventStore(in_memory_db).read_stream("knowledge_resource", resource.resource_id)
    assert [event.event_type for event in stream] == [
        "knowledge.resource.created",
        "knowledge.document.parsed",
        "knowledge.resource.ingested",
    ]
    assert in_memory_db.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0
    state = replay_knowledge_resource(stream, resource.resource_id)
    assert state is not None
    assert state.status == "active"
    assert state.document_id == document.document_id
    assert state.segment_count == len(segments)


def test_memory_service_events_replay_candidate_confirmation(in_memory_db):
    service = SqliteMemoryService(in_memory_db)
    memory = service.propose(
        kind="fact",
        subject="user",
        predicate="prefers",
        value="concise answers",
        principal_id="owner",
        status="candidate",
    )
    assert memory is not None
    assert service.confirm(memory.memory_id, confirmed_by="owner", expected_version=memory.version)

    stream = EventStore(in_memory_db).read_stream("memory", memory.memory_id)
    assert [event.event_type for event in stream] == [
        "memory.candidate.created",
        "memory.confirmed",
    ]
    assert in_memory_db.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0
    assert "value" not in stream[0].attributes
    state = replay_memory(stream, memory.memory_id)
    assert state is not None
    assert state.status == "confirmed"
    assert state.principal_id == "owner"


def test_connector_source_event_replays_safe_ingestion_metadata(in_memory_db):
    EventStore(in_memory_db).append(
        Event(
            event_type="connector.source.ingested",
            stream_type="source",
            stream_id="external-item-1",
            producer="test",
            event_class=EventClass.DOMAIN,
            payload_ref="connector-item-1",
            payload_hash="content-hash-1",
            attributes={
                "connector_id": "connector-1",
                "source_item_id": "external-item-1",
                "item_status": "digest",
            },
        )
    )
    stream = EventStore(in_memory_db).read_stream("source", "external-item-1")
    state = replay_connector_source(stream, "external-item-1")
    assert state is not None
    assert state.connector_id == "connector-1"
    assert state.item_status == "digest"
    assert state.payload_ref == "connector-item-1"
    assert "summary" not in stream[0].attributes


def test_proactive_candidate_and_decision_replay_from_one_stream(in_memory_db):
    candidate = ProactiveCandidate(
        candidate_id="candidate-1",
        principal_id="owner",
        stream_type="content",
        summary="do not store this text in the event",
        origin="connector",
        source_payload_ref="connector-item-1",
        created_at=1_000,
    )
    ProactiveCandidateRepository(in_memory_db).insert(candidate)
    ProactiveDecisionRepository(in_memory_db).insert(
        ProactiveDecision(
            decision_id="decision-1",
            candidate_id=candidate.candidate_id,
            principal_id="owner",
            action="send_now",
            delivery_id="delivery-1",
            decided_at=2_000,
        )
    )

    stream = EventStore(in_memory_db).read_stream("proactive_candidate", candidate.candidate_id)
    assert [event.event_type for event in stream] == [
        "proactive.candidate.created",
        "proactive.decision.made",
    ]
    assert "summary" not in stream[0].attributes
    state = replay_proactive_candidate(stream, candidate.candidate_id)
    assert state is not None
    assert state.status == "decided"
    assert state.action == "send_now"
    assert state.delivery_id == "delivery-1"


def test_query_lists_use_event_replay_without_legacy_projection_rows(in_memory_db):
    store = EventStore(in_memory_db)
    _append(
        store,
        event_type="runtime.turn.accepted",
        stream_type="turn",
        stream_id="event-turn-1",
        event_class=EventClass.DOMAIN,
        context=EventContext(session_id="event-session-1", turn_id="event-turn-1"),
        attributes={"priority": 70, "input_message_id": "event-message-1"},
        outcome="accepted",
    )
    _append(
        store,
        event_type="task.created",
        stream_type="task",
        stream_id="event-task-1",
        event_class=EventClass.DOMAIN,
        context=EventContext(task_id="event-task-1"),
        attributes={"task_type": "event-only", "priority": 40, "origin": "test"},
        outcome="queued",
    )
    _append(
        store,
        event_type="delivery.requested",
        stream_type="delivery",
        stream_id="event-delivery-1",
        event_class=EventClass.DOMAIN,
        payload_ref="payload://event-only/1",
        outcome="pending",
    )

    query = QueryService(in_memory_db, Config())
    turn = query.list_turns()["items"][0]
    assert turn["turn_id"] == "event-turn-1"
    assert turn["session_id"] == "event-session-1"
    assert turn["status"] == "accepted"
    assert query.list_tasks()["items"][0]["task_id"] == "event-task-1"
    assert query.list_tasks()["items"][0]["task_type"] == "event-only"
    assert query.list_deliveries()["items"][0]["delivery_id"] == "event-delivery-1"


def test_delivery_queries_do_not_fall_back_to_legacy_projection_rows(in_memory_db):
    in_memory_db.execute(
        "INSERT INTO deliveries (delivery_id,status,idempotency_key,created_at) "
        "VALUES ('legacy-query-only','unknown','legacy-query-key',1700000000000)"
    )
    in_memory_db.commit()

    query = QueryService(in_memory_db, Config())
    assert query.list_deliveries()["items"] == []
    assert query.get_delivery_detail("legacy-query-only") is None
    assert all(item["kind"] != "unknown_delivery" for item in query.attention_items())


def test_effect_recovery_is_derived_from_events_without_outbox_rows(in_memory_db):
    store = EventStore(in_memory_db)
    _append(
        store,
        event_type="delivery.requested",
        stream_type="delivery",
        stream_id="delivery-pending",
        event_class=EventClass.DOMAIN,
        payload_ref="payload://delivery/pending",
    )
    _append(
        store,
        event_type="delivery.requested",
        stream_type="delivery",
        stream_id="delivery-done",
        event_class=EventClass.DOMAIN,
    )
    _append(
        store,
        event_type="delivery.completed",
        stream_type="delivery",
        stream_id="delivery-done",
        event_class=EventClass.DOMAIN,
        outcome="sent",
    )
    _append(
        store,
        event_type="tool.call.requested",
        stream_type="tool_call",
        stream_id="tool-unknown",
        event_class=EventClass.OPERATION,
        payload_ref="payload://tool/unknown",
    )
    _append(
        store,
        event_type="tool.call.unknown",
        stream_type="tool_call",
        stream_id="tool-unknown",
        event_class=EventClass.OPERATION,
    )

    effects = EventEffectRecoveryPlanner(store).pending_effects()
    assert [(effect.stream_id, effect.state) for effect in effects] == [
        ("delivery-pending", "pending"),
        ("tool-unknown", "unknown"),
    ]


def test_canonical_effect_worker_appends_terminal_event_idempotently(in_memory_db):
    class RecordingExecutor:
        def __init__(self) -> None:
            self.request_ids: list[str] = []

        def execute(self, effect):
            self.request_ids.append(effect.request_event_id)
            return EffectOutcome("completed")

    store = EventStore(in_memory_db)
    request = _append(
        store,
        event_type="delivery.requested",
        stream_type="delivery",
        stream_id="delivery-effect-1",
        event_class=EventClass.DOMAIN,
        context=EventContext(trace_id="trace-effect-1"),
        payload_ref="payload://delivery/effect-1",
    )
    executor = RecordingExecutor()
    worker = CanonicalEffectWorker(store, executor)

    assert worker.run_pending() == 1
    assert worker.run_pending() == 0
    assert executor.request_ids == [request.event_id]
    stream = store.read_stream("delivery", "delivery-effect-1")
    assert [event.event_type for event in stream] == [
        "delivery.requested",
        "delivery.started",
        "delivery.completed",
    ]
    assert stream[-1].context.causation_id == request.event_id


def test_canonical_delivery_executor_sends_only_from_protected_event_payload(in_memory_db, tmp_path):
    class RecordingGateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def send(self, target: str, content: str, idempotency_key: str) -> GatewayResult:
            self.calls.append((target, content, idempotency_key))
            return GatewayResult(status="success", platform_message_id="platform-1")

    payload_store = PayloadStore(tmp_path / "payloads", in_memory_db)
    payload_ref, payload_hash = store_delivery_effect_payload(
        payload_store,
        DeliveryEffectPayload(
            delivery_id="delivery-event-only-1",
            target_snapshot={"channel": "web", "principal_id": "owner"},
            content="resolved response body",
            content_ref="legacy-message-reference",
            idempotency_key="provider-idempotency-1",
        ),
    )
    store = EventStore(in_memory_db)
    _append(
        store,
        event_type="delivery.requested",
        stream_type="delivery",
        stream_id="delivery-event-only-1",
        event_class=EventClass.DOMAIN,
        payload_ref=payload_ref,
        payload_hash=payload_hash,
    )
    gateway = RecordingGateway()

    assert CanonicalEffectWorker(
        store,
        CanonicalDeliveryEffectExecutor(payload_store, gateway),
    ).run_pending() == 1
    assert gateway.calls == [
        (
            '{"channel":"web","principal_id":"owner"}',
            "resolved response body",
            "provider-idempotency-1",
        )
    ]
    assert [event.event_type for event in store.read_stream("delivery", "delivery-event-only-1")] == [
        "delivery.requested",
        "delivery.started",
        "delivery.completed",
    ]


def test_event_effect_recovery_defers_scheduled_delivery(in_memory_db):
    store = EventStore(in_memory_db)
    _append(
        store,
        event_type="delivery.requested",
        stream_type="delivery",
        stream_id="delivery-scheduled-1",
        event_class=EventClass.DOMAIN,
        attributes={"scheduled_at": "2030-01-01T00:00:00+00:00"},
    )

    assert EventEffectRecoveryPlanner(store, now_ms=lambda: 1).pending_effects() == []
    assert [effect.stream_id for effect in EventEffectRecoveryPlanner(
        store,
        now_ms=lambda: 2_000_000_000_000,
    ).pending_effects()] == ["delivery-scheduled-1"]


def test_runtime_background_cycle_uses_canonical_delivery_worker_only(in_memory_db):
    import asyncio
    from types import SimpleNamespace

    from cogito.application import RuntimeApplication
    from cogito.service.agent_runner import RunOutcome

    class IdleRunner:
        async def run_once(self, worker_id: str) -> RunOutcome:
            return RunOutcome.idle

    class EmptySubscription:
        def run_pending(self, conn, *, limit: int) -> int:
            return 0

    class CanonicalDelivery:
        def __init__(self) -> None:
            self.limits: list[int] = []

        def run_pending(self, *, limit: int) -> int:
            self.limits.append(limit)
            return 1

    class IdleTaskWorker:
        async def run_once(self, worker_id: str) -> str:
            return "idle"

    app = RuntimeApplication(
        config=SimpleNamespace(
            capability=SimpleNamespace(proactive=SimpleNamespace(enabled=False)),
        ),  # type: ignore[arg-type]
        conn=in_memory_db,
        provider=None,  # type: ignore[arg-type]
        runner=IdleRunner(),
        inbound=None,  # type: ignore[arg-type]
    )
    canonical = CanonicalDelivery()
    assert not hasattr(app, "delivery_worker")
    app.event_subscription_worker = EmptySubscription()
    app.canonical_delivery_worker = canonical
    app.task_worker = IdleTaskWorker()

    result = asyncio.run(app.process_background_once("test-worker", delivery_batch=3))

    assert canonical.limits == [3]
    assert result.delivery == 1


def test_delivery_commands_replay_event_stream_without_delivery_row(in_memory_db, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from cogito.contracts.models import ReconcileDeliveryPayload
    from cogito.service.api.command_handlers import reconcile_delivery
    from cogito.service.api.deps import CommandDeps
    class ReconcileGateway:
        def reconcile(self, target: str, platform_message_id: str | None, idempotency_key: str):
            return GatewayResult(status="success", platform_message_id="reconciled-platform-id")

    payload_store = PayloadStore(tmp_path / "payloads", in_memory_db)
    service = SqliteDeliveryService(
        in_memory_db,
        gateway=ReconcileGateway(),
        effect_payload_store=payload_store,
    )
    cancelled = asyncio.run(
        service.enqueue(
            DeliveryRequest(
                target={"channel": "web", "principal_id": "owner"},
                content_ref="event-only-body",
                idempotency_key="event-only-cancel",
            )
        )
    )
    assert in_memory_db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    before_cancel = service.get(cancelled.delivery_id)
    assert before_cancel is not None and before_cancel.stream_version == 1
    assert before_cancel.target_snapshot["principal_id"] == "owner"

    asyncio.run(service.cancel(cancelled.delivery_id, before_cancel.stream_version))
    after_cancel = service.get(cancelled.delivery_id)
    assert after_cancel is not None and after_cancel.status == "cancelled"
    assert [event.event_type for event in EventStore(in_memory_db).read_stream("delivery", cancelled.delivery_id)] == [
        "delivery.requested",
        "delivery.cancelled",
    ]

    retryable = asyncio.run(
        service.enqueue(
            DeliveryRequest(
                target={"channel": "web"},
                content_ref="event-only-body",
                idempotency_key="event-only-retry",
            )
        )
    )
    store = EventStore(in_memory_db)
    retry_request = store.read_stream("delivery", retryable.delivery_id)[0]
    store.append(
        Event(
            event_type="delivery.retry_scheduled",
            stream_type="delivery",
            stream_id=retryable.delivery_id,
            producer="test",
            event_class=EventClass.DOMAIN,
            context=EventContext(causation_id=retry_request.event_id),
            outcome="retry_scheduled",
        ),
        expected_version=1,
    )
    in_memory_db.commit()
    retry_view = service.get(retryable.delivery_id)
    assert retry_view is not None and retry_view.status == "retry_scheduled"

    asyncio.run(service.retry(retryable.delivery_id, retry_view.stream_version))
    retry_events = store.read_stream("delivery", retryable.delivery_id)
    assert retry_events[-1].event_type == "delivery.retry_requested"
    assert service.get(retryable.delivery_id).status == "pending"  # type: ignore[union-attr]

    unknown = asyncio.run(
        service.enqueue(
            DeliveryRequest(
                target={"channel": "web"},
                content_ref="event-only-body",
                idempotency_key="event-only-reconcile",
            )
        )
    )
    store = EventStore(in_memory_db)
    request = store.read_stream("delivery", unknown.delivery_id)[0]
    store.append(
        Event(
            event_type="delivery.unknown",
            stream_type="delivery",
            stream_id=unknown.delivery_id,
            producer="test",
            event_class=EventClass.DOMAIN,
            context=EventContext(causation_id=request.event_id),
            attributes={"platform_message_id": "stored-platform-id"},
            outcome="unknown",
        ),
        expected_version=1,
    )
    in_memory_db.commit()

    config = SimpleNamespace(resolve_payload_dir=lambda: tmp_path / "payloads")
    detail = QueryService(in_memory_db, config).get_delivery_detail(unknown.delivery_id)
    assert detail is not None
    assert detail["status"] == "unknown"
    assert detail["target_snapshot"]["channel"] == "web"
    assert detail["operation_sequence"][-1]["event_type"] == "delivery.unknown"

    response = reconcile_delivery(
        ReconcileDeliveryPayload(delivery_id=unknown.delivery_id),
        CommandDeps(conn=in_memory_db, config=config, recovery_counts={}),
    )
    view = service.get(unknown.delivery_id)
    assert response.status == "ok"
    assert view is not None and view.status == "completed"
    assert view.platform_message_id == "stored-platform-id"


def test_delivery_request_event_references_protected_effect_payload(in_memory_db, tmp_path):
    import asyncio

    payload_store = PayloadStore(tmp_path / "payloads", in_memory_db)
    service = SqliteDeliveryService(
        in_memory_db,
        effect_payload_store=payload_store,
    )
    delivery = asyncio.run(
        service.enqueue(
            DeliveryRequest(
                target={"channel": "web", "principal_id": "owner"},
                content_ref="message-1",
                idempotency_key="delivery-effect-key",
            )
        )
    )

    event = EventStore(in_memory_db).read_stream("delivery", delivery.delivery_id)[0]
    assert event.event_type == "delivery.requested"
    assert event.attributes == {"effect_payload_kind": "delivery-effect.v2"}
    assert event.payload_ref
    assert event.payload_hash
    assert "target" not in event.attributes
    effect = load_delivery_effect_payload(payload_store, event.payload_ref)
    assert effect.delivery_id == delivery.delivery_id
    assert effect.target_snapshot["principal_id"] == "owner"
    assert effect.content == "message-1"
    assert effect.content_ref == "message-1"
