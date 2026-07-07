"""Tests for QueryPlan (E1: 移除规则化语义判断)。

验证 build_query_plan 不再做关键词推断，直接保留原始 query。
"""

from cogito.store.query_plan import QueryPlan, build_query_plan


class TestQueryPlan:
    def test_empty_query(self):
        plan = build_query_plan("")
        assert plan.query_text == ""

    def test_basic_query(self):
        plan = build_query_plan("Python")
        assert plan.query_text == "Python"
        assert plan.kinds == []
        assert plan.time_range_days == 0

    def test_no_preference_keyword_inference(self):
        """E1: 不再根据关键词猜测 preference。"""
        plan = build_query_plan("用户喜欢的编程语言")
        assert plan.kinds == []  # 不推断

    def test_no_constraint_keyword_inference(self):
        plan = build_query_plan("不要自动发送消息")
        assert plan.kinds == []

    def test_no_goal_keyword_inference(self):
        plan = build_query_plan("本季度的目标")
        assert plan.kinds == []

    def test_no_time_range_inference(self):
        plan = build_query_plan("最近的项目决策")
        assert plan.time_range_days == 0

    def test_quoted_text_preserved(self):
        plan = build_query_plan('精确短语 "hello world" 测试')
        assert "hello world" in plan.query_text

    def test_preserves_original_query(self):
        """original_query 应与原始输入一致。"""
        plan = build_query_plan("  hello  ")
        assert plan.original_query == "  hello  "
        assert plan.query_text == "hello"  # strip 后

    def test_has_all_fields(self):
        plan = build_query_plan("test")
        assert hasattr(plan, "needs_episodic")
        assert hasattr(plan, "needs_procedure")
        assert hasattr(plan, "original_query")
