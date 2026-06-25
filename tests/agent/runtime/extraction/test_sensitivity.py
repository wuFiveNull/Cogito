# Tests for SensitivityPolicy

from __future__ import annotations

import pytest

from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig
from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
    RawPreference,
)
from cogito.agent.runtime.extraction.sensitivity import SensitivityPolicy


def _make_inp() -> KnowledgeExtractionInput:
    return KnowledgeExtractionInput(
        turn_id="t1", request_id="r1", actor_id="a1", session_id="s1",
        user_text="test", assistant_text="",
    )


class TestSensitivityPolicy:
    """Test suite for SensitivityPolicy."""

    def setup_method(self) -> None:
        self.config = KnowledgeExtractionConfig(
            enabled=True,
            allow_sensitive_with_explicit_consent=True,
        )
        self.policy = SensitivityPolicy(self.config)

    def test_api_key_removed(self) -> None:
        inp = _make_inp()
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="api_key", value="sk-test1234567890abcdef",
                          operation="insert", confidence=0.99,
                          content="api key: sk-test1234567890abcdef",
                          evidence_text="", source_id=""),
        ))
        result = self.policy.apply(candidates=raw, extraction_input=inp)
        assert len(result.preferences) == 0

    def test_password_removed(self) -> None:
        inp = _make_inp()
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="password", value="myp@ss123",
                          operation="insert", confidence=0.99,
                          content="password: myp@ss123",
                          evidence_text="", source_id=""),
        ))
        result = self.policy.apply(candidates=raw, extraction_input=inp)
        assert len(result.preferences) == 0

    def test_normal_preference_passes(self) -> None:
        inp = _make_inp()
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="response.language", value="zh-CN",
                          operation="insert", confidence=0.95,
                          content="response.language: zh-CN",
                          evidence_text="", source_id=""),
        ))
        result = self.policy.apply(candidates=raw, extraction_input=inp)
        assert len(result.preferences) == 1
