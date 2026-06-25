# cogito/agent/domain/knowledge/config.py
#
# KnowledgeExtractionConfig — configuration for the knowledge extraction
# phase and its sub-components.
#
# Design rules (see KnowledgeExtractionPhase-spec §9):
#   - All limits have explicit defaults so that minimal construction works.
#   - Configuration is injected at Composition Root, never read from
#     global environment variables inside the phase.
#   - minimum_candidate_confidence: below this → discard.
#   - tentative_confidence_threshold: below this → TENTATIVE instead of
#     INSERT/UPDATE/DELETE.
#   - explicit_auto_apply_threshold: above this and EXPLICIT → retain
#     the candidate's original operation.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class KnowledgeExtractionConfig:
    """Configuration for the KnowledgeExtractionPhase.

    All timing values are in seconds unless otherwise noted.
    """

    # ── Global toggle ──────────────────────────────────────────────────
    enabled: bool = True

    # ── Input trimming ─────────────────────────────────────────────────
    max_user_text_chars: int = 16_000
    max_assistant_text_chars: int = 24_000
    max_context_items: int = 12
    max_context_chars: int = 12_000

    # ── Candidate limits ───────────────────────────────────────────────
    max_preferences: int = 12
    max_memories: int = 12

    # ── Model extraction ───────────────────────────────────────────────
    extraction_timeout_seconds: float = 12.0
    malformed_output_retries: int = 1

    # ── Confidence thresholds ──────────────────────────────────────────
    minimum_candidate_confidence: float = 0.55
    tentative_confidence_threshold: float = 0.80
    explicit_auto_apply_threshold: float = 0.90

    # ── Memory relevance ──────────────────────────────────────────────
    minimum_memory_importance: float = 0.60
    summary_minimum_information_gain: float = 0.15

    # ── Feature toggles ────────────────────────────────────────────────
    allow_inferred_preferences: bool = True
    allow_sensitive_with_explicit_consent: bool = True
    emit_candidate_content_in_logs: bool = False

    # ── Candidate size limits ──────────────────────────────────────────
    max_content_length: int = 2000
    max_key_length: int = 200
    max_source_refs: int = 10
