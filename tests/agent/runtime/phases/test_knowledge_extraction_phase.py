# Tests for KnowledgeExtractionPhase

from __future__ import annotations

from datetime import datetime

import pytest

from cogito.agent.domain.knowledge.enums import ExtractionRunStatus
from cogito.agent.domain.knowledge.extraction import (
    ExtractionDiagnostics,
    KnowledgeExtractionResult,
)
from cogito.agent.domain.memory import MemoryCandidate, SummaryCandidate
from cogito.agent.domain.messages import AssistantMessage
from cogito.agent.domain.preferences import PreferenceCandidate
from cogito.agent.domain.state import SessionSummary
from cogito.agent.ports.knowledge_extraction import (
    StubRuntimeEventEmitter,
)
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import (
    KnowledgeExtractionInvariantError,
    RecoverableKnowledgeExtractionError,
)
from cogito.agent.runtime.extraction.service import KnowledgeExtractionService
from cogito.agent.runtime.models import AgentRequest, TurnStatus
from cogito.agent.runtime.phases import KnowledgeExtractionPhase


# ── Factory helpers ────────────────────────────────────────────────────


def _make_context(**overrides: object) -> TurnContext:
    """Build a minimal TurnContext with sensible defaults."""
    ctx = TurnContext(
        request=AgentRequest(
            request_id="req-001",
            session_id="sess-001",
            actor_id="actor-001",
            text="记住我的昵称是 hunriiz",
        ),
        turn_id="turn-001",
        status=TurnStatus.RUNNING,
        output_text="好的，我记住了，你的昵称是 hunriiz。",
        final_response=AssistantMessage(content="好的，我记住了，你的昵称是 hunriiz。"),
        started_at=datetime(2026, 6, 25, 12, 0, 0),
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


# ── Fake service that produces a known result ──────────────────────────


class _FakeKnowledgeExtractionService:
    """Returns a fixed set of candidates for testing."""

    def __init__(self, fail: bool = False, degraded: bool = False) -> None:
        self._fail = fail
        self._degraded = degraded
        self.called = False
        self.last_ctx: object = None

    async def extract(self, ctx: TurnContext) -> KnowledgeExtractionResult:
        self.called = True
        self.last_ctx = ctx

        if self._fail:
            msg = "Something went wrong"
            raise RuntimeError(msg)

        if self._degraded:
            raise RecoverableKnowledgeExtractionError(
                "extractor timed out",
                safe_message="知识抽取超时",
            )

        return KnowledgeExtractionResult(
            status=ExtractionRunStatus.SUCCEEDED,
            preference_candidates=(
                PreferenceCandidate(
                    key="response.language",
                    operation="insert",
                    confidence=0.95,
                    candidate_id="kc_abc123",
                    value="zh-CN",
                ),
            ),
            memory_candidates=(),
            summary_candidate=SummaryCandidate(
                content="New knowledge extracted from this turn",
                confidence=0.85,
                candidate_id="kc_sum_001",
                source_refs=("turn-001",),
            ),
            dropped_count=0,
            diagnostics=ExtractionDiagnostics(
                duration_ms=10,
                model_calls=0,
                rule_candidate_count=1,
                accepted_count=2,
            ),
        )


# ── Tests ──────────────────────────────────────────────────────────────


class TestKnowledgeExtractionPhase:
    """Test suite for KnowledgeExtractionPhase."""

    async def test_normal_result_writes_to_context(self) -> None:
        fake_service = _FakeKnowledgeExtractionService()
        emitter = StubRuntimeEventEmitter()
        phase = KnowledgeExtractionPhase(service=fake_service, event_emitter=emitter)  # type: ignore[arg-type]

        ctx = _make_context()
        await phase.run(ctx)

        assert fake_service.called
        assert len(ctx.preference_candidates) == 1
        assert ctx.preference_candidates[0].key == "response.language"
        assert ctx.summary_candidate is not None
        assert ctx.knowledge_extraction_result is not None
        assert ctx.knowledge_extraction_result.status == ExtractionRunStatus.SUCCEEDED
        assert len(emitter.calls) == 1

    async def test_skip_waiting_approval(self) -> None:
        fake_service = _FakeKnowledgeExtractionService()
        emitter = StubRuntimeEventEmitter()
        phase = KnowledgeExtractionPhase(service=fake_service, event_emitter=emitter)  # type: ignore[arg-type]

        ctx = _make_context(status=TurnStatus.WAITING_APPROVAL)
        await phase.run(ctx)

        assert fake_service.called is False

    async def test_skip_no_final_response(self) -> None:
        fake_service = _FakeKnowledgeExtractionService()
        emitter = StubRuntimeEventEmitter()
        phase = KnowledgeExtractionPhase(service=fake_service, event_emitter=emitter)  # type: ignore[arg-type]

        ctx = _make_context()
        ctx.final_response = None
        await phase.run(ctx)

        assert fake_service.called is False

    async def test_missing_turn_id_raises(self) -> None:
        fake_service = _FakeKnowledgeExtractionService()
        emitter = StubRuntimeEventEmitter()
        phase = KnowledgeExtractionPhase(service=fake_service, event_emitter=emitter)  # type: ignore[arg-type]

        ctx = _make_context()
        ctx.turn_id = None

        with pytest.raises(KnowledgeExtractionInvariantError):
            await phase.run(ctx)

    async def test_missing_output_text_raises(self) -> None:
        fake_service = _FakeKnowledgeExtractionService()
        emitter = StubRuntimeEventEmitter()
        phase = KnowledgeExtractionPhase(service=fake_service, event_emitter=emitter)  # type: ignore[arg-type]

        ctx = _make_context()
        ctx.output_text = None

        with pytest.raises(KnowledgeExtractionInvariantError):
            await phase.run(ctx)

    async def test_programming_error_not_swallowed(self) -> None:
        fake_service = _FakeKnowledgeExtractionService(fail=True)
        emitter = StubRuntimeEventEmitter()
        phase = KnowledgeExtractionPhase(service=fake_service, event_emitter=emitter)  # type: ignore[arg-type]

        ctx = _make_context()
        with pytest.raises(RuntimeError):
            await phase.run(ctx)

    async def test_degraded_on_recoverable_error(self) -> None:
        fake_service = _FakeKnowledgeExtractionService(degraded=True)
        emitter = StubRuntimeEventEmitter()
        phase = KnowledgeExtractionPhase(service=fake_service, event_emitter=emitter)  # type: ignore[arg-type]

        ctx = _make_context()
        await phase.run(ctx)

        assert ctx.knowledge_extraction_result is not None
        assert ctx.knowledge_extraction_result.status == ExtractionRunStatus.DEGRADED

    async def test_emitter_failure_does_not_break(self) -> None:
        """Event emitter failure should not propagate after context is written."""
        class _FailingEmitter:
            async def emit_knowledge_extracted(self, *, ctx: object, result: object) -> None:
                raise RuntimeError("emitter failed")

        fake_service = _FakeKnowledgeExtractionService()
        phase = KnowledgeExtractionPhase(service=fake_service, event_emitter=_FailingEmitter())  # type: ignore[arg-type]

        ctx = _make_context()
        # Should not raise despite emitter failure
        await phase.run(ctx)

        # Context should still be written
        assert len(ctx.preference_candidates) == 1
        assert ctx.knowledge_extraction_result is not None
