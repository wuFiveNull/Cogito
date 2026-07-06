# tests/agent/ports/test_domain_event_bus_sink.py

from __future__ import annotations

from datetime import datetime

import pytest

from cogito.agent.ports.domain_event_bus_sink import (
    DomainEventBusAgentEventSink,
)
from cogito.agent.runtime.events import AgentEvent, AgentEventType
from cogito.bus.event_bus import DomainEventBus


@pytest.mark.asyncio
async def test_sink_publishes_to_domain_bus() -> None:
    """AgentEvents published via the sink should arrive on the DomainEventBus."""
    bus = DomainEventBus()
    sink = DomainEventBusAgentEventSink(bus)

    received: list = []

    def handler(event):
        received.append(event)
        return None

    bus.on("turn_started", handler)

    event = AgentEvent(
        type=AgentEventType.TURN_STARTED,
        turn_id="turn-1",
        request_id="req-1",
        timestamp=datetime.now(),
    )
    await sink.emit(event)

    assert len(received) == 1
    assert received[0].event_type == "turn_started"
    assert received[0].turn_id == "turn-1"


@pytest.mark.asyncio
async def test_sink_maps_event_type_correctly() -> None:
    """Each AgentEventType should be mapped to the correct LifecycleEvent event_type."""
    bus = DomainEventBus()
    sink = DomainEventBusAgentEventSink(bus)

    type_map: dict[AgentEventType, str] = {
        AgentEventType.TURN_STARTED: "turn_started",
        AgentEventType.MODEL_CALL_STARTED: "llm_call_started",
        AgentEventType.TOOL_CALL_COMPLETED: "tool_call_completed",
        AgentEventType.TOOL_CALL_FAILED: "tool_call_failed",
    }

    for agent_type, expected_lifecycle_type in type_map.items():
        received: list = []

        def handler(event):
            received.append(event)
            return None

        bus.on(expected_lifecycle_type, handler)

        agent_event = AgentEvent(
            type=agent_type,
            turn_id="t-1",
            request_id="r-1",
            timestamp=datetime.now(),
        )
        await sink.emit(agent_event)

        assert len(received) == 1
        assert received[0].event_type == expected_lifecycle_type
