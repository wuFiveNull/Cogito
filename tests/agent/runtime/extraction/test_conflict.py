# Tests for CandidateConflictResolver

from __future__ import annotations

import pytest

from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
    RawPreference,
)
from cogito.agent.domain.preferences import PreferenceCandidate
from cogito.agent.runtime.extraction.conflict import CandidateConflictResolver


def _make_inp(existing_keys: set[str] | None = None) -> KnowledgeExtractionInput:
    return KnowledgeExtractionInput(
        turn_id="t1", request_id="r1", actor_id="a1", session_id="s1",
        user_text="test", assistant_text="",
        current_preferences=tuple(
            PreferenceCandidate(key=k, operation="insert", confidence=1.0, content=k)
            for k in (existing_keys or set())
        ),
    )


class TestCandidateConflictResolver:
    """Test suite for CandidateConflictResolver."""

    def setup_method(self) -> None:
        self.resolver = CandidateConflictResolver()

    def test_new_value_insert(self) -> None:
        inp = _make_inp(existing_keys=set())
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="response.language", value="zh-CN",
                          operation="insert", confidence=0.95,
                          content="", evidence_text="", source_id=""),
        ))
        result = self.resolver.resolve(candidates=raw, extraction_input=inp)
        assert len(result.preferences) == 1
        assert result.preferences[0].operation == "insert"

    def test_existing_key_becomes_update(self) -> None:
        inp = _make_inp(existing_keys={"response.language"})
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="response.language", value="en-US",
                          operation="insert", confidence=0.95,
                          content="", evidence_text="", source_id=""),
        ))
        result = self.resolver.resolve(candidates=raw, extraction_input=inp)
        assert len(result.preferences) == 1
        assert result.preferences[0].operation == "update"

    def test_nonexistent_delete_becomes_ignore(self) -> None:
        inp = _make_inp(existing_keys=set())
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="response.format.table", value=None,
                          operation="delete", confidence=0.97,
                          content="", evidence_text="", source_id=""),
        ))
        result = self.resolver.resolve(candidates=raw, extraction_input=inp)
        assert result.preferences[0].operation == "ignore"

    def test_existing_delete_stays_delete(self) -> None:
        inp = _make_inp(existing_keys={"response.format.table"})
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="response.format.table", value=None,
                          operation="delete", confidence=0.97,
                          content="", evidence_text="", source_id=""),
        ))
        result = self.resolver.resolve(candidates=raw, extraction_input=inp)
        assert result.preferences[0].operation == "delete"

    def test_low_confidence_downgraded(self) -> None:
        inp = _make_inp(existing_keys=set())
        raw = RawKnowledgeExtraction(preferences=(
            RawPreference(key="custom.test", value="val",
                          operation="insert", confidence=0.60,
                          content="", evidence_text="", source_id=""),
        ))
        result = self.resolver.resolve(candidates=raw, extraction_input=inp)
        assert result.preferences[0].operation == "tentative"
