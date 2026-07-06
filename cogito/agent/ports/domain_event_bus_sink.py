"""
cogito.agent.ports.domain_event_bus_sink — AgentEvent → DomainEventBus 桥接

将 RuntimeKernel 发出的 AgentEvent 转换为 LifecycleEvent，
发布到 DomainEventBus，使外部组件可以订阅 Agent 运行时事件。
"""

from __future__ import annotations

from cogito.agent.ports.events import AgentEventSink
from cogito.agent.runtime.events import AgentEvent
from cogito.bus.event_bus import DomainEventBus
from cogito.bus.events_lifecycle import LifecycleEvent


EVENT_TYPE_MAP: dict[str, str] = {
    "turn_started": "turn_started",
    "turn_completed": "turn_completed",
    "turn_failed": "turn_failed",
    "phase_started": "phase_started",
    "phase_completed": "phase_completed",
    "phase_failed": "phase_failed",
    "model_call_started": "llm_call_started",
    "model_call_completed": "llm_call_completed",
    "model_delta": "model_delta",
    "tool_call_started": "tool_call_started",
    "tool_call_completed": "tool_call_completed",
    "tool_call_failed": "tool_call_failed",
    "retrieval_started": "retrieval_started",
    "retrieval_completed": "retrieval_completed",
    "knowledge_extracted": "knowledge_extracted",
    "persistence_completed": "persistence_completed",
}


class DomainEventBusAgentEventSink:
    """Bridges RuntimeKernel AgentEvents onto the DomainEventBus.

    Each AgentEvent is converted to a LifecycleEvent and published
    on the DomainEventBus so that subscribers (logging, metrics,
    plugins, …) can react without coupling to the runtime kernel.

    Usage:
        bus = DomainEventBus()
        sink = DomainEventBusAgentEventSink(bus)
        kernel = RuntimeKernel(phases, default_event_sink=sink)
    """

    def __init__(self, bus: DomainEventBus) -> None:
        self._bus = bus

    async def emit(self, event: AgentEvent) -> None:
        lifecycle_type = EVENT_TYPE_MAP.get(
            event.type.value,
            event.type.value,
        )

        lifecycle = LifecycleEvent(
            event_id=event.turn_id or event.request_id,
            event_type=lifecycle_type,
            trace_id=event.request_id,
            turn_id=event.turn_id,
            metadata={
                "phase": event.phase,
                **event.data,
            },
        )

        await self._bus.publish(lifecycle)
