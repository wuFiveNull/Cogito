# cogito/agent/runtime/extraction/service.py
#
# KnowledgeExtractionService — orchestrates the full extraction pipeline.
#
# Design rules (see KnowledgeExtractionPhase-spec §17):
#   - Each step in the pipeline is a separate, testable component.
#   - The service runs steps in a fixed order; it does NOT reorder
#     or skip steps based on content.
#   - Model extraction is attempted only when eligibility says so.
#   - All intermediate results are built as local variables; only
#     the final KnowledgeExtractionResult is written to TurnContext
#     (the phase handles the writing).
#   - Timeout and parse errors on the model path produce DEGRADED
#     status, not a full pipeline failure.

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig
from cogito.agent.domain.knowledge.enums import ExtractionRunStatus
from cogito.agent.domain.knowledge.extraction import (
    ExtractionDiagnostics,
    KnowledgeExtractionInput,
    KnowledgeExtractionResult,
    RawKnowledgeExtraction,
)
from cogito.agent.domain.knowledge.fingerprints import (
    compute_candidate_fingerprint,
    compute_candidate_id,
    compute_summary_fingerprint,
)
from cogito.agent.domain.memory import MemoryCandidate, SummaryCandidate
from cogito.agent.domain.preferences import CandidateOperation, PreferenceCandidate
from cogito.agent.ports.clock import ClockPort
from cogito.agent.ports.knowledge_extraction import KnowledgeExtractorPort
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.extraction.confidence import ConfidenceCalibrator
from cogito.agent.runtime.extraction.conflict import CandidateConflictResolver
from cogito.agent.runtime.extraction.deduplicator import CandidateDeduplicator
from cogito.agent.runtime.extraction.eligibility import ExtractionEligibilityEvaluator
from cogito.agent.runtime.extraction.input_builder import ExtractionInputBuilder
from cogito.agent.runtime.extraction.normalizer import CandidateNormalizer
from cogito.agent.runtime.extraction.parser import StrictRawExtractionParser
from cogito.agent.runtime.extraction.rule_extractor import DeterministicRuleExtractor
from cogito.agent.runtime.extraction.sensitivity import SensitivityPolicy
from cogito.agent.runtime.extraction.summary import SummaryCandidateBuilder
from cogito.agent.runtime.extraction.validator import CandidateValidator
from cogito.agent.runtime.errors import (
    KnowledgeExtractionTimeoutError,
    RecoverableKnowledgeExtractionError,
)

logger = logging.getLogger(__name__)


def merge_raw(a: RawKnowledgeExtraction, b: RawKnowledgeExtraction) -> RawKnowledgeExtraction:
    """Merge two RawKnowledgeExtraction objects, concatenating lists."""
    return RawKnowledgeExtraction(
        preferences=a.preferences + b.preferences,
        memories=a.memories + b.memories,
        summary=a.summary or b.summary,
    )


def _candidate_operation_from_str(op: str) -> CandidateOperation:
    """Map a raw operation string to a CandidateOperation enum."""
    mapping = {
        "insert": CandidateOperation.INSERT,
        "update": CandidateOperation.UPDATE,
        "delete": CandidateOperation.DELETE,
        "ignore": CandidateOperation.IGNORE,
        "tentative": CandidateOperation.TENTATIVE,
    }
    return mapping.get(op, CandidateOperation.TENTATIVE)


def build_result(
    candidates_raw: RawKnowledgeExtraction,
    summary_candidate: SummaryCandidate | None,
    started_at: datetime,
    completed_at: datetime,
    model_calls: int,
    warnings: list[str],
    config: KnowledgeExtractionConfig,
    extraction_input: KnowledgeExtractionInput,
) -> KnowledgeExtractionResult:
    """Build the final KnowledgeExtractionResult from processed candidates.

    This converts Raw* candidates to domain candidates (PreferenceCandidate,
    MemoryCandidate) and assigns stable fingerprints and IDs.
    """
    actor_id = extraction_input.actor_id
    rule_count = 0
    dropped_by_reason: dict[str, int] = {}

    # ── Convert preferences ──────────────────────────────────────────
    prefs: list[PreferenceCandidate] = []
    for raw in candidates_raw.preferences:
        confidence = max(0.0, min(1.0, raw.confidence))

        # Apply threshold
        if confidence < config.minimum_candidate_confidence:
            dropped_by_reason["low_confidence"] = dropped_by_reason.get("low_confidence", 0) + 1
            continue

        # Generate fingerprint
        fp = compute_candidate_fingerprint(
            actor_id=actor_id,
            kind="preference",
            canonical_key=raw.key,
            canonical_value=str(raw.value or ""),
            primary_source_id=raw.source_id or extraction_input.turn_id,
        )
        cid = compute_candidate_id(fp)
        rule_count += 1

        prefs.append(PreferenceCandidate(
            key=raw.key,
            operation=_candidate_operation_from_str(raw.operation),
            confidence=confidence,
            candidate_id=cid,
            value=raw.value,
            content=raw.content or raw.key,
            importance=0.5,
            source_refs=(raw.source_id,) if raw.source_id else (extraction_input.turn_id,),
        ))

    # ── Convert memories ─────────────────────────────────────────────
    mems: list[MemoryCandidate] = []
    for raw in candidates_raw.memories:
        if raw.importance < config.minimum_memory_importance:
            dropped_by_reason["low_importance"] = dropped_by_reason.get("low_importance", 0) + 1
            continue

        confidence = max(0.0, min(1.0, raw.confidence))
        if confidence < config.minimum_candidate_confidence:
            dropped_by_reason["low_confidence"] = dropped_by_reason.get("low_confidence", 0) + 1
            continue

        # Memory fingerprints use memory_key + content
        fp = compute_candidate_fingerprint(
            actor_id=actor_id,
            kind="memory",
            canonical_key=raw.memory_key or "memory",
            canonical_value=raw.content[:100],
            primary_source_id=raw.source_id or extraction_input.turn_id,
        )
        cid = compute_candidate_id(fp)
        rule_count += 1

        mems.append(MemoryCandidate(
            content=raw.content,
            confidence=confidence,
            importance=raw.importance,
            candidate_id=cid,
            memory_type=raw.memory_type,
            memory_key=raw.memory_key,
            operation=raw.operation,
            source_refs=(raw.source_id,) if raw.source_id else (extraction_input.turn_id,),
        ))

    # ── Assign summary fingerprint ───────────────────────────────────
    if summary_candidate:
        summary_fp = compute_summary_fingerprint(
            session_id=extraction_input.session_id,
            base_version=None,
            covered_turn_ids=(extraction_input.turn_id,),
            normalised_content=summary_candidate.content,
        )
        summary_candidate = SummaryCandidate(
            content=summary_candidate.content,
            confidence=summary_candidate.confidence,
            candidate_id=compute_candidate_id(summary_fp),
            expected_version=summary_candidate.expected_version,
            source_refs=summary_candidate.source_refs,
        )

    # ── Apply max limits ─────────────────────────────────────────────
    if len(prefs) > config.max_preferences:
        dropped_by_reason["max_preferences"] = len(prefs) - config.max_preferences
        prefs = prefs[: config.max_preferences]

    if len(mems) > config.max_memories:
        dropped_by_reason["max_memories"] = len(mems) - config.max_memories
        mems = mems[: config.max_memories]

    total_dropped = sum(dropped_by_reason.values())

    duration_ms = int((completed_at - started_at).total_seconds() * 1000)
    diagnostics = ExtractionDiagnostics(
        duration_ms=duration_ms,
        model_calls=model_calls,
        rule_candidate_count=rule_count,
        accepted_count=len(prefs) + len(mems) + (1 if summary_candidate else 0),
        dropped_by_reason=dict(dropped_by_reason),
        warnings=tuple(warnings),
    )

    return KnowledgeExtractionResult(
        status=ExtractionRunStatus.SUCCEEDED,
        preference_candidates=tuple(prefs),
        memory_candidates=tuple(mems),
        summary_candidate=summary_candidate,
        dropped_count=total_dropped,
        diagnostics=diagnostics,
    )


class KnowledgeExtractionService:
    """Orchestrate the full knowledge extraction pipeline.

    This service owns the pipeline steps and runs them in a fixed order.
    It does NOT write to TurnContext (the Phase does that).
    """

    def __init__(
        self,
        *,
        config: KnowledgeExtractionConfig,
        input_builder: ExtractionInputBuilder,
        eligibility: ExtractionEligibilityEvaluator,
        rule_extractor: DeterministicRuleExtractor,
        structured_extractor: KnowledgeExtractorPort | None,
        parser: StrictRawExtractionParser,
        normalizer: CandidateNormalizer,
        validator: CandidateValidator,
        sensitivity_policy: SensitivityPolicy,
        conflict_resolver: CandidateConflictResolver,
        confidence_calibrator: ConfidenceCalibrator,
        deduplicator: CandidateDeduplicator,
        summary_builder: SummaryCandidateBuilder,
        clock: ClockPort,
    ) -> None:
        self._config = config
        self._input_builder = input_builder
        self._eligibility = eligibility
        self._rule_extractor = rule_extractor
        self._structured_extractor = structured_extractor
        self._parser = parser
        self._normalizer = normalizer
        self._validator = validator
        self._sensitivity_policy = sensitivity_policy
        self._conflict_resolver = conflict_resolver
        self._confidence_calibrator = confidence_calibrator
        self._deduplicator = deduplicator
        self._summary_builder = summary_builder
        self._clock = clock

    async def extract(
        self,
        ctx: TurnContext,
    ) -> KnowledgeExtractionResult:
        """Run the full extraction pipeline for this turn.

        This method is safe to call even when the Phase would skip —
        the skips are checked by the Phase, not the Service.

        Args:
            ctx: The current turn context.

        Returns:
            A validated KnowledgeExtractionResult ready to be written
            to TurnContext.

        Raises:
            KnowledgeExtractionInvariantError: On missing preconditions.
            RecoverableKnowledgeExtractionError: On model failures
                (caller should produce a DEGRADED result).
        """
        started_at = self._clock.now()

        # 1. Build extraction input
        extraction_input = self._input_builder.build(ctx)

        # 2. Rule extraction (always runs)
        rule_raw = self._rule_extractor.extract(extraction_input)

        # 3. Model extraction (conditional)
        model_raw = RawKnowledgeExtraction()
        model_calls = 0
        warnings: list[str] = []

        if (
            self._structured_extractor is not None
            and self._eligibility.should_call_model(extraction_input)
        ):
            try:
                model_calls = 1
                model_raw = await asyncio.wait_for(
                    self._structured_extractor.extract(extraction_input),
                    timeout=self._config.extraction_timeout_seconds,
                )
            except asyncio.TimeoutError:
                warnings.append("model_timeout")
                raise RecoverableKnowledgeExtractionError(
                    "knowledge extractor timed out",
                    safe_message="知识抽取超时",
                ) from None
            except RecoverableKnowledgeExtractionError:
                # Allow the caller (Phase) to handle degraded mode
                warnings.append("model_unavailable")
                raise

        # 4. Merge rule + model results
        combined_raw = merge_raw(rule_raw, model_raw)

        # 5. Pipeline: parse → normalise → validate → sensitivity → conflict → confidence → dedup
        parsed = self._parser.parse(combined_raw)
        normalised = self._normalizer.normalize(parsed)
        validated = self._validator.validate(
            candidates=normalised,
            extraction_input=extraction_input,
        )
        safe = self._sensitivity_policy.apply(
            candidates=validated,
            extraction_input=extraction_input,
        )
        resolved = self._conflict_resolver.resolve(
            candidates=safe,
            extraction_input=extraction_input,
        )
        calibrated = self._confidence_calibrator.calibrate(resolved)
        deduplicated = self._deduplicator.deduplicate(
            calibrated,
            extraction_input=extraction_input,
        )

        # 6. Build summary candidate
        summary_candidate = self._summary_builder.build(
            extraction_input=extraction_input,
            accepted_candidates=deduplicated,
        )

        # 7. Build final result (convert Raw* → domain models, add fingerprints)
        completed_at = self._clock.now()
        result = build_result(
            candidates_raw=deduplicated,
            summary_candidate=summary_candidate,
            started_at=started_at,
            completed_at=completed_at,
            model_calls=model_calls,
            warnings=warnings,
            config=self._config,
            extraction_input=extraction_input,
        )

        return result
