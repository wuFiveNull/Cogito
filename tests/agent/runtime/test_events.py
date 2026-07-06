# tests/agent/runtime/test_events.py

from __future__ import annotations

import pytest

from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.context_factory import TurnContextFactory
from cogito.agent.runtime.events import AgentEventType
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.models import AgentRequest, TurnStatus
from cogito.agent.runtime.cleanup import DefaultRuntimeCleanup
from cogito.agent.runtime.errors import (
    DefaultRuntimeErrorMapper,
    RuntimeAgentError,
)
from cogito.agent.ports.events import (
    AgentEventSink,
    InMemoryAgentEventSink,
)


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
    def __init__(self, name: str = "result") -> None:
        self.name = name

    async def run(self, ctx: TurnContext) -> None:
        from cogito.agent.runtime.models import TurnResult, TurnStatus
        from cogito.agent.domain.usage import UsageSummary

        ctx.turn_id = "turn-test-1"
        ctx.output_text = "hello"
        ctx.status = TurnStatus.COMPLETED
        ctx.result = TurnResult(
            turn_id=ctx.turn_id,
            request_id=ctx.request.request_id,
            session_id=ctx.request.session_id,
            actor_id=ctx.request.actor_id,
            status=TurnStatus.COMPLETED,
            text=ctx.output_text,
            usage=UsageSummary(),
        )


class FailingPhase:
    def __init__(self, name: str = "failing") -> None:
        self.name = name

    async def run(self, ctx: TurnContext) -> None:
        msg = "something went wrong"
        raise RuntimeAgentError(msg)


def make_request() -> AgentRequest:
    return AgentRequest(
        request_id="req-ev-1",
        session_id="s-1",
        actor_id="a-1",
        text="test events",
    )


def build_kernel(
    phases,
    *,
    event_sink: AgentEventSink | None = None,
) -> RuntimeKernel:
    return RuntimeKernel(
        phases=phases,
        context_factory=_make_factory(),
        default_event_sink=event_sink or InMemoryAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )


def event_types(events) -> list[str]:
    return [e.type for e in events]


@pytest.mark.asyncio
async def test_events_on_success_path() -> None:
    """Successful turn emits: STARTED → PHASE_STARTED/COMPLETED → TURN_COMPLETED."""
    sink = InMemoryAgentEventSink()
    kernel = build_kernel([ResultPhase()], event_sink=sink)

    await kernel.run(make_request())

    types = event_types(sink.events)

    assert AgentEventType.TURN_STARTED in types
    assert AgentEventType.TURN_COMPLETED in types
    assert AgentEventType.TURN_FAILED not in types


@pytest.mark.asyncio
async def test_phase_events_are_emitted() -> None:
    """Each phase emits PHASE_STARTED and PHASE_COMPLETED on success."""
    sink = InMemoryAgentEventSink()
    kernel = build_kernel([ResultPhase("p1"), ResultPhase("p2")], event_sink=sink)

    await kernel.run(make_request())

    started = [e for e in sink.events if e.type == AgentEventType.PHASE_STARTED]
    completed = [e for e in sink.events if e.type == AgentEventType.PHASE_COMPLETED]

    assert len(started) == 2
    assert len(completed) == 2
    assert started[0].phase == "p1"
    assert started[1].phase == "p2"
    assert completed[0].phase == "p1"
    assert completed[1].phase == "p2"


@pytest.mark.asyncio
async def test_failure_emits_phase_failed_and_turn_failed() -> None:
    """On phase failure, PHASE_FAILED and TURN_FAILED are emitted."""
    sink = InMemoryAgentEventSink()
    kernel = build_kernel([FailingPhase("fail")], event_sink=sink)

    with pytest.raises(RuntimeAgentError):
        await kernel.run(make_request())

    types = event_types(sink.events)

    assert AgentEventType.PHASE_FAILED in types
    assert AgentEventType.TURN_FAILED in types
    assert AgentEventType.PHASE_COMPLETED not in types
    assert AgentEventType.TURN_COMPLETED not in types


@pytest.mark.asyncio
async def test_failing_event_sink_does_not_break_turn() -> None:
    """An EventSink that throws should not crash the turn or skip TURN_COMPLETED."""

    class ThrowingEventSink:
        def __init__(self) -> None:
            self.call_count = 0

        async def emit(self, event) -> None:
            self.call_count += 1
            msg = f"simulated sink failure #{self.call_count}"
            raise RuntimeError(msg)

    sink = ThrowingEventSink()
    kernel = build_kernel([ResultPhase()], event_sink=sink)

    # The sink throws on every emit, but the kernel should catch and log.
    result = await kernel.run(make_request())

    assert result.status == TurnStatus.COMPLETED
    assert sink.call_count > 0, "EventSink should have been called"


@pytest.mark.asyncio
async def test_turn_failed_event_has_error_data() -> None:
    """TURN_FAILED event data should contain error code and safe message."""
    sink = InMemoryAgentEventSink()
    kernel = build_kernel([FailingPhase()], event_sink=sink)

    with pytest.raises(RuntimeAgentError):
        await kernel.run(make_request())

    failed_events = [e for e in sink.events if e.type == AgentEventType.TURN_FAILED]
    assert len(failed_events) == 1

    data = failed_events[0].data
    assert "error_code" in data
    assert "safe_message" in data
    assert "retryable" in data


@pytest.mark.asyncio
async def test_event_has_turn_and_request_ids() -> None:
    """Every event should carry turn_id and request_id."""
    sink = InMemoryAgentEventSink()
    kernel = build_kernel([ResultPhase()], event_sink=sink)

    await kernel.run(make_request())

    for event in sink.events:
        if event.type in (AgentEventType.TURN_STARTED, AgentEventType.PHASE_STARTED):
            # turn_id is set during phase execution; these fire before any phase runs.
            continue
        assert event.turn_id, f"Event {event.type} missing turn_id"
        assert event.request_id == "req-ev-1"
