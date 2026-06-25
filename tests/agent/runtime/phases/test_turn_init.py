# tests/agent/runtime/phases/test_turn_init.py

"""Unit tests for TurnInitPhase.

Covers spec Section 14 test checklist:
  - Normal text request
  - Pure attachment request
  - Empty request (no text, no attachments)
  - Empty / blank identifiers
  - Attachment validation errors
  - Context not pre-initialized by TurnContextFactory
  - Context already polluted with residual state
  - Trace startup failure
  - TurnInitConfig validation
  - Timeout config propagation
"""

from __future__ import annotations

from datetime import datetime

import pytest

from cogito.agent.domain.memory import MemoryCandidate
from cogito.agent.domain.messages import ModelMessage
from cogito.agent.domain.preferences import PreferenceCandidate
from cogito.agent.domain.retrieval import RetrievedItem, RetrievedItemKind
from cogito.agent.domain.usage import ToolExecutionRecord, ToolExecutionStatus
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import (
    InvalidAgentRequestError,
    PhaseExecutionError,
)
from cogito.agent.runtime.models import (
    AgentRequest,
    AttachmentRef,
    TurnResult,
    TurnStatus,
)
from cogito.agent.runtime.phases.turn_init import TurnInitConfig, TurnInitPhase


# ── Fakes ─────────────────────────────────────────────────────────────


class FakeTrace:
    """Records calls and returns a fixed trace_id."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    async def start_turn(
        self,
        *,
        turn_id: str,
        request_id: str,
    ) -> str:
        self.calls.append((turn_id, request_id))
        if self._fail:
            raise RuntimeError("trace backend unavailable")
        return "trace-001"


FIXED_TIME = datetime(2026, 6, 24, 12, 0, 0)


# ── Helpers ───────────────────────────────────────────────────────────


def make_context(
    *,
    request: AgentRequest | None = None,
    turn_id: str | None = "turn-001",
    started_at: datetime | None = FIXED_TIME,
    status: TurnStatus = TurnStatus.RUNNING,
    **overrides: object,
) -> TurnContext:
    """Build a TurnContext as it would be after TurnContextFactory."""
    req = request or AgentRequest(
        request_id="req-001",
        session_id="s-001",
        actor_id="a-001",
        text="hello",
    )
    ctx = TurnContext(
        request=req,
        turn_id=turn_id,
        started_at=started_at,
        status=status,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def make_phase(
    *,
    trace: FakeTrace | None = None,
    max_tool_rounds: int = 8,
    timeout_seconds: float | None = None,
) -> TurnInitPhase:
    return TurnInitPhase(
        trace=trace or FakeTrace(),
        config=TurnInitConfig(
            max_tool_rounds=max_tool_rounds,
            timeout_seconds=timeout_seconds,
        ),
    )


# ── Normal path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_normal_text_request() -> None:
    """A valid text request initializes trace and sets limits."""
    trace = FakeTrace()
    phase = make_phase(trace=trace, max_tool_rounds=5)
    ctx = make_context()

    await phase.execute(ctx)

    assert ctx.trace_id == "trace-001"
    assert ctx.max_tool_rounds == 5
    assert trace.calls == [("turn-001", "req-001")]

    # Must not produce any business data
    assert ctx.retrieved_items == []
    assert ctx.model_messages == []
    assert ctx.model_responses == []
    assert ctx.tool_records == []
    assert ctx.preference_candidates == []
    assert ctx.memory_candidates == []
    assert ctx.result is None
    assert ctx.output_text is None
    assert ctx.persistence_completed is False


@pytest.mark.asyncio
async def test_pure_attachment_request() -> None:
    """Request with empty text but valid attachments must pass."""
    trace = FakeTrace()
    phase = make_phase(trace=trace)
    request = AgentRequest(
        request_id="req-002",
        session_id="s-001",
        actor_id="a-001",
        text="",
        attachments=(
            AttachmentRef(
                attachment_id="att-001",
                media_type="image/png",
            ),
        ),
    )
    ctx = make_context(request=request)

    await phase.execute(ctx)

    assert ctx.trace_id == "trace-001"


@pytest.mark.asyncio
async def test_timeout_config_written_to_metadata() -> None:
    """When timeout_seconds is set, it should appear in ctx.metadata."""
    trace = FakeTrace()
    phase = make_phase(trace=trace, timeout_seconds=120.0)
    ctx = make_context()

    await phase.execute(ctx)

    assert ctx.metadata.get("timeout_seconds") == 120.0


@pytest.mark.asyncio
async def test_timeout_config_none_not_in_metadata() -> None:
    """When timeout_seconds is None, metadata should not contain it."""
    trace = FakeTrace()
    phase = make_phase(trace=trace, timeout_seconds=None)
    ctx = make_context()

    await phase.execute(ctx)

    assert "timeout_seconds" not in ctx.metadata


# ── Request validation — identifiers ──────────────────────────────────


@pytest.mark.asyncio
async def test_empty_request_id_raises() -> None:
    """request_id must be non-blank."""
    request = AgentRequest(
        request_id="",
        session_id="s-001",
        actor_id="a-001",
        text="hello",
    )
    phase = make_phase()
    ctx = make_context(request=request)

    with pytest.raises(InvalidAgentRequestError) as excinfo:
        await phase.execute(ctx)

    assert "request_id" in str(excinfo.value)


@pytest.mark.asyncio
async def test_empty_session_id_raises() -> None:
    """session_id must be non-blank."""
    request = AgentRequest(
        request_id="req-001",
        session_id="",
        actor_id="a-001",
        text="hello",
    )
    phase = make_phase()
    ctx = make_context(request=request)

    with pytest.raises(InvalidAgentRequestError) as excinfo:
        await phase.execute(ctx)

    assert "session_id" in str(excinfo.value)


@pytest.mark.asyncio
async def test_empty_actor_id_raises() -> None:
    """actor_id must be non-blank."""
    request = AgentRequest(
        request_id="req-001",
        session_id="s-001",
        actor_id="",
        text="hello",
    )
    phase = make_phase()
    ctx = make_context(request=request)

    with pytest.raises(InvalidAgentRequestError) as excinfo:
        await phase.execute(ctx)

    assert "actor_id" in str(excinfo.value)


@pytest.mark.asyncio
async def test_blank_identifiers_raises() -> None:
    """Whitespace-only identifiers must be rejected."""
    request = AgentRequest(
        request_id="  ",
        session_id="  ",
        actor_id="  ",
        text="hello",
    )
    phase = make_phase()
    ctx = make_context(request=request)

    with pytest.raises(InvalidAgentRequestError):
        await phase.execute(ctx)


# ── Request validation — content ──────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_request_raises() -> None:
    """Request with no text and no attachments must be rejected."""
    request = AgentRequest(
        request_id="req-001",
        session_id="s-001",
        actor_id="a-001",
        text="",
    )
    phase = make_phase()
    ctx = make_context(request=request)

    with pytest.raises(InvalidAgentRequestError) as excinfo:
        await phase.execute(ctx)

    assert "内容" in (excinfo.value.safe_message or "") or "empty" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_whitespace_only_text_without_attachments_raises() -> None:
    """Whitespace-only text with no attachments must be rejected."""
    request = AgentRequest(
        request_id="req-001",
        session_id="s-001",
        actor_id="a-001",
        text="   \t\n  ",
    )
    phase = make_phase()
    ctx = make_context(request=request)

    with pytest.raises(InvalidAgentRequestError):
        await phase.execute(ctx)


# ── Request validation — attachments ──────────────────────────────────


@pytest.mark.asyncio
async def test_empty_attachment_id_raises() -> None:
    """Each attachment must have a non-blank attachment_id."""
    request = AgentRequest(
        request_id="req-001",
        session_id="s-001",
        actor_id="a-001",
        text="hello",
        attachments=(
            AttachmentRef(attachment_id="", media_type="image/png"),
        ),
    )
    phase = make_phase()
    ctx = make_context(request=request)

    with pytest.raises(InvalidAgentRequestError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_empty_media_type_raises() -> None:
    """Each attachment must have a non-blank media_type."""
    request = AgentRequest(
        request_id="req-001",
        session_id="s-001",
        actor_id="a-001",
        text="hello",
        attachments=(
            AttachmentRef(attachment_id="att-001", media_type=""),
        ),
    )
    phase = make_phase()
    ctx = make_context(request=request)

    with pytest.raises(InvalidAgentRequestError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_duplicate_attachment_id_raises() -> None:
    """Duplicate attachment_id within the same request must be rejected."""
    request = AgentRequest(
        request_id="req-001",
        session_id="s-001",
        actor_id="a-001",
        text="hello",
        attachments=(
            AttachmentRef(attachment_id="att-001", media_type="image/png"),
            AttachmentRef(attachment_id="att-001", media_type="image/jpeg"),
        ),
    )
    phase = make_phase()
    ctx = make_context(request=request)

    with pytest.raises(InvalidAgentRequestError) as excinfo:
        await phase.execute(ctx)

    assert "Duplicate" in str(excinfo.value)


# ── Context identity validation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_context_turn_id_none_raises() -> None:
    """PhaseExecutionError when turn_id was not pre-initialized."""
    phase = make_phase()
    ctx = make_context(turn_id=None)

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)

    # Trace must not be called when identity validation fails
    assert phase._trace.calls == []


@pytest.mark.asyncio
async def test_context_started_at_none_raises() -> None:
    """PhaseExecutionError when started_at was not pre-initialized."""
    phase = make_phase()
    ctx = make_context(started_at=None)

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_context_status_not_running_raises() -> None:
    """PhaseExecutionError when status is not RUNNING."""
    phase = make_phase()
    ctx = make_context(status=TurnStatus.CREATED)

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


# ── Context clean state validation ────────────────────────────────────


@pytest.mark.asyncio
async def test_context_with_preexisting_trace_id_raises() -> None:
    """Context with a pre-existing trace_id must be rejected."""
    phase = make_phase()
    ctx = make_context(trace_id="existing-trace")

    with pytest.raises(PhaseExecutionError) as excinfo:
        await phase.execute(ctx)

    assert "重复" in (excinfo.value.safe_message or "")


@pytest.mark.asyncio
async def test_context_with_retrieved_items_raises() -> None:
    """Context with residual retrieved_items must be rejected."""
    phase = make_phase()
    ctx = make_context(
        retrieved_items=[
            RetrievedItem(
                item_id="i-1",
                kind=RetrievedItemKind.DOCUMENT,
                content="test",
                score=1.0,
                source="test",
            ),
        ],
    )

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_context_with_model_messages_raises() -> None:
    """Context with residual model_messages must be rejected."""
    from cogito.agent.domain.messages import UserMessage

    phase = make_phase()
    ctx = make_context(
        model_messages=[
            UserMessage(content="hello"),
        ],
    )

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_context_with_output_text_raises() -> None:
    """Context with pre-existing output_text must be rejected."""
    phase = make_phase()
    ctx = make_context(output_text="stale output")

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_context_with_result_raises() -> None:
    """Context with a pre-existing TurnResult must be rejected."""
    from cogito.agent.domain.usage import UsageSummary

    phase = make_phase()
    ctx = make_context(
        result=TurnResult(
            turn_id="stale",
            request_id="r",
            session_id="s",
            actor_id="a",
            status=TurnStatus.COMPLETED,
            text="done",
            usage=UsageSummary(),
        ),
    )

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_context_with_persistence_completed_raises() -> None:
    """Context with persistence_completed=True must be rejected."""
    phase = make_phase()
    ctx = make_context(persistence_completed=True)

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_context_with_tool_records_raises() -> None:
    """Context with residual tool_records must be rejected."""
    phase = make_phase()
    ctx = make_context(
        tool_records=[
            ToolExecutionRecord(
                call_id="c-1",
                tool_name="test",
                status=ToolExecutionStatus.SUCCEEDED,
            ),
        ],
    )

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_context_with_preference_candidates_raises() -> None:
    """Context with residual preference_candidates must be rejected."""
    from cogito.agent.domain.preferences import CandidateOperation

    phase = make_phase()
    ctx = make_context(
        preference_candidates=[
            PreferenceCandidate(
                key="theme",
                value="dark",
                operation=CandidateOperation.INSERT,
                confidence=0.9,
            ),
        ],
    )

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_context_with_memory_candidates_raises() -> None:
    """Context with residual memory_candidates must be rejected."""
    phase = make_phase()
    ctx = make_context(
        memory_candidates=[
            MemoryCandidate(
                content="test memory",
                confidence=0.8,
                importance=0.5,
            ),
        ],
    )

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)


# ── Trace failure ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_startup_failure_raises_phase_error() -> None:
    """When trace.start_turn() fails, a PhaseExecutionError is raised."""
    trace = FakeTrace(fail=True)
    phase = make_phase(trace=trace)
    ctx = make_context()

    with pytest.raises(PhaseExecutionError):
        await phase.execute(ctx)

    # trace_id must remain None
    assert ctx.trace_id is None


# ── TurnInitConfig validation ─────────────────────────────────────────


def test_turn_init_config_max_tool_rounds_zero_raises() -> None:
    """max_tool_rounds must be greater than zero."""
    with pytest.raises(ValueError, match="max_tool_rounds"):
        TurnInitConfig(max_tool_rounds=0)


def test_turn_init_config_max_tool_rounds_negative_raises() -> None:
    """Negative max_tool_rounds must be rejected."""
    with pytest.raises(ValueError, match="max_tool_rounds"):
        TurnInitConfig(max_tool_rounds=-1)


def test_turn_init_config_timeout_zero_raises() -> None:
    """timeout_seconds of zero must be rejected."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        TurnInitConfig(timeout_seconds=0.0)


def test_turn_init_config_timeout_negative_raises() -> None:
    """Negative timeout_seconds must be rejected."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        TurnInitConfig(timeout_seconds=-1.0)


def test_turn_init_config_defaults() -> None:
    """Default values should be sensible."""
    config = TurnInitConfig()
    assert config.max_tool_rounds == 8
    assert config.timeout_seconds is None


def test_turn_init_config_custom_values() -> None:
    """Custom values should be accepted."""
    config = TurnInitConfig(max_tool_rounds=16, timeout_seconds=300.0)
    assert config.max_tool_rounds == 16
    assert config.timeout_seconds == 300.0
