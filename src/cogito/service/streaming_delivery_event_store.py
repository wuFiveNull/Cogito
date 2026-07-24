"""Event-only lifecycle persistence for synchronous streaming Delivery.

Incremental edits are deliberately transient: the platform and connected Web
clients receive them immediately, while the durable Event stream contains only
the request, placeholder creation, and terminal outcome needed for recovery.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from cogito.contracts.clock import Clock, ProductionClock, epoch_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_store import EventStore


class StreamingDeliveryEventStore:
    """Append-only lifecycle writer used by ``StreamingDeliveryController``."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Clock | None = None) -> None:
        self._events = EventStore(conn)
        self._clock = clock or ProductionClock()

    def create_streaming_delivery(
        self,
        *,
        delivery_id: str,
        attempt_id: str,
        target: dict[str, Any],
        content_ref: str,
        degradation_mode: str,
        idempotency_key: str,
        policy: dict[str, Any],
        turn_id: str,
        conversation_id: str = "",
        session_id: str = "",
        context: EventContext | None = None,
    ) -> None:
        """Persist the pre-model-call recovery fact without a mutable row."""
        self._events.append(
            Event(
                event_type="delivery.requested",
                stream_type="delivery",
                stream_id=delivery_id,
                event_class=EventClass.DOMAIN,
                producer="streaming-delivery",
                context=context
                or EventContext(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    attempt_id=attempt_id,
                ),
                summary="Streaming delivery requested",
                attributes={
                    "delivery_mode": "streaming",
                    "degradation_mode": degradation_mode,
                    "content_mode": "provisional",
                    "platform_conversation_id": str(target.get("conversation_id", "")),
                    "channel_id": str(target.get("adapter_id", "")),
                    "throttle_ms": _safe_int(policy.get("throttle_ms")),
                    "max_operations": _safe_int(policy.get("max_operations")),
                },
                payload_ref=content_ref or None,
                outcome="streaming",
                occurred_at=epoch_ms(self._clock.now()),
                idempotency_key=f"streaming:{idempotency_key}:requested",
            )
        )

    def mark_placeholder(self, delivery_id: str, attempt_id: str, platform_message_id: str) -> None:
        self._events.append(
            Event(
                event_type="delivery.started",
                stream_type="delivery",
                stream_id=delivery_id,
                event_class=EventClass.OPERATION,
                producer="streaming-delivery",
                context=self._context(delivery_id, attempt_id),
                summary="Streaming placeholder created",
                attributes={"mode": "placeholder", "platform_message_id": platform_message_id},
                outcome="sending",
                occurred_at=epoch_ms(self._clock.now()),
                idempotency_key=f"streaming:{delivery_id}:started:{attempt_id}",
            )
        )

    def record_edit(
        self,
        delivery_id: str,
        attempt_id: str,
        operation_seq: int,
        platform_message_id: str,
        receipt_kind: str,
    ) -> Event:
        """Edits are intentionally non-durable progress, not Event facts."""
        del delivery_id, attempt_id, operation_seq, platform_message_id, receipt_kind

    def finish_streaming(
        self,
        delivery_id: str,
        final_message_id: str,
        platform_message_id: str,
        final_text: str,
    ) -> None:
        del final_text  # Raw response content belongs to the Assistant Message payload.
        return self._events.append(
            Event(
                event_type="delivery.completed",
                stream_type="delivery",
                stream_id=delivery_id,
                event_class=EventClass.DOMAIN,
                producer="streaming-delivery",
                context=self._context(delivery_id),
                summary="Streaming delivery completed",
                attributes={
                    "final_message_id": final_message_id,
                    "platform_message_id": platform_message_id,
                },
                outcome="sent",
                occurred_at=epoch_ms(self._clock.now()),
                idempotency_key=f"streaming:{delivery_id}:completed",
            )
        )

    def withdraw(self, delivery_id: str, attempt_id: str, reason: str = "cancelled") -> None:
        cancelled = reason == "cancelled"
        self._events.append(
            Event(
                event_type="delivery.cancelled" if cancelled else "delivery.failed",
                stream_type="delivery",
                stream_id=delivery_id,
                event_class=EventClass.DOMAIN,
                producer="streaming-delivery",
                context=self._context(delivery_id, attempt_id),
                summary=(
                    "Streaming delivery cancelled"
                    if cancelled
                    else "Streaming delivery failed"
                ),
                outcome="cancelled" if cancelled else "failed",
                error_category=reason,
                occurred_at=epoch_ms(self._clock.now()),
                idempotency_key=f"streaming:{delivery_id}:terminal:{attempt_id}",
            )
        )

    def _context(self, delivery_id: str, attempt_id: str = "") -> EventContext:
        stream = self._events.read_stream("delivery", delivery_id)
        if not stream:
            raise ValueError(f"streaming delivery {delivery_id} was not requested")
        source = stream[0].context
        return EventContext(
            trace_id=source.trace_id,
            span_id=source.span_id,
            parent_span_id=source.parent_span_id,
            correlation_id=source.correlation_id,
            causation_id=source.causation_id,
            actor_id=source.actor_id,
            principal_id=source.principal_id,
            conversation_id=source.conversation_id,
            session_id=source.session_id,
            turn_id=source.turn_id,
            attempt_id=attempt_id or source.attempt_id,
            task_id=source.task_id,
        )


def _safe_int(value: object) -> int | None:
    return value if isinstance(value, int) else None
