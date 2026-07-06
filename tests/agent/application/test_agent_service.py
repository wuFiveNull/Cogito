# tests/agent/application/test_agent_service.py

from __future__ import annotations

import pytest

from cogito.agent.application.agent_service import AgentApplicationService
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.context_factory import TurnContextFactory
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.models import AgentRequest, TurnStatus
from cogito.agent.ports.events import InMemoryAgentEventSink
from cogito.agent.runtime.cleanup import DefaultRuntimeCleanup
from cogito.agent.runtime.errors import DefaultRuntimeErrorMapper


class _SimpleClock:
    def now(self):
        from datetime import datetime

        return datetime.now()


class _SimpleIdGenerator:
    def new_id(self) -> str:
        from uuid import uuid4

        return f"turn_{uuid4().hex[:12]}"


def _make_factory() -> TurnContextFactory:
    return TurnContextFactory(clock=_SimpleClock(), id_generator=_SimpleIdGenerator())


class ResultPhase:
    name = "result"

    async def run(self, ctx: TurnContext) -> None:
        from cogito.agent.runtime.models import TurnResult, TurnStatus
        from cogito.agent.domain.usage import UsageSummary

        ctx.output_text = "service response"
        ctx.status = TurnStatus.COMPLETED
        ctx.result = TurnResult(
            turn_id=ctx.turn_id or "",
            request_id=ctx.request.request_id,
            session_id=ctx.request.session_id,
            actor_id=ctx.request.actor_id,
            status=TurnStatus.COMPLETED,
            text=ctx.output_text,
            usage=UsageSummary(),
        )


def make_request() -> AgentRequest:
    return AgentRequest(
        request_id="req-svc-1",
        session_id="s-1",
        actor_id="a-1",
        text="test service",
    )


@pytest.mark.asyncio
async def test_service_delegates_to_kernel() -> None:
    """AgentApplicationService.process() should delegate to Kernel.run()."""
    kernel = RuntimeKernel(
        phases=[ResultPhase()],
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    service = AgentApplicationService(kernel)

    result = await service.process(make_request())

    assert result.status == TurnStatus.COMPLETED
    assert result.text == "service response"
    assert result.turn_id is not None


@pytest.mark.asyncio
async def test_service_forwards_event_sink() -> None:
    """The event_sink parameter should be forwarded to the kernel."""
    sink = InMemoryAgentEventSink()
    kernel = RuntimeKernel(
        phases=[ResultPhase()],
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    service = AgentApplicationService(kernel)

    await service.process(make_request(), event_sink=sink)

    # Events should be collected in the provided sink
    assert len(sink.events) > 0
