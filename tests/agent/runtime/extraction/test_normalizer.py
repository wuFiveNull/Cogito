# Tests for CandidateNormalizer

from __future__ import annotations

import pytest

from cogito.agent.domain.knowledge.extraction import (
    RawKnowledgeExtraction,
    RawPreference,
)
from cogito.agent.runtime.extraction.normalizer import CandidateNormalizer


class TestCandidateNormalizer:
    """Test suite for CandidateNormalizer."""

    def setup_method(self) -> None:
        self.normalizer = CandidateNormalizer()

    def _normalize(self, pref: RawPreference) -> RawKnowledgeExtraction:
        raw = RawKnowledgeExtraction(preferences=(pref,))
        return self.normalizer.normalize(raw)

    def test_chinese_language_mapped(self) -> None:
        result = self._normalize(RawPreference(
            key="language", value="中文", operation="insert",
            confidence=0.95, content="language: 中文",
        ))
        p = result.preferences[0]
        assert p.key == "response.language"
        assert p.value == "zh-CN"

    def test_english_language_mapped(self) -> None:
        result = self._normalize(RawPreference(
            key="language", value="English", operation="insert",
            confidence=0.95, content="",
        ))
        p = result.preferences[0]
        assert p.key == "response.language"
        assert p.value == "en-US"

    def test_verbosity_mapped(self) -> None:
        result = self._normalize(RawPreference(
            key="verbosity", value="简洁", operation="insert",
            confidence=0.90, content="",
        ))
        p = result.preferences[0]
        assert p.value == "concise"

    def test_unicode_normalized(self) -> None:
        result = self._normalize(RawPreference(
            key="	language ", value="中文", operation="insert",
            confidence=0.95, content="language: 中文",
        ))
        p = result.preferences[0]
        assert p.key

    def test_known_key_map(self) -> None:
        result = self._normalize(RawPreference(
            key="时区", value="Asia/Tokyo", operation="insert",
            confidence=0.95, content="",
        ))
        p = result.preferences[0]
        assert p.key == "timezone"

    def test_custom_key_fallback(self) -> None:
        result = self._normalize(RawPreference(
            key="my_custom_pref", value="val", operation="insert",
            confidence=0.80, content="",
        ))
        assert result.preferences[0].key

    def test_memory_preserved(self) -> None:
        from cogito.agent.domain.knowledge.extraction import RawMemory
        raw = RawKnowledgeExtraction(memories=(
            RawMemory(content="Test content", memory_key="test_key",
                      memory_type="fact", operation="insert",
                      confidence=0.85, importance=0.70),
        ))
        result = self.normalizer.normalize(raw)
        assert len(result.memories) == 1
        assert result.memories[0].content == "Test content"
