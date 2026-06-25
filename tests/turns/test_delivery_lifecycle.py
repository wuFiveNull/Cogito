# tests/turns/test_delivery_lifecycle.py

"""验证 TurnRunner → DeliveryManager 的出站事件进入了 DomainEventBus。"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from cogito.agent.application.agent_service import AgentApplicationService
from cogito.agent.domain.usage import UsageSummary
from cogito.agent.runtime.cleanup import DefaultRuntimeCleanup
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.context_factory import TurnContextFactory
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.models import TurnResult, TurnStatus
from cogito.agent.ports.events import NullAgentEventSink
from cogito.bus.event_bus import DomainEventBus
from cogito.bus.events import (
    InboundMessage,
    MessagePayload,
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


# ── Stub Phase ──────────────────────────────────────────────────────


class EchoPhase:
    """A simple phase that echoes the request."""

    name = "echo"

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


# ── Stub Channel ────────────────────────────────────────────────────


class CapturingChannel:
    """Channel that records sent messages."""

    def __init__(self, name: str = "cli") -> None:
        self.name = name

    async def run(self, inbound) -> None:
        pass

    async def send(self, request):
        from cogito.bus.events import DeliveryReceipt

        return DeliveryReceipt(
            outbound_id=request.outbound_id,
            status="delivered",
        )

    async def close(self) -> None:
        pass


# ── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delivery_emits_lifecycle_events() -> None:
    """DeliveryManager should emit outbound_accepted → delivery_started → delivery_succeeded."""
    bus = DomainEventBus()
    registry = ChannelRegistry()
    registry.register(CapturingChannel("cli"))

    delivery = DeliveryManager(registry, domain_bus=bus)
    kernel = RuntimeKernel(
        phases=[EchoPhase()],
        context_factory=_make_factory(),
        default_event_sink=NullAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
    )
    service = AgentApplicationService(kernel)

    # Collect lifecycle events from the bus
    received: list = []

    def collector(event):
        received.append(event)
        return None

    bus.on("outbound_accepted", collector)
    bus.on("delivery_started", collector)
    bus.on("delivery_succeeded", collector)

    runner = TurnRunner(
        service=service,
        delivery=delivery,
        domain_bus=bus,
    )

    msg = InboundMessage(
        message_id="msg-lc-001",
        external_message_id=None,
        session_key="test:session:1",
        channel="cli",
        target="user-1",
        payload=MessagePayload(parts=[TextPart(text="Hello")]),
        trace_id="trace-lc-001",
        received_at=datetime.now(),
    )

    await runner.run(msg)

    # Should have received all three delivery lifecycle events
    event_types = [e.event_type for e in received]
    assert "outbound_accepted" in event_types, "Missing outbound_accepted"
    assert "delivery_started" in event_types, "Missing delivery_started"
    assert "delivery_succeeded" in event_types, "Missing delivery_succeeded"

    # Verify order
    assert event_types == [
        "outbound_accepted",
        "delivery_started",
        "delivery_succeeded",
    ], f"Unexpected event order: {event_types}"
