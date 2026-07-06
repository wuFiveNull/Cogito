# cogito/agent/domain/knowledge/extraction.py
#
# Data models for the knowledge extraction pipeline.
#
# Design rules (see KnowledgeExtractionPhase-spec §6.8–6.9, §8.1–8.2):
#   - KnowledgeExtractionInput is the prebuilt input for extractors.
#   - RawKnowledgeExtraction is the unvalidated output from an extractor.
#   - KnowledgeExtractionResult is the final, validated aggregate that
#     gets written to TurnContext.
#   - ExtractionDiagnostics carries only counts and safe messages — no
#     candidate content, no user text, no keys.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from cogito.agent.domain.knowledge.enums import ExtractionRunStatus
from cogito.agent.domain.memory import MemoryCandidate, SummaryCandidate
from cogito.agent.domain.preferences import PreferenceCandidate


# ── Input ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExtractionContextItem:
    """A single context item provided alongside the main turn texts."""

    source_id: str
    text: str


@dataclass(frozen=True, slots=True)
class KnowledgeExtractionInput:
    """Fully prepared input for a knowledge extractor.

    This is built by ExtractionInputBuilder from TurnContext and never
    contains the full conversation history.
    """

    turn_id: str
    request_id: str
    actor_id: str
    session_id: str
    user_text: str
    assistant_text: str
    current_preferences: tuple[PreferenceCandidate, ...] = ()
    locale: str | None = None


# ── Raw output from an extractor ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RawPreference:
    key: str
    value: str | None
    operation: str
    confidence: float
    content: str
    evidence_text: str = ""
    source_id: str = ""


@dataclass(frozen=True, slots=True)
class RawMemory:
    content: str
    memory_key: str
    memory_type: str
    operation: str
    confidence: float
    importance: float
    evidence_text: str = ""
    source_id: str = ""


@dataclass(frozen=True, slots=True)
class RawSummary:
    content: str
    confidence: float
    covered_turn_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RawKnowledgeExtraction:
    """Unvalidated, unfiltered output from a knowledge extractor.

    All fields default to empty so that partial / failed extractions
    merge cleanly with rule-based extraction results.
    """

    preferences: tuple[RawPreference, ...] = ()
    memories: tuple[RawMemory, ...] = ()
    summary: RawSummary | None = None


# ── Final validated result ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExtractionDiagnostics:
    """Diagnostic metadata for a knowledge-extraction run.

    Contains only counts and safe message codes — never candidate
    content, user text, or sensitive data.
    """

    duration_ms: int = 0
    model_calls: int = 0
    rule_candidate_count: int = 0
    accepted_count: int = 0
    dropped_by_reason: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeExtractionResult:
    """The final, validated aggregate that gets written to TurnContext."""

    status: ExtractionRunStatus
    preference_candidates: tuple[PreferenceCandidate, ...]
    memory_candidates: tuple[MemoryCandidate, ...]
    summary_candidate: SummaryCandidate | None
    dropped_count: int
    diagnostics: ExtractionDiagnostics
