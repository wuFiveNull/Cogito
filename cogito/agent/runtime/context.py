# cogito/agent/runtime/context.py
#
# TurnContext — strongly typed mutable state for one agent turn.
#
# A new TurnContext is created per AgentRequest and flows through the
# 8-phase pipeline.  Phases communicate by reading from and writing to
# this object.  Each field has a clear owner Phase.
#
# Design rules (see initial-framework-spec §7, agent-loop-spec §6):
#   - Core fields are explicitly declared, not hidden in metadata dict.
#   - AgentLoop fields (model_calls_used, pending_approval, …) live
#     here so PersistencePhase can save them without cross-phase coupling.
#   - metadata is a limited extension area for truly transient data.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol

from cogito.agent.domain.approval import (
    AgentLoopCheckpoint,
    PendingApprovalBatch,
)
from cogito.agent.domain.memory import MemoryCandidate, SummaryCandidate
from cogito.agent.domain.messages import AssistantMessage, ModelMessage
from cogito.agent.domain.model import ModelRoundOutput
from cogito.agent.domain.model_input import ContextAssemblyResult
from cogito.agent.domain.knowledge.extraction import KnowledgeExtractionResult
from cogito.agent.domain.preferences import PreferenceCandidate
from cogito.agent.domain.retrieval import (
    RetrievedItem,
    RetrievalDiagnostics,
)
from cogito.agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)
from cogito.agent.domain.tools import ToolDefinition
from cogito.agent.domain.usage import ToolExecutionRecord, UsageSummary
from cogito.agent.runtime.events import AgentEventType
from cogito.agent.runtime.models import AgentRequest, TurnResult, TurnStatus


# ── TurnEventEmitter (bound to a single turn) ───────────────────────────
# Phase code emits events through this object, not the raw AgentEventSink.
# It auto-completes turn_id, request_id, timestamp and isolates failures.
# (see agent-loop-spec §6.1)


class TurnEventEmitter(Protocol):
    """Safe event emitter for one turn, bound to the context."""

    async def emit(
        self,
        event_type: AgentEventType,
        *,
        phase: str | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        ...


# ── CancellationToken (see agent-loop-spec §6.2) ──────────────────────────


class CancellationToken(Protocol):
    @property
    def is_cancelled(self) -> bool:
        ...

    def raise_if_cancelled(self) -> None:
        ...


# ── TurnContext ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class TurnContext:
    """Mutable per-turn state for the agent runtime pipeline.

    Lifecycle
    ---------
    Created by TurnContextFactory → TurnInitPhase extends →
    Phase 2–8 each read & write → TurnFinalizePhase snapshots →
    RuntimeCleanup finalises.
    """

    request: AgentRequest

    # ── Lifecycle ─────────────────────────────────────────────────────
    turn_id: str | None = None
    status: TurnStatus = TurnStatus.CREATED
    started_at: datetime | None = None
    completed_at: datetime | None = None
    current_phase: str | None = None

    # ── Trace / cancellation / runtime limits ─────────────────────────
    trace_id: str | None = None
    cancellation_requested: bool = False
    max_tool_rounds: int = 8

    # AgentLoop deadline check (set by TurnInitPhase or from session cfg)
    deadline_at: datetime | None = None
    cancellation_token: CancellationToken | None = None
    event_emitter: TurnEventEmitter | None = field(default=None, repr=False)

    # ── Deterministic state (loaded by StateLoadPhase) ────────────────
    session: SessionState | None = None
    recent_messages: list[ConversationMessage] = field(default_factory=list)
    session_summary: SessionSummary | None = None
    user_profile: UserProfile | None = None
    user_settings: UserSettings = field(default_factory=UserSettings)
    session_config: SessionConfig = field(default_factory=SessionConfig)

    # ── Retrieval (written by InformationRetrievalPhase) ──────────────
    retrieved_items: list[RetrievedItem] = field(default_factory=list)
    retrieval_diagnostics: RetrievalDiagnostics | None = None
    current_preferences: list[object] = field(default_factory=list)

    # ── Context assembly (written by ContextAssemblyPhase) ────────────
    model_messages: list[ModelMessage] = field(default_factory=list)
    available_tools: list[ToolDefinition] = field(default_factory=list)
    context_assembly: ContextAssemblyResult | None = None
    effective_model_profile: str | None = None

    # ── Agent loop (written by AgentLoopPhase) ────────────────────────
    model_responses: list[ModelRoundOutput] = field(default_factory=list)
    final_response: AssistantMessage | None = None
    output_text: str | None = None
    tool_records: list[ToolExecutionRecord] = field(default_factory=list)
    usage: UsageSummary = field(default_factory=UsageSummary)

    model_calls_used: int = 0
    tool_rounds_used: int = 0
    total_tool_calls_used: int = 0

    # ── Approval / suspension (written by AgentLoopPhase) ─────────────
    pending_approval: PendingApprovalBatch | None = None
    loop_checkpoint: AgentLoopCheckpoint | None = None

    # ── Knowledge extraction (written by KnowledgeExtractionPhase) ────
    preference_candidates: list[PreferenceCandidate] = field(default_factory=list)
    memory_candidates: list[MemoryCandidate] = field(default_factory=list)
    summary_candidate: SummaryCandidate | None = None
    knowledge_extraction_result: KnowledgeExtractionResult | None = None

    # ── Persistence / final result ───────────────────────────────────
    persistence_completed: bool = False
    persistence_outcome: object | None = None
    current_span_id: str | None = None
    result: TurnResult | None = None

    # ── Context governance tracking ─────────────────────────────────
    compression_attempts: list[str] = field(default_factory=list)
    ineffective_compression_count: int = 0

    # ── Failure information ──────────────────────────────────────────
    error: BaseException | None = None

    # ── Limited extension area (see spec §2.5) ──────────────────────
    metadata: dict[str, object] = field(default_factory=dict)
