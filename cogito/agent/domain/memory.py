# cogito/agent/domain/memory.py
#
# Memory and summary candidate models for knowledge extraction.
#
# Design rules (see initial-framework-spec §6.5, persistence-phase-spec §5.3–5.4):
#   - MemoryCandidate carries extracted memory candidates from
#     KnowledgeExtractionPhase to PersistencePhase.
#   - SummaryCandidate carries a session-summary update candidate.
#   - All fields are frozen/immutable.
#   - candidate_id is required for audit trail and idempotency.
#   - memory_type matches the DB column values: fact, preference, rule, event.
#   - memory_key is the natural deduplication key per user.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """Extracted memory candidate from KnowledgeExtractionPhase.

    ``candidate_id`` is a stable identifier for audit and idempotency.

    ``memory_type`` determines the kind of memory (fact / preference /
    rule / event).  It maps directly to the DB ``memories.memory_type``
    column.

    ``memory_key`` is the natural deduplication key per user, e.g.
    ``residence.city``, ``restaurant.ambience``, ``rule.payment.confirm``.

    ``operation`` must be one of ``insert``, ``update``, ``delete``,
    ``ignore``, or ``tentative``.  It encodes what the PersistencePhase
    should attempt to do.

    ``source_refs`` is a tuple of stable references (event IDs, span IDs)
    that serve as evidence for this candidate.
    """

    content: str
    confidence: float
    importance: float = 0.5
    candidate_id: str = ""
    memory_type: str = "fact"
    memory_key: str = ""
    value: object | None = None
    operation: str = "insert"
    valid_from: str | None = None
    valid_until: str | None = None
    source_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SummaryCandidate:
    """Candidate for updating the session summary.

    ``candidate_id`` is a stable identifier for audit.

    ``expected_version`` is the ``SessionSummary.version`` that the
    KnowledgeExtractionPhase read when producing this candidate.  If
    the version has changed by the time PersistencePhase runs, the
    candidate is considered stale and must be rejected.

    ``source_refs`` is a tuple of event IDs that support this summary.
    """

    content: str
    confidence: float
    candidate_id: str = ""
    expected_version: int | None = None
    source_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
