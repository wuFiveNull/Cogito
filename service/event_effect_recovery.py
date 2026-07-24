"""Derive recoverable external-effect work solely from canonical Events."""

from __future__ import annotations

from dataclasses import dataclass

from cogito.domain.event import Event
from cogito.store.event_store import EventStore


@dataclass(frozen=True, slots=True)
class PendingEffect:
    """An idempotent external request that needs execution or reconciliation."""

    effect_type: str
    stream_id: str
    request_event_id: str
    state: str  # pending | unknown
    payload_ref: str | None
    payload_hash: str
    trace_id: str
    causation_id: str


class EventEffectRecoveryPlanner:
    """Replacement read side for the legacy Outbox recovery scan.

    Consumers receive a request Event's immutable identity and retain their
    existing provider idempotency key/receipt validation.  They append a
    terminal Event after the side effect; no worker cursor or mutable queue is
    stored here.
    """

    def __init__(self, event_store: EventStore) -> None:
        self._events = event_store

    def pending_effects(self) -> list[PendingEffect]:
        return [
            *self._pending_for_stream(
                "delivery",
                requested="delivery.requested",
                terminal={"delivery.completed", "delivery.failed"},
                unknown="delivery.unknown",
            ),
            *self._pending_for_stream(
                "tool_call",
                requested="tool.call.requested",
                terminal={"tool.call.completed", "tool.call.failed", "tool.call.cancelled"},
                unknown="tool.call.unknown",
            ),
        ]

    def _pending_for_stream(
        self,
        stream_type: str,
        *,
        requested: str,
        terminal: set[str],
        unknown: str,
    ) -> list[PendingEffect]:
        by_stream: dict[str, list[Event]] = {}
        for event in self._events.read_stream_type(stream_type):
            by_stream.setdefault(event.stream_id, []).append(event)

        pending: list[PendingEffect] = []
        for stream_id, events in by_stream.items():
            request = next((event for event in events if event.event_type == requested), None)
            if request is None:
                continue
            latest = events[-1]
            if latest.event_type in terminal:
                continue
            state = "unknown" if latest.event_type == unknown else "pending"
            pending.append(
                PendingEffect(
                    effect_type=stream_type,
                    stream_id=stream_id,
                    request_event_id=request.event_id,
                    state=state,
                    payload_ref=request.payload_ref,
                    payload_hash=request.payload_hash,
                    trace_id=request.context.trace_id,
                    causation_id=request.context.causation_id,
                )
            )
        return sorted(pending, key=lambda effect: effect.request_event_id)
