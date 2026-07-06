# tests/turns/conftest.py

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from cogito.agent.application.agent_service import AgentApplicationService
from cogito.agent.runtime.cleanup import DefaultRuntimeCleanup
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.context_factory import TurnContextFactory
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.models import AgentRequest, TurnResult, TurnStatus
from cogito.agent.domain.usage import UsageSummary
from cogito.agent.ports.events import NullAgentEventSink
from cogito.bus.events import (
    InboundMessage,
    MessagePayload,
    OutboundRequest,
    TextPart,
)
from cogito.channels.registry import ChannelRegistry
from cogito.delivery.manager import DeliveryManager
from cogito.turns.runner import TurnRunner


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


# ── Stub Channel for testing ─────────────────────────────────────────


class StubChannel:
    """A test channel that records sent outbound requests."""

    def __init__(self, name: str = "test") -> None:
        self.name = name
        self.sent: list[OutboundRequest] = []

    async def run(self, inbound) -> None:
        pass

    async def send(self, request: OutboundRequest):
        self.sent.append(request)
        from cogito.bus.events import DeliveryReceipt

        return DeliveryReceipt(
            outbound_id=request.outbound_id,
            status="delivered",
        )

    async def close(self) -> None:
        pass


# ── Stub Phase for testing the bridge ────────────────────────────────

class StubAgentPhase:
    """A phase that produces a canned response — for testing the bridge."""

    name = "respond"

    async def run(self, ctx: TurnContext) -> None:
        ctx.output_text = f"Echo: {ctx.request.text}"
        ctx.result = TurnResult(
            turn_id=ctx.turn_id or f"turn_{uuid4().hex[:12]}",
            request_id=ctx.request.request_id,
            session_id=ctx.request.session_id,
            actor_id=ctx.request.actor_id,
            status=TurnStatus.COMPLETED,
            text=ctx.output_text,
            usage=UsageSummary(model_calls=1),
        )


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def channel_registry() -> ChannelRegistry:
    registry = ChannelRegistry()
    registry.register(StubChannel("cli"))
    return registry


@pytest.fixture
def delivery_manager(channel_registry) -> DeliveryManager:
    return DeliveryManager(channel_registry)


@pytest.fixture
def stub_kernel() -> RuntimeKernel:
    return RuntimeKernel(
        phases=[
            StubAgentPhase(),
        ],
        context_factory=_make_factory(),
        default_event_sink=NullAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
    )


@pytest.fixture
def agent_service(stub_kernel) -> AgentApplicationService:
    return AgentApplicationService(stub_kernel)


@pytest.fixture
def turn_runner(
    agent_service,
    delivery_manager,
) -> TurnRunner:
    return TurnRunner(
        service=agent_service,
        delivery=delivery_manager,
        domain_bus=None,
    )


@pytest.fixture
def inbound_message() -> InboundMessage:
    return InboundMessage(
        message_id="msg-001",
        external_message_id="ext-001",
        session_key="test:user:default",
        channel="cli",
        target="user-1",
        payload=MessagePayload(
            parts=[TextPart(text="Hello, agent!")],
        ),
        trace_id="trace-001",
        received_at=datetime.now(),
    )
