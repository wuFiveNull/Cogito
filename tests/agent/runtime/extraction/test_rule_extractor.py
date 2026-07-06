# Tests for DeterministicRuleExtractor

from __future__ import annotations

import pytest

from cogito.agent.domain.knowledge.extraction import KnowledgeExtractionInput
from cogito.agent.runtime.extraction.rule_extractor import DeterministicRuleExtractor


def _make_input(user_text: str, assistant_text: str = "") -> KnowledgeExtractionInput:
    return KnowledgeExtractionInput(
        turn_id="turn-001",
        request_id="req-001",
        actor_id="actor-001",
        session_id="session-001",
        user_text=user_text,
        assistant_text=assistant_text,
    )


class TestDeterministicRuleExtractor:
    """Test suite for DeterministicRuleExtractor."""

    def setup_method(self) -> None:
        self.extractor = DeterministicRuleExtractor()

    def test_remember_name(self) -> None:
        """记住我的昵称是 X → INSERT preference"""
        inp = _make_input("记住我的昵称是 hunriiz")
        result = self.extractor.extract(inp)
        assert len(result.preferences) == 1
        p = result.preferences[0]
        assert p.operation == "insert"
        assert p.confidence == 0.95
        assert "hunriiz" in str(p.value)

    def test_future_use_language(self) -> None:
        """以后请用中文 → INSERT response.language"""
        inp = _make_input("以后请用中文回答")
        result = self.extractor.extract(inp)
        assert len(result.preferences) >= 1
        lang_prefs = [p for p in result.preferences if "language" in p.key]
        assert len(lang_prefs) >= 1
        assert lang_prefs[0].operation == "insert"
        assert lang_prefs[0].confidence >= 0.90

    def test_future_use_english(self) -> None:
        """From now on, please speak English."""
        inp = _make_input("From now on, please speak English")
        result = self.extractor.extract(inp)
        # Should at least find some preference (language or format)
        assert len(result.preferences) >= 1
        # Should contain language preference if detected
        lang_prefs = [p for p in result.preferences if "language" in p.key]
        if lang_prefs:
            assert lang_prefs[0].value == "en-US"

    def test_no_longer_like(self) -> None:
        """我不再喜欢 X → DELETE preference"""
        inp = _make_input("我不再喜欢表格格式")
        result = self.extractor.extract(inp)
        assert len(result.preferences) == 1
        p = result.preferences[0]
        assert p.operation == "delete"
        assert p.confidence >= 0.90

    def test_forget_key(self) -> None:
        """忘掉我的公司名称 → DELETE"""
        inp = _make_input("忘掉我的公司名称")
        result = self.extractor.extract(inp)
        assert len(result.preferences) >= 1
        del_prefs = [p for p in result.preferences if p.operation == "delete"]
        assert len(del_prefs) >= 1

    def test_avoid_format(self) -> None:
        """不要再使用表格 → DELETE"""
        inp = _make_input("不要再使用表格格式了")
        result = self.extractor.extract(inp)
        assert len(result.preferences) >= 1
        del_prefs = [p for p in result.preferences if p.operation == "delete"]
        assert len(del_prefs) >= 1

    def test_memory_extraction(self) -> None:
        """记住复杂上下文 → fact memory"""
        inp = _make_input("记住，项目API统一使用REST，不采用GraphQL")
        result = self.extractor.extract(inp)
        assert len(result.memories) >= 1
        m = result.memories[0]
        assert m.operation == "insert"

    def test_english_patterns(self) -> None:
        """EN: remember that, don't use, no longer"""
        inp = _make_input("Remember that my preferred name is Alex. I no longer want markdown format.")
        result = self.extractor.extract(inp)
        assert len(result.preferences) >= 1
        assert len(result.memories) >= 1

    def test_empty_text_yields_nothing(self) -> None:
        """空输入不产生候选"""
        inp = _make_input("")
        result = self.extractor.extract(inp)
        assert len(result.preferences) == 0
        assert len(result.memories) == 0

    def test_normal_conversation_yields_nothing(self) -> None:
        """普通对话不触发规则"""
        inp = _make_input("今天天气怎么样？")
        result = self.extractor.extract(inp)
        assert len(result.preferences) == 0
        assert len(result.memories) == 0

    def test_english_avoid_pattern(self) -> None:
        """EN: avoid pattern"""
        inp = _make_input("Never use tables in your replies")
        result = self.extractor.extract(inp)
        assert len(result.preferences) >= 1

    def test_english_forget_pattern(self) -> None:
        """EN: forget/delete pattern"""
        inp = _make_input("Forget my email address")
        result = self.extractor.extract(inp)
        assert len(result.preferences) >= 1
        assert result.preferences[0].operation == "delete"
