# Tests for CandidateValidator

from __future__ import annotations

import pytest

from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
    RawPreference,
)
from cogito.agent.runtime.extraction.validator import CandidateValidator


def _make_input(user_text: str = "") -> KnowledgeExtractionInput:
    return KnowledgeExtractionInput(
        turn_id="t1", request_id="r1", actor_id="a1", session_id="s1",
        user_text=user_text, assistant_text="",
    )


class TestCandidateValidator:
    """Test suite for CandidateValidator."""

    def setup_method(self) -> None:
        self.validator = CandidateValidator()

    def test_normal_preference_passes(self) -> None:
        inp = _make_input("记住我的昵称是张三")
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="response.language", value="zh-CN",
                          operation="insert", confidence=0.95,
                          content="response.language: zh-CN",
                          evidence_text="记住我的昵称是张三",
                          source_id="t1"),
        ))
        result = self.validator.validate(candidates=raw, extraction_input=inp)
        assert len(result.preferences) == 1

    def test_third_party_rejected(self) -> None:
        inp = _make_input("我朋友不吃肉")
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="dietary.preference", value="vegetarian",
                          operation="insert", confidence=0.90,
                          content="dietary preference: vegetarian",
                          evidence_text="我朋友不吃肉",
                          source_id="t1"),
        ))
        result = self.validator.validate(candidates=raw, extraction_input=inp)
        assert len(result.preferences) == 0

    def test_hypothetical_rejected(self) -> None:
        inp = _make_input("假设我喜欢深色模式")
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="ui.theme", value="dark",
                          operation="insert", confidence=0.85,
                          content="ui.theme: dark",
                          evidence_text="假设我喜欢深色模式",
                          source_id="t1"),
        ))
        result = self.validator.validate(candidates=raw, extraction_input=inp)
        assert len(result.preferences) == 0
