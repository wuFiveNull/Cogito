"""Relevance 启发式评分 + 决策测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cogito.service.relevance import decide, score_relevance


class TestScoreRelevance:
    NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)

    def test_keyword_match_increases_score(self):
        s1 = score_relevance("AI breakthrough", "new model", self.NOW, ["AI", "Python"])
        s2 = score_relevance("Cooking recipe", "food", self.NOW, ["AI", "Python"])
        assert s1 > s2

    def test_multiple_keywords_higher(self):
        s1 = score_relevance("AI and Python", "code", self.NOW, ["AI", "Python"])
        s2 = score_relevance("AI only", "text", self.NOW, ["AI", "Python"])
        assert s1 > s2

    def test_recency_newer_higher(self):
        fresh = score_relevance("T", "S", self.NOW, [])
        old = score_relevance("T", "S", self.NOW - timedelta(days=7), [])
        assert fresh > old

    def test_no_interests_neutral_keyword(self):
        # 显式注入 now=self.NOW，消除 test 对真实当前时间的依赖（M0 基线修复）
        s = score_relevance("anything", "text", self.NOW, [], now=self.NOW)
        # 无兴趣时 keyword_score=0.3, recency=1.0 → 0.6*0.3+0.4*1.0 = 0.58
        assert 0.5 < s < 0.7

    def test_no_published_at_neutral_recency(self):
        s = score_relevance("AI", "text", None, ["AI"])
        # recency=0.5, keyword=1.0/1 → 0.6*1.0+0.4*0.5 = 0.8
        assert 0.7 < s < 0.9

    def test_score_bounded_0_to_1(self):
        s = score_relevance("AI Python Agent", "match all", self.NOW, ["AI", "Python", "Agent"])
        assert 0.0 <= s <= 1.0

    def test_old_content_decays(self):
        very_old = score_relevance("AI", "text", self.NOW - timedelta(days=365), ["AI"])
        recent = score_relevance("AI", "text", self.NOW, ["AI"])
        assert very_old < recent


class TestDecide:
    def test_digest_when_above_threshold(self):
        assert decide(0.5, 0.4) == "digest"

    def test_silent_when_below_threshold(self):
        assert decide(0.3, 0.4) == "silent"

    def test_equal_to_threshold_is_digest(self):
        assert decide(0.4, 0.4) == "digest"

    def test_custom_threshold(self):
        assert decide(0.6, 0.7) == "silent"
        assert decide(0.8, 0.7) == "digest"
