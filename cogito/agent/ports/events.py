# cogito/agent/ports/events.py

from __future__ import annotations

import logging
from typing import Protocol

from cogito.agent.runtime.events import AgentEvent

logger = logging.getLogger(__name__)


class AgentEventSink(Protocol):
    """Abstract destination for runtime events."""

    async def emit(self, event: AgentEvent) -> None:
        ...


class NullAgentEventSink:
    """Discards all events."""

    async def emit(self, event: AgentEvent) -> None:
        return None


class InMemoryAgentEventSink:
    """Collects events in memory for testing."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


class CompositeAgentEventSink:
    """Broadcasts events to multiple sinks."""

    def __init__(self, sinks: list[AgentEventSink]) -> None:
        self._sinks = list(sinks)

    async def emit(self, event: AgentEvent) -> None:
        for sink in self._sinks:
            try:
                await sink.emit(event)
            except Exception:
                logger.exception(
                    "Agent event sink failed",
                    extra={"event_type": event.type},
                )
