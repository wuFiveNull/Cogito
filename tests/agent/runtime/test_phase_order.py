# tests/agent/runtime/test_phase_order.py

from __future__ import annotations

import pytest

from cogito.agent.bootstrap.runtime_factory import build_runtime_kernel, build_test_kernel
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.models import AgentRequest
from cogito.agent.runtime.phase import RuntimePhase
from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.ids import IdGeneratorPort
from cogito.agent.ports.events import InMemoryAgentEventSink


class FixedClock:
    def __init__(self, now) -> None:
        self._now = now

    def now(self):
        return self._now


class FixedIdGenerator:
    def __init__(self) -> None:
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"


class RecordingPhase:
    def __init__(self, name: str, records: list[str]) -> None:
        self.name = name
        self._records = records

    async def run(self, ctx: TurnContext) -> None:
        self._records.append(self.name)


class ResultProducer:
    def __init__(self, name: str = "result_producer") -> None:
        self.name = name

    async def run(self, ctx: TurnContext) -> None:
        from cogito.agent.runtime.models import TurnResult, TurnStatus
        from cogito.agent.domain.usage import UsageSummary

        ctx.output_text = "done"
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


@pytest.mark.asyncio
async def test_default_phase_order() -> None:
    """Verify the standard 8-phase pipeline order."""
    from datetime import datetime

    clock = FixedClock(datetime(2026, 6, 24, 12, 0, 0))
    id_gen = FixedIdGenerator()

    kernel = build_runtime_kernel(
        clock=clock,
        id_generator=id_gen,
        event_sink=InMemoryAgentEventSink(),
    )

    # The default kernel has real phase instances; we just verify
    # they are in the right order by checking the phase list.
    expected = [
        "turn_init",
        "state_load",
        "information_retrieval",
        "context_assembly",
        "agent_loop",
        "knowledge_extraction",
        "persistence",
        "turn_finalize",
    ]

    actual = [p.name for p in kernel._phases]
    assert actual == expected, f"Phase order mismatch: {actual} != {expected}"


@pytest.mark.asyncio
async def test_custom_phase_works_with_test_kernel() -> None:
    """The test kernel should accept a custom phase."""
    records: list[str] = []
    phases: list[RuntimePhase] = [
        RecordingPhase("custom_a", records),
        RecordingPhase("custom_b", records),
        ResultProducer(),
    ]

    kernel = build_test_kernel(phases)
    request = AgentRequest(
        request_id="test-1",
        session_id="s-1",
        actor_id="a-1",
        text="test",
    )
    result = await kernel.run(request)

    assert result.status.value == "completed"
    assert records == ["custom_a", "custom_b"]


@pytest.mark.asyncio
async def test_custom_phase_insertion() -> None:
    """Phases can be inserted into the pipeline without kernel changes."""
    records: list[str] = []
    phases: list[RuntimePhase] = [
        RecordingPhase("a", records),
        RecordingPhase("c", records),
        ResultProducer(),
    ]
    phases.insert(1, RecordingPhase("b", records))

    kernel = build_test_kernel(phases)
    await kernel.run(
        AgentRequest(
            request_id="r",
            session_id="s",
            actor_id="a",
            text="t",
        ),
    )

    assert records == ["a", "b", "c"]
