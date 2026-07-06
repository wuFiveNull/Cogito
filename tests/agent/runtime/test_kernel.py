# tests/agent/runtime/test_kernel.py

from __future__ import annotations

import asyncio

import pytest

from cogito.agent.runtime.context_factory import TurnContextFactory
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.models import AgentRequest, TurnStatus
from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.events import InMemoryAgentEventSink
from cogito.agent.ports.ids import IdGeneratorPort
from cogito.agent.runtime.cleanup import DefaultRuntimeCleanup
from cogito.agent.runtime.errors import (
    DefaultRuntimeErrorMapper,
    DuplicatePhaseNameError,
    MissingTurnResultError,
    PhaseNotImplementedError,
    RuntimeAgentError,
)
from cogito.agent.runtime.events import AgentEventType
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.phase import RuntimePhase


# ── Helpers ──────────────────────────────────────────────────────────


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


class RecordingPhase:
    """A phase that records its name in order."""

    def __init__(self, name: str, records: list[str]) -> None:
        self.name = name
        self._records = records

    async def run(self, ctx: TurnContext) -> None:
        self._records.append(self.name)


class FailingPhase:
    """A phase that raises an exception."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, ctx: TurnContext) -> None:
        raise PhaseNotImplementedError("phase failed")


class ResultProducingPhase:
    """A phase that sets ctx.output_text and ctx.result."""

    def __init__(self, name: str = "result_producer") -> None:
        self.name = name

    async def run(self, ctx: TurnContext) -> None:
        from cogito.agent.runtime.models import TurnResult, TurnStatus
        from cogito.agent.domain.usage import UsageSummary

        ctx.output_text = "Hello, world!"
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


def make_request(**kwargs: object) -> AgentRequest:
    return AgentRequest(
        request_id=kwargs.get("request_id", "req-1"),
        session_id=kwargs.get("session_id", "session-1"),
        actor_id=kwargs.get("actor_id", "actor-1"),
        text=kwargs.get("text", "test message"),
    )


def build_kernel(
    phases: list[RuntimePhase],
) -> RuntimeKernel:
    return RuntimeKernel(
        phases=phases,
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_execution_order() -> None:
    """Verify phases execute in the provided order."""
    records: list[str] = []
    phases = [
        RecordingPhase("a", records),
        RecordingPhase("b", records),
        RecordingPhase("c", records),
    ]
    # Append a result producer so we don't get MissingTurnResultError
    phases.append(ResultProducingPhase())

    kernel = build_kernel(phases)
    await kernel.run(make_request())

    assert records == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_duplicate_phase_name_raises_at_init() -> None:
    """Kernel must reject duplicate phase names at construction."""
    records: list[str] = []
    phases = [
        RecordingPhase("dup", records),
        RecordingPhase("dup", records),
    ]
    with pytest.raises(DuplicatePhaseNameError):
        build_kernel(phases)


@pytest.mark.asyncio
async def test_missing_turn_result_raises() -> None:
    """If no phase produces a TurnResult, MissingTurnResultError is raised."""
    records: list[str] = []
    phases = [
        RecordingPhase("a", records),
    ]
    kernel = build_kernel(phases)
    with pytest.raises(MissingTurnResultError):
        await kernel.run(make_request())


@pytest.mark.asyncio
async def test_phase_failure_stops_pipeline() -> None:
    """When a phase fails, subsequent phases are not executed."""
    records: list[str] = []
    phases = [
        RecordingPhase("before", records),
        FailingPhase("fail"),
        RecordingPhase("after", records),
    ]
    kernel = build_kernel(phases)

    with pytest.raises(RuntimeAgentError, match="phase failed"):
        await kernel.run(make_request())

    assert records == ["before"], "Phases after failure must not execute"


@pytest.mark.asyncio
async def test_successful_turn_returns_result() -> None:
    """A successful turn returns a valid TurnResult."""
    phases = [ResultProducingPhase()]
    kernel = build_kernel(phases)

    result = await kernel.run(make_request(text="Hi"))

    assert result.status == TurnStatus.COMPLETED
    assert result.text == "Hello, world!"
    assert result.turn_id is not None
    assert result.request_id == "req-1"


@pytest.mark.asyncio
async def test_cleanup_executes_on_success() -> None:
    """Cleanup must execute on a successful path."""
    cleanup_called = False

    class TrackingCleanup:
        async def run(self, ctx: TurnContext) -> None:
            nonlocal cleanup_called
            cleanup_called = True

    phases = [ResultProducingPhase()]
    kernel = RuntimeKernel(
        phases=phases,
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=TrackingCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    await kernel.run(make_request())

    assert cleanup_called, "Cleanup must run on success"


@pytest.mark.asyncio
async def test_cleanup_executes_on_failure() -> None:
    """Cleanup must execute when a phase fails."""
    cleanup_called = False

    class TrackingCleanup:
        async def run(self, ctx: TurnContext) -> None:
            nonlocal cleanup_called
            cleanup_called = True

    phases = [FailingPhase("fail")]
    kernel = RuntimeKernel(
        phases=phases,
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=TrackingCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    with pytest.raises(RuntimeAgentError):
        await kernel.run(make_request())

    assert cleanup_called, "Cleanup must run on failure"


@pytest.mark.asyncio
async def test_cleanup_executes_on_cancellation() -> None:
    """Cleanup must execute when the turn is cancelled."""
    cleanup_called = False

    class TrackingCleanup:
        async def run(self, ctx: TurnContext) -> None:
            nonlocal cleanup_called
            cleanup_called = True

    class CancellingPhase:
        name = "canceller"

        async def run(self, ctx: TurnContext) -> None:
            import asyncio

            raise asyncio.CancelledError()

    phases = [CancellingPhase()]
    kernel = RuntimeKernel(
        phases=phases,
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=TrackingCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    with pytest.raises(asyncio.CancelledError):
        await kernel.run(make_request())

    assert cleanup_called, "Cleanup must run on cancellation"


@pytest.mark.asyncio
async def test_kernel_accepts_extra_phase() -> None:
    """Adding a new phase should not require modifying the kernel."""
    records: list[str] = []
    phases = [
        RecordingPhase("p1", records),
        RecordingPhase("p2", records),
        RecordingPhase("p3", records),
        ResultProducingPhase(),
    ]

    # Insert an extra phase in the middle
    phases.insert(2, RecordingPhase("extra", records))

    kernel = build_kernel(phases)
    await kernel.run(make_request())

    assert records == ["p1", "p2", "extra", "p3"]


@pytest.mark.asyncio
async def test_kernel_does_not_require_eight_phases() -> None:
    """The kernel should work with any number of phases."""
    kernel = build_kernel([ResultProducingPhase()])
    result = await kernel.run(make_request())
    assert result.status == TurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_kernel_with_only_turn_init_and_finalize() -> None:
    """Minimal 2-phase pipeline should work."""
    records: list[str] = []

    kernel = build_kernel([
        RecordingPhase("init", records),
        ResultProducingPhase("finalize"),
    ])
    result = await kernel.run(make_request())
    assert result.status == TurnStatus.COMPLETED
    assert records == ["init"]


# ── Integration: TurnFinalizePhase ─────────────────────────────────────


class PreFinalizePhase:
    """Sets up the preconditions for TurnFinalizePhase."""

    def __init__(self, name: str = "pre_finalize") -> None:
        self.name = name

    async def run(self, ctx: TurnContext) -> None:
        ctx.output_text = "integration answer"
        ctx.status = TurnStatus.RUNNING


class MissingOutputPhase:
    """Leaves output_text as None — triggers FinalizePhase error."""

    def __init__(self, name: str = "missing_output") -> None:
        self.name = name

    async def run(self, ctx: TurnContext) -> None:
        ctx.output_text = None
        ctx.status = TurnStatus.RUNNING


@pytest.mark.asyncio
async def test_finalize_integration_with_real_phase() -> None:
    """Using a real TurnFinalizePhase produces a consistent result."""
    from cogito.agent.runtime.phases import TurnFinalizePhase

    phases = [
        PreFinalizePhase(),
        TurnFinalizePhase(),
    ]
    kernel = build_kernel(phases)
    result = await kernel.run(make_request())

    assert result.status == TurnStatus.COMPLETED
    assert result.text == "integration answer"
    assert result.turn_id is not None


@pytest.mark.asyncio
async def test_finalize_failure_triggers_turn_failed() -> None:
    """When TurnFinalizePhase fails, TURN_FAILED is emitted and cleanup runs."""
    from cogito.agent.runtime.phases import TurnFinalizePhase

    cleanup_called = False

    class TrackingCleanup:
        async def run(self, ctx: TurnContext) -> None:
            nonlocal cleanup_called
            cleanup_called = True

    event_sink = InMemoryAgentEventSink()
    phases = [
        MissingOutputPhase(),
        TurnFinalizePhase(),
    ]
    kernel = RuntimeKernel(
        phases=phases,
        context_factory=_make_factory(),
        default_event_sink=event_sink,
        cleanup=TrackingCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )

    from cogito.agent.runtime.errors import InvalidTurnStateError

    with pytest.raises(InvalidTurnStateError):
        await kernel.run(make_request())

    types = [e.type for e in event_sink.events]

    assert AgentEventType.PHASE_FAILED in types
    assert AgentEventType.TURN_FAILED in types
    assert AgentEventType.TURN_COMPLETED not in types

    # Confirm TurnFinalizePhase itself failed (did not emit PHASE_COMPLETED)
    finalize_completed = [
        e for e in event_sink.events
        if e.type == AgentEventType.PHASE_COMPLETED and e.phase == "turn_finalize"
    ]
    assert len(finalize_completed) == 0
    assert cleanup_called, "Cleanup must run when FinalizePhase fails"


@pytest.mark.asyncio
async def test_finalize_failure_preserves_cleanup_after_cancellation() -> None:
    """Cancellation during FinalizePhase still triggers cleanup."""
    cleanup_called = False

    class CancellingAndMissingPhase:
        name = "bad_phase"

        async def run(self, ctx: TurnContext) -> None:
            import asyncio

            ctx.output_text = None
            raise asyncio.CancelledError()

    class TrackingCleanup:
        async def run(self, ctx: TurnContext) -> None:
            nonlocal cleanup_called
            cleanup_called = True

    phases = [CancellingAndMissingPhase()]
    kernel = RuntimeKernel(
        phases=phases,
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=TrackingCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )

    with pytest.raises(asyncio.CancelledError):
        await kernel.run(make_request())

    assert cleanup_called, "Cleanup must run on cancellation even near Finalize"
