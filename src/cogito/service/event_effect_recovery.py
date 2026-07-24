"""Derive recoverable external-effect work solely from canonical Events."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import time

from cogito.domain.event import Event
from cogito.store.event_store import EventStore


@dataclass(frozen=True, slots=True)
class PendingEffect:
    """An idempotent external request that needs execution or reconciliation."""

    effect_type: str
    stream_id: str
    request_event_id: str
    state: str
    payload_ref: str | None
    payload_hash: str
    trace_id: str
    causation_id: str


class EventEffectRecoveryPlanner:
    """Replacement read side for the legacy Outbox recovery scan.

    Consumers receive a request Event's immutable identity and retain their
    existing provider idempotency key/receipt validation. They append a
    terminal Event after the side effect; no mutable worker cursor is stored.
    """

    def __init__(
        self,
        event_store: EventStore,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._events = event_store
        self._now_ms = now_ms or (lambda: int(time() * 1000))

    def pending_effects(self) -> list[PendingEffect]:
        now_ms = self._now_ms()
        return [
            *self._pending_for_stream(
                "delivery",
                requested="delivery.requested",
                terminal={
                    "delivery.completed",
                    "delivery.failed",
                    "delivery.unknown",
                    "delivery.cancelled",
                    "delivery.retry_scheduled",
                },
                unknown="delivery.unknown",
                now_ms=now_ms,
            ),
            *self._pending_for_stream(
                "tool_call",
                requested="tool.call.requested",
                terminal={"tool.call.completed", "tool.call.failed", "tool.call.cancelled"},
                unknown="tool.call.unknown",
                now_ms=now_ms,
            ),
        ]

    def _pending_for_stream(
        self,
        stream_type: str,
        *,
        requested: str,
        terminal: set[str],
        unknown: str,
        now_ms: int,
    ) -> list[PendingEffect]:
        by_stream: dict[str, list[Event]] = {}
        for event in self._events.read_stream_type(stream_type):
            by_stream.setdefault(event.stream_id, []).append(event)

        pending: list[PendingEffect] = []
        for stream_id, events in by_stream.items():
            request = next((event for event in events if event.event_type == requested), None)
            if request is None:
                continue
            if stream_type == "delivery" and request.attributes.get("delivery_mode") == "streaming":
                # Streaming Delivery is driven synchronously by its Turn. Its
                # lifecycle facts must not be sent again by the background
                # worker that handles ordinary requested effects.
                continue
            scheduled_at = _scheduled_at_ms(request.attributes.get("scheduled_at"))
            if scheduled_at is not None and scheduled_at > now_ms:
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


def _scheduled_at_ms(value: object) -> int | None:
    """Interpret the safe scheduling attribute; malformed values must not send early."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    # A malformed scheduling field is an operator-visible configuration issue,
    # not permission to send the effect immediately.
    return 2**63 - 1
