# cogito/agent/application/agent_service.py

from __future__ import annotations

from cogito.agent.ports.events import AgentEventSink
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.models import AgentRequest, TurnResult


class AgentApplicationService:
    """Application-level entry point for agent turn processing.

    This service sits between the MessageBus worker and the kernel.
    It is still MessageBus-agnostic; callers are responsible for
    adapting their specific transport.
    """

    def __init__(self, kernel: RuntimeKernel) -> None:
        self._kernel = kernel

    async def process(
        self,
        request: AgentRequest,
        *,
        event_sink: AgentEventSink | None = None,
    ) -> TurnResult:
        return await self._kernel.run(
            request,
            event_sink=event_sink,
        )
