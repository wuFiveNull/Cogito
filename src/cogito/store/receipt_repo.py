"""Side-effect receipt Event boundary and replay read model."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_replay import SideEffectReceiptProjection, replay_side_effect_receipt
from cogito.store.event_store import EventStore


@dataclass
class ReceiptRecord:
    receipt_id: str
    capability_id: str
    operation_id: str | None
    request_hash: str
    side_effect_class: str
    status: str
    reconcile_status: str = "not_needed"
    raw_ref: str | None = None
    summary: str | None = None
    attempt_id: str = ""
    attempt_type: str = "run"
    created_at: int = 0
    resolved_at: int | None = None
    audit_id: str | None = None


class SideEffectReceiptRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: ReceiptRecord) -> None:
        """Record a safe receipt fact; raw provider evidence stays behind payload_ref."""
        EventStore(self._conn).append(
            Event(
                event_type="side_effect.receipt.recorded",
                stream_type="side_effect_receipt",
                stream_id=record.receipt_id,
                producer="side-effect-receipt-repository",
                event_class=EventClass.DOMAIN,
                context=self._context_for_attempt(record.attempt_id),
                summary=f"Side-effect receipt recorded: {record.capability_id}"[:2_000],
                attributes={
                    "capability_id": record.capability_id,
                    "operation_id": record.operation_id or "",
                    "request_hash": record.request_hash,
                    "side_effect_class": record.side_effect_class,
                    "reconcile_status": record.reconcile_status,
                    "attempt_type": record.attempt_type,
                    "audit_id": record.audit_id or "",
                },
                payload_ref=record.raw_ref,
                outcome=record.status,
                occurred_at=record.created_at or 0,
                idempotency_key=f"side-effect-receipt:{record.receipt_id}:recorded",
            )
        )

    def get(self, receipt_id: str) -> ReceiptRecord | None:
        projection = replay_side_effect_receipt(self._events(), receipt_id)
        return self._projection_to_record(projection) if projection is not None else None

    def find_by_attempt(self, attempt_type: str, attempt_id: str) -> list[ReceiptRecord]:
        return [
            record
            for record in self._replayed_records(self._events())
            if record.attempt_type == attempt_type and record.attempt_id == attempt_id
        ]

    def find_pending_reconcile(self, limit: int = 50) -> list[ReceiptRecord]:
        """查询需要人工/自动对账的 unknown 收据。"""
        return [
            record
            for record in self._replayed_records(self._events())
            if record.status == "unknown" and record.reconcile_status == "pending"
        ][:limit]

    def update_status(self, receipt_id: str, status: str, resolved_at: int | None = None) -> None:
        existing = self.get(receipt_id)
        if existing is None:
            return
        prior = EventStore(self._conn).read_stream("side_effect_receipt", receipt_id)
        EventStore(self._conn).append(
            Event(
                event_type="side_effect.receipt.resolved",
                stream_type="side_effect_receipt",
                stream_id=receipt_id,
                producer="side-effect-receipt-repository",
                event_class=EventClass.DOMAIN,
                context=self._child_context(
                    existing.context, prior[-1].event_id if prior else receipt_id
                ),
                summary=f"Side-effect receipt resolved: {existing.capability_id}"[:2_000],
                attributes={},
                outcome=status,
                occurred_at=resolved_at or 0,
                idempotency_key=f"side-effect-receipt:{receipt_id}:resolved:{status}:{resolved_at or ''}",
            )
        )

    def update_reconcile(
        self,
        receipt_id: str,
        reconcile_status: str,
        summary: str | None = None,
    ) -> None:
        existing = self.get(receipt_id)
        if existing is None:
            return
        prior = EventStore(self._conn).read_stream("side_effect_receipt", receipt_id)
        EventStore(self._conn).append(
            Event(
                event_type="side_effect.receipt.reconciled",
                stream_type="side_effect_receipt",
                stream_id=receipt_id,
                producer="side-effect-receipt-repository",
                event_class=EventClass.DOMAIN,
                context=self._child_context(
                    existing.context, prior[-1].event_id if prior else receipt_id
                ),
                summary=f"Side-effect receipt reconciliation: {existing.capability_id}"[:2_000],
                attributes={},
                outcome=reconcile_status,
                occurred_at=0,
                idempotency_key=f"side-effect-receipt:{receipt_id}:reconcile:{reconcile_status}",
            )
        )

    def list_recent(self, limit: int = 50) -> list[ReceiptRecord]:
        return sorted(
            self._replayed_records(self._events()),
            key=lambda record: record.created_at,
            reverse=True,
        )[:limit]

    def _context_for_attempt(self, attempt_id: str) -> EventContext:
        if not attempt_id:
            return EventContext()
        source = next(
            (
                event
                for event in EventStore(self._conn).list_events(attempt_id=attempt_id, limit=500)
                if event.context.trace_id
            ),
            None,
        )
        if source is None:
            return EventContext(attempt_id=attempt_id)
        return self._child_context(source.context, source.event_id, attempt_id=attempt_id)

    @staticmethod
    def _child_context(
        source: EventContext, causation_id: str, *, attempt_id: str | None = None
    ) -> EventContext:
        return EventContext(
            trace_id=source.trace_id,
            span_id=source.span_id,
            parent_span_id=source.parent_span_id,
            correlation_id=source.correlation_id,
            causation_id=causation_id,
            actor_id=source.actor_id,
            principal_id=source.principal_id,
            conversation_id=source.conversation_id,
            session_id=source.session_id,
            turn_id=source.turn_id,
            attempt_id=attempt_id or source.attempt_id,
            task_id=source.task_id,
        )

    def _events(self):
        return EventStore(self._conn).read_stream_type("side_effect_receipt")

    @staticmethod
    def _replayed_records(events) -> list[ReceiptRecord]:
        grouped: dict[str, list[Event]] = {}
        for event in events:
            grouped.setdefault(event.stream_id, []).append(event)
        return sorted(
            (
                SideEffectReceiptRepository._projection_to_record(projection)
                for receipt_id, stream in grouped.items()
                if (projection := replay_side_effect_receipt(stream, receipt_id)) is not None
            ),
            key=lambda record: record.created_at,
        )

    @staticmethod
    def _projection_to_record(projection: SideEffectReceiptProjection) -> ReceiptRecord:
        return ReceiptRecord(
            receipt_id=projection.receipt_id,
            capability_id=projection.capability_id,
            operation_id=projection.operation_id,
            request_hash=projection.request_hash,
            side_effect_class=projection.side_effect_class,
            status=projection.status,
            reconcile_status=projection.reconcile_status,
            raw_ref=projection.raw_ref,
            attempt_id=projection.attempt_id,
            attempt_type=projection.attempt_type,
            created_at=projection.created_at,
            resolved_at=projection.resolved_at,
            audit_id=projection.audit_id,
        )
