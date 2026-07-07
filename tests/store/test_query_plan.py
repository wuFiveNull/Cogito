"""Tests for QueryPlan — 检索查询计划构建。"""

from cogito.store.query_plan import build_query_plan, QueryPlan


class TestQueryPlan:
    def test_empty_query(self):
        plan = build_query_plan("")
        assert plan.query_text == ""

    def test_basic_query(self):
        plan = build_query_plan("Python")
        assert plan.query_text == "Python"
        assert plan.kinds == []
        assert plan.time_range_days == 0

    def test_preference_keyword(self):
        plan = build_query_plan("用户喜欢的编程语言")
        assert "preference" in plan.kinds

    def test_constraint_keyword(self):
        plan = build_query_plan("不要自动发送消息")
        assert "constraint" in plan.kinds

    def test_goal_keyword(self):
        plan = build_query_plan("本季度的目标")
        assert "goal" in plan.kinds

    def test_time_range(self):
        plan = build_query_plan("最近的项目决策")
        assert plan.time_range_days == 7

    def test_quoted_text_preserved(self):
        plan = build_query_plan('精确短语 "hello world" 测试')
        assert "hello world" in plan.query_text
