# Tests for ConfidenceCalibrator

from __future__ import annotations

import pytest

from cogito.agent.domain.knowledge.extraction import (
    RawKnowledgeExtraction,
    RawPreference,
)
from cogito.agent.runtime.extraction.confidence import ConfidenceCalibrator


class TestConfidenceCalibrator:
    """Test suite for ConfidenceCalibrator."""

    def setup_method(self) -> None:
        self.calibrator = ConfidenceCalibrator()

    def test_confidence_clamped(self) -> None:
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="test", value="x", operation="insert",
                          confidence=1.5, content=""),
            RawPreference(key="test2", value="y", operation="insert",
                          confidence=-0.5, content=""),
        ))
        result = self.calibrator.calibrate(raw)
        assert result.preferences[0].confidence == 1.0
        assert result.preferences[1].confidence == 0.0

    def test_normal_confidence_preserved(self) -> None:
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="test", value="x", operation="insert",
                          confidence=0.85, content=""),
        ))
        result = self.calibrator.calibrate(raw)
        assert result.preferences[0].confidence == 0.85

    def test_very_low_confidence_rejected(self) -> None:
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="test", value="x", operation="insert",
                          confidence=0.001, content=""),
        ))
        result = self.calibrator.calibrate(raw)
        assert result.preferences[0].confidence == 0.0

    def test_memory_importance_clamped(self) -> None:
        from cogito.agent.domain.knowledge.extraction import RawMemory
        raw = RawKnowledgeExtraction(memories=(
            RawMemory(content="test", memory_key="k", memory_type="fact",
                      operation="insert", confidence=1.2, importance=1.5),
        ))
        result = self.calibrator.calibrate(raw)
        assert result.memories[0].confidence == 1.0
        assert result.memories[0].importance == 1.0
