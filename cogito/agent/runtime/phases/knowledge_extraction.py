# cogito/agent/runtime/phases/knowledge_extraction.py
#
# KnowledgeExtractionPhase — Phase 6 of the 8-phase pipeline.
#
# Extracts knowledge candidates from the current turn for downstream
# persistence.  Outputs are candidates (PreferenceCandidate,
# MemoryCandidate, SummaryCandidate), not direct DB writes.
#
# Design rules (see initial-framework-spec §4.6, agent-loop-spec §23.2):
#   - If the turn is WAITING_APPROVAL, this phase is a no-op (the
#     turn isn't final yet — don't extract from a "pending" state).
#   - If the turn is DENIED or CANCELLED, this phase is a no-op.
#   - All extracted data must be clearly marked as candidates with
#     confidence scores, not committed as facts.
#   - No DB writes happen here.
#   - The phase is a pure orchestrator — all business logic lives in
#     KnowledgeExtractionService.

from __future__ import annotations

import asyncio
import logging

from cogito.agent.domain.knowledge.extraction import KnowledgeExtractionResult
from cogito.agent.ports.knowledge_extraction import RuntimeEventEmitter
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import (
    KnowledgeExtractionInvariantError,
    RecoverableKnowledgeExtractionError,
)
from cogito.agent.runtime.extraction.service import KnowledgeExtractionService
from cogito.agent.runtime.models import TurnStatus
from cogito.agent.runtime.phase import BasePhase

logger = logging.getLogger(__name__)


class KnowledgeExtractionPhase(BasePhase):
    """Extract knowledge candidates from the current turn.

    Responsibilities:
    - Validate preconditions (turn_id, output_text non-None).
    - Invoke KnowledgeExtractionService to run the extraction pipeline.
    - Atomically write the result to TurnContext fields.
    - Emit a knowledge_extracted lifecycle event.
    - On recoverable errors (timeout, unavailable), produce a DEGRADED
      result that preserves any already-successful rule candidates.

    Skips extraction when:
    - status is WAITING_APPROVAL (turn not final).
    - final_response is None (no agent output to analyse).
    """

    name = "knowledge_extraction"

    def __init__(
        self,
        *,
        service: KnowledgeExtractionService,
        event_emitter: RuntimeEventEmitter,
    ) -> None:
        self._service = service
        self._event_emitter = event_emitter

    async def execute(self, ctx: TurnContext) -> None:
        # ── Skip conditions ───────────────────────────────────────────
        if ctx.status is TurnStatus.WAITING_APPROVAL:
            return

        if ctx.final_response is None:
            return

        # ── Precondition validation ───────────────────────────────────
        self._validate_preconditions(ctx)

        # ── Run extraction pipeline ───────────────────────────────────
        try:
            result = await self._service.extract(ctx)
        except asyncio.CancelledError:
            raise
        except RecoverableKnowledgeExtractionError as exc:
            logger.warning(
                "Knowledge extraction degraded: %s",
                exc,
                extra={
                    "turn_id": ctx.turn_id,
                    "request_id": ctx.request.request_id,
                },
            )
            result = self._build_degraded_result(ctx, exc)
        except KnowledgeExtractionInvariantError:
            raise
        except Exception:
            logger.exception(
                "Unexpected error in knowledge extraction",
                extra={
                    "turn_id": ctx.turn_id,
                    "request_id": ctx.request.request_id,
                },
            )
            raise

        # ── Atomic write to context ───────────────────────────────────
        ctx.preference_candidates = list(result.preference_candidates)
        ctx.memory_candidates = list(result.memory_candidates)
        ctx.summary_candidate = result.summary_candidate
        ctx.knowledge_extraction_result = result

        # ── Emit event ────────────────────────────────────────────────
        try:
            await self._event_emitter.emit_knowledge_extracted(
                ctx=ctx,
                result=result,
            )
        except Exception:
            logger.exception("Failed to emit knowledge_extracted event")

    @staticmethod
    def _validate_preconditions(ctx: TurnContext) -> None:
        if ctx.turn_id is None:
            raise KnowledgeExtractionInvariantError(
                "turn_id is required before knowledge extraction",
                safe_message="运行状态不完整",
            )

        if ctx.output_text is None:
            raise KnowledgeExtractionInvariantError(
                "output_text is required before knowledge extraction",
                safe_message="最终响应尚未生成",
            )

    @staticmethod
    def _build_degraded_result(
        ctx: TurnContext,
        exc: RecoverableKnowledgeExtractionError,
    ) -> KnowledgeExtractionResult:
        """Build a degraded result when the extraction service fails.

        Returns an empty result so that PersistencePhase can at least
        persist the user message and assistant response, even though
        no candidates were extracted.
        """
        from cogito.agent.domain.knowledge.enums import ExtractionRunStatus
        from cogito.agent.domain.knowledge.extraction import ExtractionDiagnostics, KnowledgeExtractionResult

        return KnowledgeExtractionResult(
            status=ExtractionRunStatus.DEGRADED,
            preference_candidates=(),
            memory_candidates=(),
            summary_candidate=None,
            dropped_count=0,
            diagnostics=ExtractionDiagnostics(
                duration_ms=0,
                model_calls=0,
                warnings=(exc.code,),
            ),
        )
