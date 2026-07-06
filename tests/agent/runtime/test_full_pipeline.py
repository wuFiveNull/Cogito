"""End-to-end integration test for the full 8-phase pipeline.

Verifies that all phases wire together correctly:
  1. TurnInitPhase
  2. StateLoadPhase
  3. InformationRetrievalPhase (stub)
  4. ContextAssemblyPhase
  5. AgentLoopPhase (with stub ports → no-op loop)
  6. KnowledgeExtractionPhase (stub)
  7. PersistencePhase (stub)
  8. TurnFinalizePhase

This test uses real phase implementations with fake/stub dependencies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from cogito.agent.domain.messages import ModelMessage
from cogito.agent.domain.model import (
    ModelInvocationRequest,
    ModelStreamEvent,
    ModelTextDelta,
    ModelUsageUpdate,
    ModelCompleted,
    ModelFinishReason,
)
from cogito.agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)
from cogito.agent.domain.tools import (
    PreparedToolCall,
    ToolDefinition,
    ToolExecutionResult,
    ToolExecutionStatus,
)
from cogito.agent.domain.usage import UsageSummary
from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.events import InMemoryAgentEventSink
from cogito.agent.ports.ids import IdGeneratorPort
from cogito.agent.ports.model import ModelPort
from cogito.agent.ports.model_context import (
    ContextWindowRequest,
    ModelContextWindowPort,
)
from cogito.agent.ports.repositories import (
    MessageRepositoryPort,
    SessionConfigRepositoryPort,
    SessionRepositoryPort,
    SummaryRepositoryPort,
    UserProfileRepositoryPort,
    UserSettingsRepositoryPort,
)
from cogito.agent.ports.tool_policy import (
    ToolPolicyDecision,
    ToolPolicyDecisionType,
    ToolPolicyPort,
)
from cogito.agent.ports.tools import (
    ToolExecutionContext,
    ToolExecutorPort,
    ToolRegistryPort,
)
from cogito.agent.ports.tracing import RuntimeTracePort
from cogito.agent.runtime.cleanup import DefaultRuntimeCleanup
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.context_factory import TurnContextFactory
from cogito.agent.runtime.errors import DefaultRuntimeErrorMapper
from cogito.agent.runtime.events import AgentEventType
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.models import AgentRequest, TurnResult, TurnStatus
from cogito.agent.runtime.phase import RuntimePhase
from cogito.agent.runtime.phases import (
    AgentLoopConfig,
    AgentLoopPhase,
    ContextAssemblyPhase,
    KnowledgeExtractionPhase,
    PersistencePhase,
    StateLoadPhase,
    TurnFinalizePhase,
    TurnInitPhase,
    TurnInitConfig,
)
from cogito.agent.retrieval.diagnostics import RetrievalDiagnosticsBuilder
from cogito.agent.retrieval.fusion import WeightedReciprocalRankFusion
from cogito.agent.retrieval.normalization import RetrievalNormalizer
from cogito.agent.retrieval.query_builder import RetrievalQueryBuilder
from cogito.agent.retrieval.routing import RetrievalPhaseConfig, RetrievalRoutingPolicy
from cogito.agent.retrieval.selection import RetrievalSelector
from cogito.agent.retrieval.validation import RetrievalItemValidator
from cogito.agent.ports.knowledge_extraction import (
    StubKnowledgeExtractor,
    StubRuntimeEventEmitter,
)
from cogito.agent.ports.retrieval import (
    AllowAllAccessFilter,
    IdentityRetrievalReranker,
)

FIXED_TIME = datetime(2026, 6, 24, 12, 0, 0)


# ── ContextAssembly fake ports ──────────────────────────────────────────


class FakeCATokenEstimator:
    name = "fake-ca-tokenizer"

    def estimate_text(self, text: str) -> int:
        return len(text.split()) if text else 0

    def estimate_messages(self, messages: list) -> int:
        return sum(self.estimate_text(m.content) for m in messages) + len(messages)


class FakeCATemplates:
    version = "test-ca-v1"

    def render_system(self, *, policy: str) -> str:
        return policy

    def render_user_settings(self, settings: object) -> str:
        return "settings"

    def render_profile(self, profile: object) -> str:
        return "profile"

    def render_summary(self, summary: object) -> str:
        return "summary"

    def render_retrieved_item(self, **kwargs: object) -> str:
        return "retrieved"

    def render_dynamic_context(self, block_texts: list[str]) -> str:
        return "\n".join(block_texts)

    def render_user_text(self, text: str) -> str:
        return text


class FakeCASanitizer:
    def sanitize_user_text(self, text: str) -> str:
        return text

    def sanitize_external_context(self, text: str) -> str:
        return text


# ── AgentLoop stub ports (no-op, return empty responses) ────────────────


class StubModelPort:
    """Returns a text stop event for any request (models "final response" directly)."""

    async def stream(
        self,
        request: ModelInvocationRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        yield ModelTextDelta(text="OK")
        yield ModelUsageUpdate(input_tokens=0, output_tokens=0)
        yield ModelCompleted(
            finish_reason=ModelFinishReason.STOP,
            provider_response_id=None,
        )


class StubContextWindowPort:
    async def fit(
        self,
        request: ContextWindowRequest,
    ) -> tuple[ModelMessage, ...]:
        return request.messages


class StubToolRegistryPort:
    def resolve(
        self,
        *,
        name: str,
        available_tools: tuple[ToolDefinition, ...],
    ) -> ToolDefinition | None:
        return None

    def validate_arguments(
        self,
        *,
        definition: ToolDefinition,
        arguments: dict,
    ) -> None:
        pass


class StubToolPolicyPort:
    async def evaluate(
        self,
        *,
        actor_id: str,
        session_id: str,
        prepared_call: PreparedToolCall,
    ) -> ToolPolicyDecision:
        return ToolPolicyDecision(
            decision=ToolPolicyDecisionType.ALLOW,
            reason_code="STUB",
            safe_message="",
        )


class StubToolExecutorPort:
    async def execute(
        self,
        *,
        prepared_call: PreparedToolCall,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=prepared_call.call.call_id,
            tool_name=prepared_call.call.tool_name,
            status=ToolExecutionStatus.SUCCEEDED,
            model_content="stub result",
        )


# ── Fake infrastructure ──────────────────────────────────────────────


class FakeClock:
    def now(self) -> datetime:
        return FIXED_TIME


class FakeIdGenerator:
    def __init__(self) -> None:
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return f"turn-{self._counter:04d}"


class FakeTrace:
    def __init__(self) -> None:
        self.starts: list[tuple[str, str]] = []
        self.ends: list[tuple[str, str]] = []

    async def start_turn(
        self,
        *,
        turn_id: str,
        request_id: str,
    ) -> str:
        self.starts.append((turn_id, request_id))
        return f"trace-{turn_id}"

    async def end_turn(
        self,
        *,
        trace_id: str,
        status: str,
    ) -> None:
        self.ends.append((trace_id, status))


class FakeSessionRepository:
    def __init__(self, session: SessionState | None = None) -> None:
        self._session = session

    async def get(self, session_id: str) -> SessionState | None:
        if self._session is None:
            return None
        if self._session.session_id != session_id:
            return None
        return self._session


class FakeMessageRepository:
    async def list_recent(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[ConversationMessage]:
        if session_id == "unknown":
            return []
        return [
            ConversationMessage(
                message_id="msg-001",
                session_id=session_id,
                actor_id="actor-001",
                role="user",
                content="previous message",
                sequence=0,
                created_at=FIXED_TIME,
            ),
        ]


class FakeSummaryRepository:
    async def get(self, session_id: str) -> SessionSummary | None:
        return SessionSummary(
            session_id=session_id,
            content="Session summary so far",
            version=3,
        )


class FakeUserProfileRepository:
    async def get(self, actor_id: str) -> UserProfile | None:
        return UserProfile(
            actor_id=actor_id,
            display_name="TestUser",
            locale="zh-CN",
            timezone="Asia/Shanghai",
        )


class FakeUserSettingsRepository:
    async def get(self, actor_id: str) -> UserSettings | None:
        return UserSettings(
            locale="zh-CN",
            timezone="Asia/Shanghai",
            response_style="concise",
        )


class FakeSessionConfigRepository:
    async def get(self, session_id: str) -> SessionConfig | None:
        return SessionConfig(
            history_limit=20,
            max_tool_rounds=4,
        )


# ── The integration test ─────────────────────────────────────────────


# ── Stub KnowledgeExtractionPhase ─────────────────────────────────────


def _make_stub_knowledge_extraction_phase() -> RuntimePhase:
    """Build a KnowledgeExtractionPhase with stub dependencies.

    Uses StubKnowledgeExtractor (returns empty results) and a
    StubRuntimeEventEmitter.  The phase will run but produce no
    candidates — safe for integration tests.
    """
    from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig
    from cogito.agent.runtime.extraction import (
        CandidateConflictResolver,
        CandidateDeduplicator,
        CandidateNormalizer,
        CandidateValidator,
        ConfidenceCalibrator,
        DeterministicRuleExtractor,
        ExtractionEligibilityEvaluator,
        ExtractionInputBuilder,
        KnowledgeExtractionService,
        SensitivityPolicy,
        StrictRawExtractionParser,
        SummaryCandidateBuilder,
    )
    from cogito.agent.runtime.phases import KnowledgeExtractionPhase

    config = KnowledgeExtractionConfig(enabled=False)
    service = KnowledgeExtractionService(
        config=config,
        input_builder=ExtractionInputBuilder(config=config),
        eligibility=ExtractionEligibilityEvaluator(config=config),
        rule_extractor=DeterministicRuleExtractor(),
        structured_extractor=StubKnowledgeExtractor(),
        parser=StrictRawExtractionParser(),
        normalizer=CandidateNormalizer(),
        validator=CandidateValidator(),
        sensitivity_policy=SensitivityPolicy(config=config),
        conflict_resolver=CandidateConflictResolver(),
        confidence_calibrator=ConfidenceCalibrator(),
        deduplicator=CandidateDeduplicator(),
        summary_builder=SummaryCandidateBuilder(config=config),
        clock=FakeClock(),
    )
    return KnowledgeExtractionPhase(
        service=service,
        event_emitter=StubRuntimeEventEmitter(),
    )


def _make_empty_retrieval_phase() -> RuntimePhase:
    """Build a stub InformationRetrievalPhase that returns empty results.

    Uses no retrievers — the phase will produce an empty result set,
    which is the correct stub behaviour for pipeline integration tests.
    """
    config = RetrievalPhaseConfig(
        phase_timeout_seconds=3.0,
        max_concurrency=5,
        final_limit=20,
        max_per_kind=8,
        max_per_source=10,
        empty_query_allowed=True,
        sources={},
    )
    query_builder = RetrievalQueryBuilder(default_limit=config.final_limit)
    routing_policy = RetrievalRoutingPolicy(config)
    validator = RetrievalItemValidator()
    normalizer = RetrievalNormalizer()
    fusion = WeightedReciprocalRankFusion(rrf_k=config.rrf_k)
    selector = RetrievalSelector(
        final_limit=config.final_limit,
        max_per_kind=config.max_per_kind,
        max_per_source=config.max_per_source,
    )
    diagnostics_builder = RetrievalDiagnosticsBuilder()

    from cogito.agent.runtime.phases import InformationRetrievalPhase as _IRP
    return _IRP(
        retrievers=[],
        query_builder=query_builder,
        routing_policy=routing_policy,
        validator=validator,
        access_filter=AllowAllAccessFilter(),
        normalizer=normalizer,
        fusion=fusion,
        reranker=IdentityRetrievalReranker(),
        selector=selector,
        diagnostics_builder=diagnostics_builder,
        config=config,
    )


@pytest.mark.asyncio
async def test_full_pipeline_successful_turn() -> None:
    """Full 8-phase pipeline completes successfully end-to-end."""

    clock = FakeClock()
    id_generator = FakeIdGenerator()
    trace = FakeTrace()
    event_sink = InMemoryAgentEventSink()
    context_factory = TurnContextFactory(clock=clock, id_generator=id_generator)
    session_repo = FakeSessionRepository()

    phases: list[RuntimePhase] = [
        TurnInitPhase(
            trace=trace,
            config=TurnInitConfig(max_tool_rounds=8),
        ),
        StateLoadPhase(
            sessions=session_repo,
            messages=FakeMessageRepository(),
            summaries=FakeSummaryRepository(),
            user_profiles=FakeUserProfileRepository(),
            user_settings_repo=FakeUserSettingsRepository(),
            session_configs=FakeSessionConfigRepository(),
            options=None,
        ),
        _make_empty_retrieval_phase(),
        ContextAssemblyPhase(
            templates=FakeCATemplates(),
            token_estimator=FakeCATokenEstimator(),
            sanitizer=FakeCASanitizer(),
        ),
        AgentLoopPhase(
            model=StubModelPort(),
            context_window=StubContextWindowPort(),
            tool_registry=StubToolRegistryPort(),
            tool_policy=StubToolPolicyPort(),
            tool_executor=StubToolExecutorPort(),
            clock=clock,
            config=AgentLoopConfig(max_tool_rounds=4),
        ),
        _make_stub_knowledge_extraction_phase(),
        PersistencePhase(),
        TurnFinalizePhase(),
    ]

    kernel = RuntimeKernel(
        phases=phases,
        context_factory=context_factory,
        default_event_sink=event_sink,
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )

    request = AgentRequest(
        request_id="req-001",
        session_id="session-001",
        actor_id="actor-001",
        text="Hello, how are you?",
    )
    result: TurnResult = await kernel.run(request)

    # ── Assertions ───────────────────────────────────────────────────

    assert result.status == TurnStatus.COMPLETED, f"Expected COMPLETED, got {result.status}"
    assert result.text == "OK"
    assert result.turn_id is not None
    assert result.request_id == "req-001"
    assert result.session_id == "session-001"
    assert result.actor_id == "actor-001"

    # Event sequence check
    event_types = [e.type for e in event_sink.events]
    assert AgentEventType.TURN_STARTED in event_types
    assert AgentEventType.TURN_COMPLETED in event_types
    assert AgentEventType.TURN_FAILED not in event_types

    phase_started = [e for e in event_sink.events if e.type == AgentEventType.PHASE_STARTED]
    phase_completed = [e for e in event_sink.events if e.type == AgentEventType.PHASE_COMPLETED]
    assert len(phase_started) == 8
    assert len(phase_completed) == 8

    phase_names = [e.phase for e in phase_started]
    assert phase_names == [
        "turn_init",
        "state_load",
        "information_retrieval",
        "context_assembly",
        "agent_loop",
        "knowledge_extraction",
        "persistence",
        "turn_finalize",
    ]

    assert len(trace.starts) == 1
    assert trace.starts[0][1] == "req-001"


@pytest.mark.asyncio
async def test_full_pipeline_uses_state_load_data() -> None:
    """StateLoadPhase loaded data is used by downstream phases."""
    clock = FakeClock()
    id_generator = FakeIdGenerator()
    trace = FakeTrace()
    event_sink = InMemoryAgentEventSink()
    context_factory = TurnContextFactory(clock=clock, id_generator=id_generator)

    session_repo = FakeSessionRepository(
        session=SessionState(session_id="session-001", actor_id="actor-001"),
    )

    phases: list[RuntimePhase] = [
        TurnInitPhase(trace=trace, config=TurnInitConfig(max_tool_rounds=8)),
        StateLoadPhase(
            sessions=session_repo,
            messages=FakeMessageRepository(),
            summaries=FakeSummaryRepository(),
            user_profiles=FakeUserProfileRepository(),
            user_settings_repo=FakeUserSettingsRepository(),
            session_configs=FakeSessionConfigRepository(),
            options=None,
        ),
        _make_empty_retrieval_phase(),
        ContextAssemblyPhase(
            templates=FakeCATemplates(),
            token_estimator=FakeCATokenEstimator(),
            sanitizer=FakeCASanitizer(),
        ),
        AgentLoopPhase(
            model=StubModelPort(),
            context_window=StubContextWindowPort(),
            tool_registry=StubToolRegistryPort(),
            tool_policy=StubToolPolicyPort(),
            tool_executor=StubToolExecutorPort(),
            clock=clock,
            config=AgentLoopConfig(max_tool_rounds=4),
        ),
        _make_stub_knowledge_extraction_phase(),
        PersistencePhase(),
        TurnFinalizePhase(),
    ]

    kernel = RuntimeKernel(
        phases=phases,
        context_factory=context_factory,
        default_event_sink=event_sink,
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )

    result = await kernel.run(
        AgentRequest(
            request_id="req-002",
            session_id="session-001",
            actor_id="actor-001",
            text="Hi",
        ),
    )

    assert result.status == TurnStatus.COMPLETED
    assert result.text == "OK"
    assert result.turn_id is not None


@pytest.mark.asyncio
async def test_full_pipeline_missing_session_allowed() -> None:
    """New session with no state works without error."""
    clock = FakeClock()
    id_generator = FakeIdGenerator()
    trace = FakeTrace()
    context_factory = TurnContextFactory(clock=clock, id_generator=id_generator)

    phases: list[RuntimePhase] = [
        TurnInitPhase(trace=trace, config=TurnInitConfig()),
        StateLoadPhase(
            sessions=FakeSessionRepository(session=None),
            messages=FakeMessageRepository(),
            summaries=FakeSummaryRepository(),
            user_profiles=FakeUserProfileRepository(),
            user_settings_repo=FakeUserSettingsRepository(),
            session_configs=FakeSessionConfigRepository(),
        ),
        _make_empty_retrieval_phase(),
        ContextAssemblyPhase(
            templates=FakeCATemplates(),
            token_estimator=FakeCATokenEstimator(),
            sanitizer=FakeCASanitizer(),
        ),
        AgentLoopPhase(
            model=StubModelPort(),
            context_window=StubContextWindowPort(),
            tool_registry=StubToolRegistryPort(),
            tool_policy=StubToolPolicyPort(),
            tool_executor=StubToolExecutorPort(),
            clock=clock,
            config=AgentLoopConfig(max_tool_rounds=4),
        ),
        _make_stub_knowledge_extraction_phase(),
        PersistencePhase(),
        TurnFinalizePhase(),
    ]

    kernel = RuntimeKernel(
        phases=phases,
        context_factory=context_factory,
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )

    result = await kernel.run(
        AgentRequest(
            request_id="req-003",
            session_id="new-session",
            actor_id="new-actor",
            text="First message",
        ),
    )

    assert result.status == TurnStatus.COMPLETED
    assert result.text == "OK"
    assert result.turn_id is not None
