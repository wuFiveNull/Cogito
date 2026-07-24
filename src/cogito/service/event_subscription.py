"""Event-log consumer adapter and at-least-once subscription worker.

This replaces the runtime Outbox lease/publish loop.  The temporary legacy
consumer implementations still receive the small delivery-shaped view they
expect, but the source of work is always the immutable ``event_log``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from cogito.domain.event import Event, EventContext
from cogito.store.event_store import EventStore

_LOGGER = logging.getLogger(__name__)


class ConsumerEvent(Protocol):
    """The minimal, read-only event view accepted by legacy consumers."""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    payload_ref: str | None
    content_hash: str
    schema_version: str
    correlation_id: str
    causation_id: str
    origin: str
    trust_label: str
    context: EventContext


_CANONICAL_TO_LEGACY_TYPE = {
    "interaction.message.accepted": "InboundMessageAccepted",
    "runtime.turn.completed": "TurnCompleted",
    "runtime.session.completed": "SessionCompleted",
    "connector.source.ingested": "SourceEventIngested",
    "memory.source.invalidated": "MemorySourceInvalidated",
    "drift.result.committed": "DriftResultCommitted",
}


@dataclass(frozen=True, slots=True)
class CanonicalConsumerEvent:
    """Compatibility view created from a canonical Event, never persisted."""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    payload_ref: str | None
    content_hash: str
    schema_version: str
    correlation_id: str
    causation_id: str
    origin: str
    trust_label: str = "unverified"
    context: EventContext = field(default_factory=EventContext)

    @classmethod
    def from_event(cls, event: Event) -> CanonicalConsumerEvent | None:
        legacy_type = _CANONICAL_TO_LEGACY_TYPE.get(event.event_type)
        if legacy_type is None:
            return None
        # The old immediate-evaluation consumer schedules work per Turn.  Its
        # canonical input is a message stream, so use the explicit subject
        # identifier rather than the message stream id.
        aggregate_id = event.context.turn_id or event.stream_id
        return cls(
            event_id=event.event_id,
            event_type=legacy_type,
            aggregate_type=event.stream_type,
            aggregate_id=aggregate_id,
            aggregate_version=event.stream_version,
            payload_ref=event.payload_ref,
            content_hash=event.payload_hash,
            schema_version=str(event.type_version),
            correlation_id=event.context.correlation_id,
            causation_id=event.context.causation_id,
            origin=event.producer,
            context=event.context,
        )


class CanonicalEventConsumerWorker:
    """Dispatch subscribed Event facts without queue rows or worker leases.

    Consumers remain at-least-once: an unsuccessful handler is simply found on
    the next scan.  Their result facts must be appended with their own causal
    idempotency key; the remaining compatibility consumers also retain their
    existing consumption key until their projections are removed.
    """

    def __init__(self, event_store: EventStore, registry: object) -> None:
        self._events = event_store
        self._registry = registry

    def run_pending(self, conn: sqlite3.Connection, *, limit: int = 50) -> int:
        consumed = 0
        for event in self._events.read_events_by_type(
            frozenset(_CANONICAL_TO_LEGACY_TYPE), limit=max(1, limit)
        ):
            envelope = CanonicalConsumerEvent.from_event(event)
            if envelope is None:
                continue
            consumer = self._registry.find(envelope)
            if consumer is None:
                continue
            try:
                if consumer.handle(conn, envelope):
                    consumed += 1
            except Exception:
                _LOGGER.exception(
                    "canonical event consumer failed: event=%s consumer=%s",
                    event.event_id,
                    consumer.name,
                )
        return consumed
