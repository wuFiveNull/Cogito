from __future__ import annotations

from cogito.domain.event import Event, EventClass, EventContext
from cogito.service.event_subscription import CanonicalEventConsumerWorker, ConsumerEvent
from cogito.store.event_store import EventStore


class _Consumer:
    name = "test-consumer"

    def __init__(self) -> None:
        self.received: list[ConsumerEvent] = []

    def handle(self, _conn, event: ConsumerEvent) -> bool:
        self.received.append(event)
        return True


class _Registry:
    def __init__(self, consumer: _Consumer) -> None:
        self._consumer = consumer

    def find(self, event: ConsumerEvent):
        return self._consumer if event.event_type == "InboundMessageAccepted" else None


def test_subscription_worker_reads_canonical_event_log_without_outbox(in_memory_db):
    store = EventStore(in_memory_db)
    store.append(
        Event(
            event_type="interaction.message.accepted",
            stream_type="message",
            stream_id="message-1",
            producer="test",
            event_class=EventClass.DOMAIN,
            context=EventContext(turn_id="turn-1", correlation_id="trace-1"),
        )
    )
    consumer = _Consumer()
    worker = CanonicalEventConsumerWorker(store, _Registry(consumer))

    assert worker.run_pending(in_memory_db) == 1
    received = consumer.received[0]
    assert received.aggregate_id == "turn-1"
    assert received.correlation_id == "trace-1"
    assert received.context.turn_id == "turn-1"


def test_subscription_worker_preserves_completion_subject_context(in_memory_db):
    store = EventStore(in_memory_db)
    store.append(
        Event(
            event_type="runtime.turn.completed",
            stream_type="turn",
            stream_id="turn-1",
            producer="turn-completion",
            event_class=EventClass.DOMAIN,
            context=EventContext(
                trace_id="trace-1",
                correlation_id="trace-1",
                conversation_id="conversation-1",
                session_id="session-1",
                principal_id="principal-1",
                turn_id="turn-1",
                attempt_id="attempt-1",
            ),
        )
    )

    class _CompletionRegistry(_Registry):
        def find(self, event: ConsumerEvent):
            return self._consumer if event.event_type == "TurnCompleted" else None

    consumer = _Consumer()
    assert CanonicalEventConsumerWorker(store, _CompletionRegistry(consumer)).run_pending(in_memory_db) == 1
    received = consumer.received[0]
    assert received.aggregate_id == "turn-1"
    assert received.context.session_id == "session-1"
    assert received.context.principal_id == "principal-1"


def test_subscription_worker_ignores_unsubscribed_events(in_memory_db):
    store = EventStore(in_memory_db)
    store.append(
        Event(
            event_type="task.created",
            stream_type="task",
            stream_id="task-1",
            producer="test",
            event_class=EventClass.DOMAIN,
        )
    )
    consumer = _Consumer()

    assert CanonicalEventConsumerWorker(store, _Registry(consumer)).run_pending(in_memory_db) == 0
    assert consumer.received == []
