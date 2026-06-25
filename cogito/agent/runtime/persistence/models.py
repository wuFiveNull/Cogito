# cogito/agent/runtime/persistence/models.py
#
# Immutable domain models for the PersistencePhase transaction pipeline.
#
# Design rules (see persistence-phase-spec §5, §7):
#   - All models are frozen dataclasses with slots=True.
#   - PersistencePlan is the complete, immutable input to a single
#     _persist_once call.  It is built once and reused across retries.
#   - PersistenceOutcome carries the result back to TurnContext.
#   - Domain types (PreferenceCandidate, MemoryCandidate, SummaryCandidate)
#     are imported from the existing domain package.
#
# PersistencePlan does NOT carry:
#   - Database-generated values (seq_no, session version after advance)
#   - Embedded raw connections, cursors, or adapters
#   - LLM model references or tool executor references

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from cogito.agent.domain.memory import MemoryCandidate, SummaryCandidate
from cogito.agent.domain.preferences import PreferenceCandidate
from cogito.agent.domain.usage import UsageSummary


# ── Event Draft (pre-sequence-allocation) ────────────────────────────────


@dataclass(frozen=True, slots=True)
class EventDraft:
    """One event to persist, with stable IDs generated before the transaction.

    ``logical_order`` is the deterministic ordering key (0 = user message,
    10/11 = first tool pair, 10000 = assistant message).  It is converted
    to ``seq_no`` inside the transaction.

    ``extraction_status`` follows the KnowledgeExtractionPhase convention:
      - user_message / assistant_message → 'pending'
      - tool_request / tool_result / tool_error → 'ignored'
    """

    event_id: str
    user_id: str
    session_id: str
    request_id: str
    turn_id: str
    role: str
    event_type: str
    content: str
    content_json: Mapping[str, object]
    extraction_status: str = "pending"
    logical_order: int = 0


# ── Persisted Event (after sequence allocation) ────────────────────────


@dataclass(frozen=True, slots=True)
class PersistedEvent:
    """An event that has been assigned a database ``seq_no``.

    This is the type returned by the event repository's ``add_many``.
    It extends ``EventDraft`` with the sequence number and the
    current timestamp as ``created_at``.
    """

    event_id: str
    user_id: str
    session_id: str
    request_id: str
    turn_id: str
    seq_no: int
    role: str
    event_type: str
    content: str
    content_json: Mapping[str, object]
    extraction_status: str
    created_at: datetime


# ── Embedding (prepared outside the write transaction) ──────────────────


@dataclass(frozen=True, slots=True)
class PreparedEmbedding:
    """A pre-computed embedding vector ready for storage.

    ``candidate_id`` links back to the memory/preference candidate.
    ``blob`` is the float32 little-endian byte representation.
    ``format`` is always ``float32-le`` for consistency.
    """

    candidate_id: str
    model: str
    dimensions: int
    blob: bytes
    format: str = "float32-le"


# ── Session state read from DB ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Snapshot of the ``sessions`` row at a point in time.

    Used by PersistencePhase to read session version and seq_no
    before advancing the session state.
    """

    session_id: str
    user_id: str
    version: int
    next_seq_no: int
    summary_text: str | None = None
    summary_version: int = 0
    summary_updated_at: str | None = None
    last_turn_id: str | None = None
    last_request_id: str | None = None
    last_message_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


# ── PersistencePlan (immutable, retry-safe) ────────────────────────────


@dataclass(frozen=True, slots=True)
class PersistencePlan:
    """Complete, immutable plan for one turn's persistence.

    This object is built ONCE per turn (outside the retry loop) and
    reused across all retry attempts.  All IDs within it are stable.

    ``commit_fingerprint`` is a SHA-256 hex digest of the canonical
    JSON representation of the plan's semantic content.  It is used
    for idempotency detection: if a prior commit with the same
    ``(user_id, request_id, commit_fingerprint)`` exists, the phase
    returns an idempotent replay outcome instead of writing again.

    ``expected_session_version`` and ``expected_summary_version`` are
    set from the ``SessionSnapshot`` loaded by StateLoadPhase.  If
    they don't match at write time, an optimistic concurrency error
    is raised (retryable).
    """

    commit_id: str
    turn_id: str
    request_id: str
    user_id: str
    session_id: str
    persistence_span_id: str | None

    expected_session_version: int | None
    expected_summary_version: int | None

    events: tuple[EventDraft, ...]
    preference_candidates: tuple[PreferenceCandidate, ...]
    memory_candidates: tuple[MemoryCandidate, ...]
    summary_candidate: SummaryCandidate | None
    embeddings: tuple[PreparedEmbedding, ...]

    usage: UsageSummary
    started_at: datetime
    persistence_started_at: datetime
    commit_fingerprint: str


# ── Write audit entries (both in-memory and DB) ────────────────────────


@dataclass(frozen=True, slots=True)
class CandidateWriteOutcome:
    """Outcome of processing one candidate (preference / memory / summary).

    Produced by the policy services and aggregated into ``PersistenceOutcome``.
    """

    candidate_id: str
    candidate_type: str  # 'preference' | 'memory' | 'summary'
    candidate_key: str | None
    status: str  # applied_insert | applied_update | applied_delete | ...
    record_id: str | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class CandidateWriteAudit:
    """One row in ``candidate_write_audits``.

    This is the DB record form of ``CandidateWriteOutcome``, enriched
    with the commit and session context.
    """

    id: str
    commit_id: str
    user_id: str
    session_id: str
    turn_id: str
    candidate_id: str
    candidate_type: str
    candidate_key: str | None = None
    requested_operation: str = ""
    result_status: str = "ignored"
    target_record_id: str | None = None
    reason_code: str | None = None
    confidence: float | None = None
    importance: float | None = None
    source_event_ids_json: str = "[]"
    metadata_json: str = "{}"


# ── Turn Commit (DB row shape) ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TurnCommitRecord:
    """One row in ``turn_commits``.

    ``outcome_json`` is the canonical JSON of the ``PersistenceOutcome``.
    """

    commit_id: str
    user_id: str
    session_id: str
    request_id: str
    turn_id: str
    commit_fingerprint: str
    user_event_id: str
    assistant_event_id: str
    session_version: int
    outcome_json: str
    persistence_span_id: str | None = None
    committed_at: str | None = None


# ── Embedding Job (DB row shape) ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EmbeddingJob:
    """One row in ``embedding_jobs``."""

    id: str
    memory_id: str
    embedding_model: str
    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None


# ── PersistenceOutcome (final result) ──────────────────────────────────


@dataclass(frozen=True, slots=True)
class PersistenceOutcome:
    """Result of persisting one turn.

    Set on ``TurnContext.persistence_outcome`` after the transaction
    completes (or an idempotent replay is detected).
    """

    commit_id: str
    turn_id: str
    request_id: str
    session_id: str
    committed_at: datetime
    session_version: int
    summary_version: int
    idempotent_replay: bool
    user_event_id: str
    assistant_event_id: str
    tool_event_ids: tuple[str, ...] = ()
    candidate_outcomes: tuple[CandidateWriteOutcome, ...] = ()
    embedding_job_count: int = 0
