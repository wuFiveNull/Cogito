# tests/agent/runtime/test_cleanup.py

from __future__ import annotations

import asyncio

import pytest

from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.context_factory import TurnContextFactory
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.models import AgentRequest
from cogito.agent.ports.events import InMemoryAgentEventSink
from cogito.agent.runtime.errors import (
    DefaultRuntimeErrorMapper,
    RuntimeAgentError,
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
    name = "result"

    async def run(self, ctx: TurnContext) -> None:
        from cogito.agent.runtime.models import TurnResult, TurnStatus
        from cogito.agent.domain.usage import UsageSummary

        ctx.output_text = "ok"
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


class FailingPhase:
    name = "fail"

    async def run(self, ctx: TurnContext) -> None:
        msg = "intentional failure"
        raise RuntimeAgentError(msg)


class CancellingPhase:
    name = "cancel"

    async def run(self, ctx: TurnContext) -> None:
        raise asyncio.CancelledError()


def make_request() -> AgentRequest:
    return AgentRequest(
        request_id="req-cl-1",
        session_id="s-1",
        actor_id="a-1",
        text="cleanup test",
    )


@pytest.mark.asyncio
async def test_cleanup_executes_after_success() -> None:
    """Cleanup runs after successful completion."""
    flags: list[str] = []

    class TrackingCleanup:
        async def run(self, ctx: TurnContext) -> None:
            flags.append("cleanup")

    kernel = RuntimeKernel(
        phases=[ResultPhase()],
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=TrackingCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    await kernel.run(make_request())

    assert flags == ["cleanup"]


@pytest.mark.asyncio
async def test_cleanup_executes_after_phase_failure() -> None:
    """Cleanup runs after a phase failure."""
    flags: list[str] = []

    class TrackingCleanup:
        async def run(self, ctx: TurnContext) -> None:
            flags.append("cleanup")

    kernel = RuntimeKernel(
        phases=[FailingPhase()],
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=TrackingCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    with pytest.raises(RuntimeAgentError):
        await kernel.run(make_request())

    assert flags == ["cleanup"]


@pytest.mark.asyncio
async def test_cleanup_executes_after_cancellation() -> None:
    """Cleanup runs when the turn is cancelled."""
    flags: list[str] = []

    class TrackingCleanup:
        async def run(self, ctx: TurnContext) -> None:
            flags.append("cleanup")

    kernel = RuntimeKernel(
        phases=[CancellingPhase()],
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=TrackingCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    with pytest.raises(asyncio.CancelledError):
        await kernel.run(make_request())

    assert flags == ["cleanup"]


@pytest.mark.asyncio
async def test_cleanup_does_not_swallow_original_error() -> None:
    """If cleanup itself fails, the original error is not masked."""

    class FailingCleanup:
        async def run(self, ctx: TurnContext) -> None:
            raise RuntimeError("cleanup failure")

    kernel = RuntimeKernel(
        phases=[FailingPhase()],
        context_factory=_make_factory(),
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=FailingCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    # The original RuntimeAgentError should still propagate
    with pytest.raises(RuntimeAgentError) as excinfo:
        await kernel.run(make_request())

    assert "intentional failure" in str(excinfo.value)


@pytest.mark.asyncio
async def test_cleanup_sets_completed_at() -> None:
    """Default cleanup sets completed_at on the context."""
    kernel = RuntimeKernel(
        phases=[ResultPhase()],
        context_factory=_make_factory(),
        cleanup=None,
        error_mapper=DefaultRuntimeErrorMapper(),
    )
    result = await kernel.run(make_request())
    assert result is not None


@pytest.mark.asyncio
async def test_cleanup_is_idempotent() -> None:
    """Cleanup should be safe to call multiple times."""
    from cogito.agent.runtime.cleanup import DefaultRuntimeCleanup

    cleanup = DefaultRuntimeCleanup()
    ctx = TurnContext(request=make_request())

    # Calling cleanup multiple times should not raise
    await cleanup.run(ctx)
    await cleanup.run(ctx)
    await cleanup.run(ctx)

    assert ctx.completed_at is not None
