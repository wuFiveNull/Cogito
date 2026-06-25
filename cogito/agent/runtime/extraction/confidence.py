# cogito/agent/runtime/extraction/confidence.py
#
# ConfidenceCalibrator — recalibrates candidate confidence based on
# assertion mode and evidence quality.
#
# Design rules (see KnowledgeExtractionPhase-spec §10.11):
#   - Model-supplied confidence is treated as a suggestion only.
#   - Each rule pattern maps to a base confidence score.
#   - Final value is clamped to [0.0, 1.0].

from __future__ import annotations

from cogito.agent.domain.knowledge.extraction import (
    RawKnowledgeExtraction,
    RawMemory,
    RawPreference,
)


class ConfidenceCalibrator:
    """Recalibrate candidate confidence scores.

    This is the last step before deduplication.  It ensures that
    confidence scores are consistent regardless of their source
    (rule vs model).

    Thread-safety: stateless.
    """

    def calibrate(self, candidates: RawKnowledgeExtraction) -> RawKnowledgeExtraction:
        """Recalibrate all candidates.

        Args:
            candidates: Resolved (but uncalibrated) candidates.

        Returns:
            RawKnowledgeExtraction with calibrated confidence scores.
        """
        calibrated_prefs = tuple(
            self._calibrate_pref(p) for p in candidates.preferences
        )
        calibrated_mems = tuple(
            self._calibrate_mem(m) for m in candidates.memories
        )

        return RawKnowledgeExtraction(
            preferences=calibrated_prefs,
            memories=calibrated_mems,
            summary=candidates.summary,
        )

    def _calibrate_pref(self, pref: RawPreference) -> RawPreference:
        """Calibrate a single preference candidate's confidence."""
        calibrated = pref.confidence

        # Rule-based base scores are already set — just clamp
        calibrated = max(0.0, min(1.0, calibrated))

        # Reject impossibly low confidence
        if calibrated < 0.01:
            calibrated = 0.0

        return RawPreference(
            key=pref.key,
            value=pref.value,
            operation=pref.operation,
            confidence=calibrated,
            content=pref.content,
            evidence_text=pref.evidence_text,
            source_id=pref.source_id,
        )

    def _calibrate_mem(self, mem: RawMemory) -> RawMemory:
        calibrated = max(0.0, min(1.0, mem.confidence))
        importance = max(0.0, min(1.0, mem.importance))

        return RawMemory(
            content=mem.content,
            memory_key=mem.memory_key,
            memory_type=mem.memory_type,
            operation=mem.operation,
            confidence=calibrated,
            importance=importance,
            evidence_text=mem.evidence_text,
            source_id=mem.source_id,
        )
