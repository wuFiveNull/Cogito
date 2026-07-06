# Tests for CandidateDeduplicator

from __future__ import annotations

import pytest

from cogito.agent.domain.knowledge.extraction import (
    RawKnowledgeExtraction,
    RawPreference,
)
from cogito.agent.runtime.extraction.deduplicator import CandidateDeduplicator


class TestCandidateDeduplicator:
    """Test suite for CandidateDeduplicator."""

    def setup_method(self) -> None:
        self.dedup = CandidateDeduplicator()

    def test_dedup_same_key_value(self) -> None:
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="response.language", value="zh-CN",
                          operation="insert", confidence=0.95, content="A"),
            RawPreference(key="response.language", value="zh-CN",
                          operation="insert", confidence=0.90, content="A"),
        ))
        result = self.dedup.deduplicate(raw)
        # Should keep the higher-confidence one
        assert len(result.preferences) == 1

    def test_different_keys_kept(self) -> None:
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="response.language", value="zh-CN",
                          operation="insert", confidence=0.95, content="A"),
            RawPreference(key="response.verbosity", value="concise",
                          operation="insert", confidence=0.90, content="B"),
        ))
        result = self.dedup.deduplicate(raw)
        assert len(result.preferences) == 2
