"""Execute canonical requested effects and record only terminal Events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cogito.domain.event import Event, EventClass, EventContext
from cogito.service.event_effect_recovery import EventEffectRecoveryPlanner, PendingEffect
from cogito.store.event_store import EventStore


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    """Provider result; callers must use ``request_event_id`` as their idempotency key."""

    state: str  # completed | failed | unknown
    error_category: str = ""


class EffectExecutor(Protocol):
    def execute(self, effect: PendingEffect) -> EffectOutcome:
        """Perform/reconcile one effect using ``effect.request_event_id`` idempotently."""
        ...


class CanonicalEffectWorker:
    """Event-driven worker with no queue/lease table.

    At-least-once execution is intentional: a crash after the provider call but
    before appending a terminal Event is recovered by re-running the same
    request Event. External adapters must therefore bind their existing
    idempotency and receipt validation to ``request_event_id``.
    """

    def __init__(self, event_store: EventStore, executor: EffectExecutor) -> None:
        self._events = event_store
        self._planner = EventEffectRecoveryPlanner(event_store)
        self._executor = executor

    def run_pending(self, *, limit: int = 50) -> int:
        completed = 0
        for effect in self._planner.pending_effects()[: max(1, limit)]:
            self._append_started(effect)
            outcome = self._executor.execute(effect)
            self._append_terminal(effect, outcome)
            completed += 1
        return completed

    def _append_started(self, effect: PendingEffect) -> None:
        self._events.append(
            Event(
                event_type=self._event_type(effect.effect_type, "started"),
                stream_type=effect.effect_type,
                stream_id=effect.stream_id,
                producer="canonical-effect-worker",
                event_class=EventClass.OPERATION,
                context=EventContext(
                    trace_id=effect.trace_id,
                    causation_id=effect.request_event_id,
                ),
                summary=f"{effect.effect_type} effect started",
                outcome="running",
                idempotency_key=f"effect:{effect.request_event_id}:started",
            )
        )

    def _append_terminal(self, effect: PendingEffect, outcome: EffectOutcome) -> None:
        if outcome.state not in {"completed", "failed", "unknown"}:
            raise ValueError(f"unsupported effect outcome: {outcome.state}")
        event_type = self._event_type(effect.effect_type, outcome.state)
        self._events.append(
            Event(
                event_type=event_type,
                stream_type=effect.effect_type,
                stream_id=effect.stream_id,
                producer="canonical-effect-worker",
                event_class=(
                    EventClass.OPERATION if effect.effect_type == "tool_call" else EventClass.DOMAIN
                ),
                context=EventContext(
                    trace_id=effect.trace_id,
                    causation_id=effect.request_event_id,
                ),
                summary=f"{effect.effect_type} effect {outcome.state}",
                outcome=outcome.state,
                error_category=outcome.error_category,
                idempotency_key=f"effect:{effect.request_event_id}:{outcome.state}",
            )
        )

    @staticmethod
    def _event_type(effect_type: str, state: str) -> str:
        prefix = "tool.call" if effect_type == "tool_call" else "delivery"
        return f"{prefix}.{state}"
