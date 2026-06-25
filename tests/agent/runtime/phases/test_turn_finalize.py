# tests/agent/runtime/phases/test_turn_finalize.py

from __future__ import annotations

import pytest

from cogito.agent.domain.usage import ToolExecutionRecord, ToolExecutionStatus, UsageSummary
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import InvalidTurnStateError
from cogito.agent.runtime.models import AgentRequest, TurnResult, TurnStatus
from cogito.agent.runtime.phases import TurnFinalizePhase


# ── Fixture ────────────────────────────────────────────────────────────


def make_context() -> TurnContext:
    ctx = TurnContext(
        request=AgentRequest(
            request_id="request-1",
            session_id="session-1",
            actor_id="actor-1",
            text="hello",
        ),
    )
    ctx.turn_id = "turn-1"
    ctx.status = TurnStatus.RUNNING
    ctx.output_text = "final answer"
    ctx.usage = UsageSummary(
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
        model_calls=2,
        tool_calls=1,
    )
    ctx.tool_records.append(
        ToolExecutionRecord(
            call_id="call-1",
            tool_name="search",
            status=ToolExecutionStatus.SUCCEEDED,
            duration_ms=15,
        ),
    )
    return ctx


# ── Happy path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_builds_turn_result() -> None:
    """Normal execution builds a complete TurnResult."""
    ctx = make_context()
    phase = TurnFinalizePhase()

    await phase.run(ctx)

    assert ctx.status is TurnStatus.COMPLETED
    assert ctx.result is not None
    assert ctx.result.turn_id == "turn-1"
    assert ctx.result.request_id == "request-1"
    assert ctx.result.session_id == "session-1"
    assert ctx.result.actor_id == "actor-1"
    assert ctx.result.text == "final answer"
    assert ctx.result.status is TurnStatus.COMPLETED
    assert ctx.result.usage.total_tokens == 30
    assert len(ctx.result.tool_records) == 1


# ── Precondition: turn_id ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_rejects_missing_turn_id() -> None:
    """TurnFinalizePhase raises when turn_id is None."""
    ctx = make_context()
    ctx.turn_id = None

    with pytest.raises(InvalidTurnStateError) as excinfo:
        await TurnFinalizePhase().run(ctx)

    assert "turn_id" in str(excinfo.value)


@pytest.mark.asyncio
async def test_finalize_rejects_empty_turn_id() -> None:
    """TurnFinalizePhase raises when turn_id is blank."""
    ctx = make_context()
    ctx.turn_id = "   "

    with pytest.raises(InvalidTurnStateError):
        await TurnFinalizePhase().run(ctx)


# ── Precondition: output_text ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_rejects_missing_output_text() -> None:
    """TurnFinalizePhase raises when output_text is None."""
    ctx = make_context()
    ctx.output_text = None

    with pytest.raises(InvalidTurnStateError) as excinfo:
        await TurnFinalizePhase().run(ctx)

    assert "output_text" in str(excinfo.value)


@pytest.mark.asyncio
async def test_finalize_preserves_empty_output_text() -> None:
    """An empty string is a legitimate output — not treated as missing."""
    ctx = make_context()
    ctx.output_text = ""

    await TurnFinalizePhase().run(ctx)

    assert ctx.result is not None
    assert ctx.result.text == ""


# ── Tool records snapshot ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_records_are_snapshotted() -> None:
    """Modifying ctx.tool_records after finalization does not affect the result."""
    ctx = make_context()

    await TurnFinalizePhase().run(ctx)
    assert ctx.result is not None

    ctx.tool_records.clear()

    assert len(ctx.result.tool_records) == 1
    assert ctx.result.tool_records[0].call_id == "call-1"


# ── Metadata allowlist ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_result_metadata_uses_allowlist() -> None:
    """Only explicitly allowed keys are projected into TurnResult.metadata."""
    ctx = make_context()
    ctx.metadata.update({
        "finish_reason": "stop",
        "system_prompt": "secret",
        "adapter_object": object(),
    })

    await TurnFinalizePhase().run(ctx)

    assert ctx.result is not None
    assert ctx.result.metadata == {"finish_reason": "stop"}


@pytest.mark.asyncio
async def test_result_metadata_empty_when_no_allowed_keys() -> None:
    """If no metadata keys match the allowlist, metadata is an empty dict."""
    ctx = make_context()
    ctx.metadata.update({
        "system_prompt": "secret",
        "extra_debug": "info",
    })

    await TurnFinalizePhase().run(ctx)

    assert ctx.result is not None
    assert ctx.result.metadata == {}


# ── Idempotent re-entry ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_is_idempotent_for_same_context() -> None:
    """Running finalize twice on the same (unchanged) context is a no-op."""
    ctx = make_context()
    phase = TurnFinalizePhase()

    await phase.run(ctx)
    first = ctx.result

    await phase.run(ctx)

    assert ctx.result is first


@pytest.mark.asyncio
async def test_finalize_rejects_conflicting_existing_result() -> None:
    """If ctx is modified after first finalization, a second run is rejected."""
    ctx = make_context()
    phase = TurnFinalizePhase()

    await phase.run(ctx)
    ctx.output_text = "changed after finalization"

    with pytest.raises(InvalidTurnStateError) as excinfo:
        await phase.run(ctx)

    assert "conflict" in str(excinfo.value).lower()


# ── Status convergence ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_sets_status_to_completed() -> None:
    """ctx.status and ctx.result.status are both COMPLETED after finalization."""
    ctx = make_context()

    await TurnFinalizePhase().run(ctx)

    assert ctx.status is TurnStatus.COMPLETED
    assert ctx.result is not None
    assert ctx.result.status is TurnStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        TurnStatus.CREATED,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
    ],
)
async def test_finalize_rejects_invalid_status(status: TurnStatus) -> None:
    """Only RUNNING (or already COMPLETED) may be finalized normally."""
    ctx = make_context()
    ctx.status = status

    with pytest.raises(InvalidTurnStateError):
        await TurnFinalizePhase().run(ctx)


@pytest.mark.asyncio
async def test_finalize_accepts_already_completed_status() -> None:
    """Already COMPLETED context is accepted (idempotent re-entry)."""
    ctx = make_context()
    ctx.status = TurnStatus.COMPLETED

    await TurnFinalizePhase().run(ctx)

    assert ctx.status is TurnStatus.COMPLETED
    assert ctx.result is not None


@pytest.mark.asyncio
async def test_finalize_rejects_context_with_error() -> None:
    """If ctx.error is set, finalize refuses to produce a normal result."""
    ctx = make_context()
    ctx.error = RuntimeError("something broke")

    with pytest.raises(InvalidTurnStateError):
        await TurnFinalizePhase().run(ctx)
