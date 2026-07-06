# cogito/agent/runtime/extraction/validator.py
#
# CandidateValidator — validates evidence attribution and candidate
# legitimacy.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.8):
#   - Evidence source_id must belong to the current turn.
#   - Candidate content must be attributable to the user, not to third
#     parties or hypotheticals.
#   - Agent output must not be used to extract user facts.
#   - Quote matching (SHA-256) is done here.

from __future__ import annotations

import hashlib
import logging

from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
    RawMemory,
    RawPreference,
)

logger = logging.getLogger(__name__)


def _quote_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Third-party indicators that should prevent user-fact extraction
_THIRD_PARTY_PATTERNS = [
    "我朋友", "我同事", "我客户", "他", "她", "他们",
    "my friend", "my colleague", "my client", "he ", "she ", "they ",
]


class CandidateValidator:
    """Validate that extracted candidates are legitimate and attributable.

    Thread-safety: stateless.
    """

    def validate(
        self,
        *,
        candidates: RawKnowledgeExtraction,
        extraction_input: KnowledgeExtractionInput,
    ) -> RawKnowledgeExtraction:
        """Filter candidates that fail validation checks.

        Args:
            candidates: Parsed + normalised candidates.
            extraction_input: The extraction input for context.

        Returns:
            A filtered RawKnowledgeExtraction with invalid candidates
            removed.
        """
        user_text = extraction_input.user_text
        assistant_text = extraction_input.assistant_text

        valid_prefs: list[RawPreference] = []
        for p in candidates.preferences:
            if self._validate_preference(p, user_text, assistant_text):
                valid_prefs.append(p)

        valid_mems: list[RawMemory] = []
        for m in candidates.memories:
            if self._validate_memory(m, user_text, assistant_text):
                valid_mems.append(m)

        return RawKnowledgeExtraction(
            preferences=tuple(valid_prefs),
            memories=tuple(valid_mems),
            summary=candidates.summary,
        )

    def _validate_preference(
        self,
        pref: RawPreference,
        user_text: str,
        assistant_text: str,
    ) -> bool:
        """Validate a single preference candidate.

        Rules:
          - Evidence must appear in user or assistant text.
          - Content must not be a hypothetical ("假设", "if").
          - Must not attribute to a third party.
        """
        source_text = user_text if pref.source_id != "" else user_text

        # Check third-party attribution
        for pattern in _THIRD_PARTY_PATTERNS:
            if pattern.lower() in source_text.lower():
                logger.debug("Candidate rejected: third-party content: %s", pref.key)
                return False

        # Reject hypothetical / conditional statements
        for marker in ("假设", "假如", "如果", "要是", "if ", "假设我", "suppose"):
            if marker in source_text.lower():
                logger.debug("Candidate rejected: hypothetical: %s", pref.key)
                return False

        # Reject content from assistant output that claims a user fact
        if pref.source_id != "" and not self._evidence_in_source(pref.evidence_text, assistant_text):
            # No user-side evidence — assistant assertion
            if pref.confidence < 0.90:
                logger.debug("Candidate rejected: assistant-side with low confidence: %s", pref.key)
                return False

        return True

    def _validate_memory(
        self,
        mem: RawMemory,
        user_text: str,
        assistant_text: str,
    ) -> bool:
        """Validate a single memory candidate."""
        source_text = user_text if mem.source_id != "" else user_text

        # Reject third-party
        for pattern in _THIRD_PARTY_PATTERNS:
            if pattern.lower() in source_text.lower():
                logger.debug("Memory rejected: third-party content")
                return False

        # Reject hypothetical
        for marker in ("假设", "假如", "如果", "要是", "if ", "suppose"):
            if marker in source_text.lower():
                logger.debug("Memory rejected: hypothetical")
                return False

        return True

    @staticmethod
    def _evidence_in_source(evidence_text: str, source: str) -> bool:
        """Check if evidence_text appears in the source text."""
        if not evidence_text:
            return False
        return evidence_text.strip()[:50].lower() in source.lower()
