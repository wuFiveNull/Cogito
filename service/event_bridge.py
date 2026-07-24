"""Compatibility bridge from legacy DomainEvent producers to canonical Events.

This is deliberately the only legacy adapter.  New code must create an
``Event`` directly; existing producers can be migrated independently without
leaving gaps in the canonical audit stream.
"""

from __future__ import annotations

from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.events import DomainEvent


_LEGACY_TYPE_MAP = {
    "InboundMessageAccepted": "interaction.message.accepted",
    "TurnQueued": "runtime.turn.queued",
    "TurnCompleted": "runtime.turn.completed",
    "SessionCompleted": "runtime.session.completed",
    "ApprovalResponded": "approval.responded",
    "SourceEventIngested": "connector.source.ingested",
    "MemoryExtractionRequested": "memory.extraction.requested",
    "MemorySignalRecorded": "memory.signal.recorded",
    "MemorySourceInvalidated": "memory.source.invalidated",
    "MemoryWeightRecomputed": "memory.weight.recomputed",
    "MemoryConfirmed": "memory.confirmed",
    "MemoryCandidateCreated": "memory.candidate.created",
    "MemoryRejected": "memory.rejected",
    "MemoryErased": "memory.erased",
    "KnowledgeResourceDeleted": "knowledge.resource.deleted",
    "DriftResultCommitted": "drift.result.committed",
}


def canonical_event_from_domain(event: DomainEvent) -> Event:
    """Translate a legacy outbox fact without copying untrusted payload bytes."""
    event_type = _LEGACY_TYPE_MAP.get(event.event_type)
    if event_type is None and event.event_type.endswith("Completed"):
        # Agent-tool command names are dynamic today. Keep their name in the
        # safe metadata while using one registered canonical fact type.
        event_type = "agent.command.completed"
    if event_type is None:
        event_type = "legacy.snapshot.imported"
    trace_id = event.correlation_id
    aggregate_type = event.aggregate_type or "legacy"
    aggregate_id = event.aggregate_id or event.event_id
    context = EventContext(
        trace_id=trace_id,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        turn_id=aggregate_id if aggregate_type == "turn" else "",
        session_id=aggregate_id if aggregate_type == "session" else "",
        task_id=aggregate_id if aggregate_type == "task" else "",
    )
    return Event(
        event_id=event.event_id,
        event_type=event_type,
        stream_type=aggregate_type,
        stream_id=aggregate_id,
        producer=f"legacy:{event.origin or 'system'}",
        event_class=EventClass.DOMAIN if event_type != "legacy.snapshot.imported" else EventClass.TELEMETRY,
        context=context,
        summary=f"{event.event_type} ({aggregate_type}/{aggregate_id})"[:2_000],
        attributes={
            "legacy_event_type": event.event_type,
            "aggregate_version": event.aggregate_version,
            "trust_label": event.trust_label,
        },
        payload_ref=event.payload_ref,
        payload_hash=event.content_hash,
        outcome="recorded",
        type_version=1,
        occurred_at=int(event.occurred_at.timestamp() * 1000),
        idempotency_key=f"legacy-domain:{event.event_id}",
    )
