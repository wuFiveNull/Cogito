# cogito/agent/bootstrap/runtime_factory.py

from __future__ import annotations

from collections.abc import Sequence

from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.events import (
    AgentEventSink,
    InMemoryAgentEventSink,
    NullAgentEventSink,
)
from cogito.agent.ports.ids import IdGeneratorPort
from cogito.agent.ports.knowledge_extraction import (
    KnowledgeExtractorPort,
    RuntimeEventEmitter,
    StubKnowledgeExtractor,
)
from cogito.agent.ports.model import ModelPort
from cogito.agent.ports.model_context import ModelContextWindowPort
from cogito.agent.ports.prompt_cache import PromptCachePort
from cogito.agent.ports.summarizer import SummarizerPort
from cogito.agent.runtime.cleanup import (
    CompositeCleanup,
    ConsolidationCleanup,
    DefaultRuntimeCleanup,
)
from cogito.agent.ports.repositories import (
    MessageRepositoryPort,
    SessionConfigRepositoryPort,
    SessionRepositoryPort,
    SummaryRepositoryPort,
    UserProfileRepositoryPort,
    UserSettingsRepositoryPort,
)
from cogito.agent.ports.retrieval import (
    AllowAllAccessFilter,
    IdentityRetrievalReranker,
    RetrievalAccessFilterPort,
    RetrievalFusionPort,
    RetrievalRerankerPort,
    RetrieverPort,
)
from cogito.agent.ports.sanitizer import ContextSanitizerPort, DefaultContextSanitizer
from cogito.agent.ports.templates import PromptTemplatePort, DefaultPromptTemplates
from cogito.agent.ports.tokenizer import TokenEstimatorPort, ApproximateTokenEstimator
from cogito.agent.ports.tool_policy import ToolPolicyPort
from cogito.agent.ports.tools import ToolExecutorPort, ToolRegistryPort
from cogito.agent.domain.tools import ToolDefinition
from cogito.agent.ports.tracing import RuntimeTracePort
from cogito.agent.retrieval.diagnostics import RetrievalDiagnosticsBuilder
from cogito.agent.retrieval.fusion import WeightedReciprocalRankFusion
from cogito.agent.retrieval.normalization import RetrievalNormalizer
from cogito.agent.retrieval.query_builder import RetrievalQueryBuilder
from cogito.agent.retrieval.routing import RetrievalPhaseConfig, RetrievalRoutingPolicy, SourceConfig
from cogito.agent.retrieval.selection import RetrievalSelector
from cogito.agent.retrieval.validation import RetrievalItemValidator
from cogito.agent.runtime.cleanup import DefaultRuntimeCleanup
from cogito.agent.runtime.context_factory import TurnContextFactory
from cogito.agent.runtime.errors import DefaultRuntimeErrorMapper
from cogito.agent.runtime.events import AgentEventType
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
from cogito.agent.runtime.extraction.config import default_knowledge_extraction_config
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.phase import RuntimePhase
from cogito.agent.runtime.persistence.fingerprint import PersistenceFingerprint
from cogito.agent.runtime.persistence.planner import PersistencePlanBuilder
from cogito.agent.runtime.persistence.sanitizer import PersistenceSanitizer
from cogito.agent.runtime.phases import (
    AgentLoopConfig,
    AgentLoopPhase,
    ContextAssemblyOptions,
    ContextAssemblyPhase,
    InformationRetrievalPhase,
    KnowledgeExtractionPhase,
    PersistencePhase,
    StateLoadPhase,
    TurnFinalizePhase,
    TurnInitPhase,
    TurnInitConfig,
)
from cogito.database.connection import AsyncDatabase
from cogito.infrastructure.sqlite.repositories.state_load import (
    SQLiteMessageReadAdapter,
    SQLiteSessionConfigReadAdapter,
    SQLiteSessionReadAdapter,
    SQLiteSummaryReadAdapter,
    SQLiteUserProfileReadAdapter,
    SQLiteUserSettingsReadAdapter,
)


class _NullTrace:
    """No-op trace implementation for testing or when no trace is configured."""

    async def start_turn(
        self,
        *,
        turn_id: str,
        request_id: str,
    ) -> str:
        return turn_id or "trace-unknown"

    async def end_turn(
        self,
        *,
        trace_id: str,
        status: str,
    ) -> None:
        pass


class _DefaultRuntimeEventEmitter:
    """Default implementation of RuntimeEventEmitter.

    Emits knowledge_extracted events through the TurnEventEmitter
    on the context.  Event payload is limited to safe diagnostic
    data (counts, status, duration) — never candidate content.
    """

    async def emit_knowledge_extracted(
        self,
        *,
        ctx: object,
        result: object,
    ) -> None:
        from cogito.agent.domain.knowledge.extraction import KnowledgeExtractionResult

        if not isinstance(result, KnowledgeExtractionResult):
            return
        if ctx is None or not hasattr(ctx, "event_emitter") or ctx.event_emitter is None:  # type: ignore[union-attr]
            return

        await ctx.event_emitter.emit(  # type: ignore[union-attr]
            AgentEventType.KNOWLEDGE_EXTRACTED,
            phase="knowledge_extraction",
            data={
                "status": str(result.status.value),
                "preferences": len(result.preference_candidates),
                "memories": len(result.memory_candidates),
                "summary_generated": result.summary_candidate is not None,
                "dropped": result.dropped_count,
                "duration_ms": result.diagnostics.duration_ms if result.diagnostics else 0,
                "warning_codes": list(result.diagnostics.warnings) if result.diagnostics else [],
            },
        )


def build_information_retrieval_phase(
    *,
    retrievers: Sequence[RetrieverPort] | None = None,
    access_filter: RetrievalAccessFilterPort | None = None,
    reranker: RetrievalRerankerPort | None = None,
    config: RetrievalPhaseConfig | None = None,
) -> InformationRetrievalPhase:
    """Build a fully-wired InformationRetrievalPhase.

    When ``retrievers`` is not provided, the phase is still created
    but will have no sources configured (returns empty results for
    every turn).  This is the safe default for testing.

    Args:
        retrievers: List of RetrieverPort implementations.
        access_filter: ACL filter (defaults to AllowAllAccessFilter).
        reranker: Optional re-ranker (defaults to IdentityRetrievalReranker).
        config: Retrieval phase configuration (defaults to empty sources).

    Returns:
        A ready-to-use InformationRetrievalPhase.
    """
    resolved_retrievers = list(retrievers) if retrievers else []
    resolved_config = config or RetrievalPhaseConfig(sources={})
    resolved_access_filter = access_filter or AllowAllAccessFilter()
    resolved_reranker = reranker or IdentityRetrievalReranker()

    query_builder = RetrievalQueryBuilder(
        default_limit=resolved_config.final_limit,
    )
    routing_policy = RetrievalRoutingPolicy(resolved_config)
    validator = RetrievalItemValidator()
    normalizer = RetrievalNormalizer()
    fusion = WeightedReciprocalRankFusion(rrf_k=resolved_config.rrf_k)
    selector = RetrievalSelector(
        final_limit=resolved_config.final_limit,
        max_per_kind=resolved_config.max_per_kind,
        max_per_source=resolved_config.max_per_source,
    )
    diagnostics_builder = RetrievalDiagnosticsBuilder()

    return InformationRetrievalPhase(
        retrievers=resolved_retrievers,
        query_builder=query_builder,
        routing_policy=routing_policy,
        validator=validator,
        access_filter=resolved_access_filter,
        normalizer=normalizer,
        fusion=fusion,
        reranker=resolved_reranker,
        selector=selector,
        diagnostics_builder=diagnostics_builder,
        config=resolved_config,
    )


def build_knowledge_extraction_phase(
    *,
    clock: ClockPort,
    extractor: KnowledgeExtractorPort | None = None,
    event_emitter: RuntimeEventEmitter | None = None,
    config: object | None = None,
) -> KnowledgeExtractionPhase:
    """Build a fully-wired KnowledgeExtractionPhase.

    When ``extractor`` is not provided, only rule-based extraction
    runs (no LLM-based structured extraction).  This is the safe
    default for testing or when no extraction model is configured.

    Args:
        clock: Time source for diagnostics.
        extractor: Optional structured extractor port.
        event_emitter: Optional safe event emitter; defaults to a
            ``_DefaultRuntimeEventEmitter``.
        config: Optional ``KnowledgeExtractionConfig``; defaults to
            standard limits.

    Returns:
        A ready-to-use KnowledgeExtractionPhase.
    """
    from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig

    resolved_config = config if isinstance(config, KnowledgeExtractionConfig) else (
        config or default_knowledge_extraction_config()
    )
    resolved_extractor = extractor or StubKnowledgeExtractor()
    resolved_emitter = event_emitter or _DefaultRuntimeEventEmitter()

    service = KnowledgeExtractionService(
        config=resolved_config,
        input_builder=ExtractionInputBuilder(config=resolved_config),
        eligibility=ExtractionEligibilityEvaluator(config=resolved_config),
        rule_extractor=DeterministicRuleExtractor(),
        structured_extractor=resolved_extractor,
        parser=StrictRawExtractionParser(),
        normalizer=CandidateNormalizer(),
        validator=CandidateValidator(),
        sensitivity_policy=SensitivityPolicy(config=resolved_config),
        conflict_resolver=CandidateConflictResolver(),
        confidence_calibrator=ConfidenceCalibrator(),
        deduplicator=CandidateDeduplicator(),
        summary_builder=SummaryCandidateBuilder(config=resolved_config),
        clock=clock,
    )

    return KnowledgeExtractionPhase(
        service=service,
        event_emitter=resolved_emitter,
    )


def build_runtime_kernel(
    *,
    clock: ClockPort,
    id_generator: IdGeneratorPort,
    trace: RuntimeTracePort | None = None,
    event_sink: AgentEventSink | None = None,
    # AgentLoopPhase ports
    model: ModelPort | None = None,
    context_window: ModelContextWindowPort | None = None,
    tool_registry: ToolRegistryPort | None = None,
    tool_policy: ToolPolicyPort | None = None,
    tool_executor: ToolExecutorPort | None = None,
    agent_loop_config: AgentLoopConfig | None = None,
    # ContextAssemblyPhase ports (optional — use defaults when not provided)
    prompt_templates: PromptTemplatePort | None = None,
    token_estimator: TokenEstimatorPort | None = None,
    sanitizer: ContextSanitizerPort | None = None,
    context_assembly_options: ContextAssemblyOptions | None = None,
    # StateLoadPhase repositories (optional — stubs when not provided)
    session_repository: SessionRepositoryPort | None = None,
    message_repository: MessageRepositoryPort | None = None,
    summary_repository: SummaryRepositoryPort | None = None,
    user_profile_repository: UserProfileRepositoryPort | None = None,
    user_settings_repository: UserSettingsRepositoryPort | None = None,
    session_config_repository: SessionConfigRepositoryPort | None = None,
    # InformationRetrievalPhase (optional — empty results when not provided)
    retrievers: Sequence[RetrieverPort] | None = None,
    retrieval_access_filter: RetrievalAccessFilterPort | None = None,
    retrieval_reranker: RetrievalRerankerPort | None = None,
    retrieval_config: RetrievalPhaseConfig | None = None,
    # PersistencePhase dependencies (optional — stubs when not provided)
    uow_factory: object | None = None,
    persistence_planner: object | None = None,
    persistence_sanitizer: object | None = None,
    persistence_fingerprint: object | None = None,
    preference_policy: object | None = None,
    memory_policy: object | None = None,
    retry_policy: object | None = None,
    commit_recovery: object | None = None,
    embedding_port: object | None = None,
    embedding_model: str = "",
    # KnowledgeExtractionPhase (optional — rule-based only when not provided)
    knowledge_extractor: KnowledgeExtractorPort | None = None,
    knowledge_extraction_event_emitter: RuntimeEventEmitter | None = None,
    knowledge_extraction_config: object | None = None,
    # Tool catalog (optional — no tools available when not provided)
    tool_catalog: ToolCatalogPort | None = None,
    # Tool definitions (optional — directly provide tool defs for ctx.available_tools)
    tool_definitions: Sequence[ToolDefinition] | None = None,
    # Consolidation dependencies
    consolidation_service: object | None = None,
    # Memory injector for markdown memory files
    memory_injector: object | None = None,
    # Prompt cache (optional — defaults to None / no caching)
    prompt_cache: PromptCachePort | None = None,
    # Summarizer (optional — defaults to None / no LLM summarization)
    summarizer: SummarizerPort | None = None,
) -> RuntimeKernel:
    """Build a production-ready runtime kernel with all eight phases.

    Args:
        clock: Time source for TurnContextFactory.
        id_generator: ID generation service for TurnContextFactory.
        trace: Runtime trace port (defaults to NullTrace if not provided).
        event_sink: Optional default event sink.
        model: LLM model port for AgentLoopPhase.
        context_window: Context window port for AgentLoopPhase.
        tool_registry: Tool registry port for AgentLoopPhase (name resolution + validation).
        tool_policy: Tool policy port for AgentLoopPhase (authorisation).
        tool_executor: Tool executor port for AgentLoopPhase.
        agent_loop_config: Configuration for AgentLoopPhase.
        prompt_templates: Optional prompt template port for ContextAssemblyPhase.
        token_estimator: Optional token estimator for ContextAssemblyPhase.
        sanitizer: Optional context sanitizer for ContextAssemblyPhase.
        context_assembly_options: Optional options for ContextAssemblyPhase.
        session_repository: Optional session repository for StateLoadPhase.
        message_repository: Optional message repository for StateLoadPhase.
        summary_repository: Optional summary repository for StateLoadPhase.
        user_profile_repository: Optional user profile repository for StateLoadPhase.
        user_settings_repository: Optional user settings repository for StateLoadPhase.
        session_config_repository: Optional session config repository for StateLoadPhase.

    When a required Port is not provided, AgentLoopPhase will still be
    created but will raise at runtime when the missing Port is called.
    Use ``AgentLoopPhase`` directly for fine-grained control.
    """
    context_factory = TurnContextFactory(
        clock=clock,
        id_generator=id_generator,
    )

    trace_port = trace or _NullTrace()

    # ContextAssemblyPhase defaults
    resolved_templates = prompt_templates or DefaultPromptTemplates()
    resolved_token_estimator = token_estimator or ApproximateTokenEstimator()
    resolved_sanitizer = sanitizer or DefaultContextSanitizer()
    resolved_ca_options = context_assembly_options or ContextAssemblyOptions()

    # AgentLoopPhase — auto-create ToolSystem if not wired
    # AgentLoopPhase — create even with missing deps (will raise clearly
    # at runtime).  Call build_tool_system() from tool_factory.py first
    # and pass tool_system.executor / tool_system.catalog / registry here.
    if any(p is None for p in (model, context_window, tool_registry, tool_policy, tool_executor)):
        _agent_loop = AgentLoopPhase(
            model=model,  # type: ignore[arg-type]
            context_window=context_window,  # type: ignore[arg-type]
            tool_registry=tool_registry,  # type: ignore[arg-type]
            tool_policy=tool_policy,  # type: ignore[arg-type]
            tool_executor=tool_executor,  # type: ignore[arg-type]
            clock=clock,
            config=agent_loop_config or AgentLoopConfig(),
            summarizer=summarizer,
        )
    else:
        _agent_loop = AgentLoopPhase(
            model=model,
            context_window=context_window,
            tool_registry=tool_registry,
            tool_policy=tool_policy,
            tool_executor=tool_executor,
            clock=clock,
            config=agent_loop_config or AgentLoopConfig(),
            summarizer=summarizer,
        )

    phases: list[RuntimePhase] = [
        TurnInitPhase(
            trace=trace_port,
            config=TurnInitConfig(
                max_tool_rounds=(
                    agent_loop_config.max_tool_rounds
                    if agent_loop_config
                    else 8
                ),
            ),
        ),
        StateLoadPhase(
            sessions=session_repository,
            messages=message_repository,
            summaries=summary_repository,
            user_profiles=user_profile_repository,
            user_settings_repo=user_settings_repository,
            session_configs=session_config_repository,
        ),
        build_information_retrieval_phase(
            retrievers=retrievers,
            access_filter=retrieval_access_filter,
            reranker=retrieval_reranker,
            config=retrieval_config,
        ),
        ContextAssemblyPhase(
            templates=resolved_templates,
            token_estimator=resolved_token_estimator,
            sanitizer=resolved_sanitizer,
            options=resolved_ca_options,
            tool_definitions=tool_definitions,
            prompt_cache=prompt_cache,
            memory_injector=memory_injector,
        ),
        _agent_loop,
        build_knowledge_extraction_phase(
            clock=clock,
            extractor=knowledge_extractor,
            event_emitter=knowledge_extraction_event_emitter,
            config=knowledge_extraction_config,
        ),
        PersistencePhase(
            clock=clock,
            uow_factory=uow_factory,
            planner=persistence_planner or (
                PersistencePlanBuilder(
                    id_generator=id_generator,
                    fingerprint=persistence_fingerprint or PersistenceFingerprint(),
                    sanitizer=persistence_sanitizer or PersistenceSanitizer(),
                    embedding_port=embedding_port,
                    embedding_model=embedding_model,
                )
                if uow_factory is not None
                else None
            ),
            sanitizer=persistence_sanitizer,
            fingerprint=persistence_fingerprint,
            preference_policy=preference_policy,
            memory_policy=memory_policy,
            retry_policy=retry_policy,
            commit_recovery=commit_recovery,
            embedding_port=embedding_port,
            embedding_model=embedding_model,
        ),
        TurnFinalizePhase(),
    ]

    # Build cleanup chain (default + optional consolidation)
    cleanup_hooks: list[object] = [DefaultRuntimeCleanup()]
    if consolidation_service is not None and message_repository is not None:
        cleanup_hooks.append(
            ConsolidationCleanup(
                session_repo=message_repository,
                consolidation=consolidation_service,
                enabled=True,
            ),
        )
    cleanup = CompositeCleanup(*cleanup_hooks)

    return RuntimeKernel(
        phases=phases,
        context_factory=context_factory,
        default_event_sink=event_sink or NullAgentEventSink(),
        cleanup=cleanup,
        error_mapper=DefaultRuntimeErrorMapper(),
    )


def build_test_kernel(
    phases: Sequence[RuntimePhase],
    *,
    context_factory: TurnContextFactory | None = None,
) -> RuntimeKernel:
    """Build a test-ready kernel with the given phases and test infrastructure.

    Args:
        phases: The phases to include in the pipeline.
        context_factory: Optional factory; a simple default is used if omitted.
    """
    if context_factory is None:
        from cogito.agent.runtime.context import TurnContext
        from cogito.agent.runtime.models import AgentRequest, TurnStatus
        from datetime import datetime
        from uuid import uuid4

        class _SimpleClock:
            def now(self):
                return datetime.now()

        class _SimpleIdGenerator:
            def new_id(self) -> str:
                return f"turn_{uuid4().hex[:12]}"

        context_factory = TurnContextFactory(
            clock=_SimpleClock(),
            id_generator=_SimpleIdGenerator(),
        )

    return RuntimeKernel(
        phases=phases,
        context_factory=context_factory,
        default_event_sink=InMemoryAgentEventSink(),
        cleanup=DefaultRuntimeCleanup(),
        error_mapper=DefaultRuntimeErrorMapper(),
    )


def build_state_load_adapters(
    db: AsyncDatabase,
) -> tuple[
    SQLiteSessionReadAdapter,
    SQLiteMessageReadAdapter,
    SQLiteSummaryReadAdapter,
    SQLiteUserProfileReadAdapter,
    SQLiteUserSettingsReadAdapter,
    SQLiteSessionConfigReadAdapter,
]:
    """Create StateLoadPhase repository adapters from an AsyncDatabase.

    Returns all six adapters as a tuple in the order:
        (session, message, summary, user_profile, user_settings, session_config)

    Usage::

        db = await AsyncDatabase.open("path/to/cogito.db")
        session, msg, summ, prof, sett, cfg = build_state_load_adapters(db)
        kernel = build_runtime_kernel(
            ...,
            session_repository=session,
            message_repository=msg,
            summary_repository=summ,
            user_profile_repository=prof,
            user_settings_repository=sett,
            session_config_repository=cfg,
        )
    """
    return (
        SQLiteSessionReadAdapter(db),
        SQLiteMessageReadAdapter(db),
        SQLiteSummaryReadAdapter(db),
        SQLiteUserProfileReadAdapter(db),
        SQLiteUserSettingsReadAdapter(db),
        SQLiteSessionConfigReadAdapter(db),
    )
