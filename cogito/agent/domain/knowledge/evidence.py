# cogito/agent/domain/knowledge/evidence.py
#
# EvidenceRef — immutable evidence reference for knowledge candidates.
#
# Design rules (see KnowledgeExtractionPhase-spec §6.2):
#   - source_type identifies the kind of source (user message, assistant
#     message, tool result, etc.).
#   - source_id is the ID of the source within the turn (message ID,
#     event ID, or span ID).
#   - quote_hash is SHA-256 of the quoted text fragment — this allows
#     audit without storing the original sensitive text.
#   - metadata must NOT contain the full original text, API keys, or
#     authentication secrets.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from cogito.agent.domain.knowledge.enums import (
    AssertionMode,
    EvidenceSourceType,
)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """An immutable evidence reference for a knowledge candidate.

    Each EvidenceRef ties a candidate back to a specific, verifiable
    source within the turn that produced it.
    """

    source_type: EvidenceSourceType
    source_id: str
    quote_hash: str
    assertion_mode: AssertionMode = AssertionMode.INFERRED
    start_offset: int | None = None
    end_offset: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
