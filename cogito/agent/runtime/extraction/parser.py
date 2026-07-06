# cogito/agent/runtime/extraction/parser.py
#
# StrictRawExtractionParser — validates and parses RawKnowledgeExtraction.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.6):
#   - Validates enum values, string lengths, numeric ranges.
#   - Rejects unknown fields (via strict field checking).
#   - Never lets malformed model output silently become a candidate.
#   - Returns safe error descriptions, not model raw output text.

from __future__ import annotations

import logging

from cogito.agent.domain.knowledge.extraction import (
    RawKnowledgeExtraction,
    RawMemory,
    RawPreference,
)

logger = logging.getLogger(__name__)

_VALID_OPERATIONS = frozenset({"insert", "update", "delete", "ignore", "tentative"})
_VALID_MEMORY_TYPES = frozenset({"fact", "preference", "rule", "event"})
_MAX_CONTENT_LENGTH = 2000
_MAX_KEY_LENGTH = 200
_MAX_CONFIDENCE = 1.0
_MIN_CONFIDENCE = 0.0


class StrictRawExtractionParser:
    """Validate and parse a RawKnowledgeExtraction.

    This parser enforces domain invariants.  It does NOT modify
    candidate values — that is the job of CandidateNormalizer.
    """

    def parse(self, raw: RawKnowledgeExtraction) -> RawKnowledgeExtraction:
        """Parse and validate a raw extraction result.

        Invalid candidates are silently dropped (logged at DEBUG level).
        The parser never raises — invalid input simply yields fewer
        candidates.

        Args:
            raw: The raw extraction output to validate.

        Returns:
            A filtered RawKnowledgeExtraction containing only valid
            candidates.
        """
        valid_prefs = [p for p in raw.preferences if self._validate_preference(p)]
        valid_mems = [m for m in raw.memories if self._validate_memory(m)]
        valid_summary = raw.summary

        # Summary validation
        if valid_summary is not None:
            if not valid_summary.content or len(valid_summary.content) > _MAX_CONTENT_LENGTH:
                valid_summary = None

        return RawKnowledgeExtraction(
            preferences=tuple(valid_prefs),
            memories=tuple(valid_mems),
            summary=valid_summary,
        )

    def _validate_preference(self, pref: RawPreference) -> bool:
        if not pref.key or len(pref.key) > _MAX_KEY_LENGTH:
            logger.debug("Dropping preference: invalid key %r", pref.key)
            return False

        if pref.operation not in _VALID_OPERATIONS:
            logger.debug("Dropping preference: invalid operation %r", pref.operation)
            return False

        if pref.confidence < _MIN_CONFIDENCE or pref.confidence > _MAX_CONFIDENCE:
            logger.debug("Dropping preference: confidence out of range %r", pref.confidence)
            return False

        if pref.content and len(pref.content) > _MAX_CONTENT_LENGTH:
            logger.debug("Dropping preference: content too long")
            return False

        return True

    def _validate_memory(self, mem: RawMemory) -> bool:
        if not mem.content or len(mem.content) > _MAX_CONTENT_LENGTH:
            logger.debug("Dropping memory: invalid content")
            return False

        if mem.memory_type not in _VALID_MEMORY_TYPES:
            logger.debug("Dropping memory: invalid type %r", mem.memory_type)
            return False

        if mem.operation not in _VALID_OPERATIONS:
            logger.debug("Dropping memory: invalid operation %r", mem.operation)
            return False

        if mem.confidence < _MIN_CONFIDENCE or mem.confidence > _MAX_CONFIDENCE:
            logger.debug("Dropping memory: confidence out of range %r", mem.confidence)
            return False

        if mem.importance < 0.0 or mem.importance > 1.0:
            logger.debug("Dropping memory: importance out of range %r", mem.importance)
            return False

        return True
