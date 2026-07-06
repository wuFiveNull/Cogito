"""Tests for ToolPolicy.

覆盖场景：
- 默认 allow
- denylist 拒绝
- allowlist 检查
- 高风险工具 + maintenance 模式
- 空策略
"""

from __future__ import annotations

from cogito.capability.models import ToolDef
from cogito.capability.policy import ToolPolicy, PolicyDecision


class TestToolPolicy:
    def test_default_allow(self):
        policy = ToolPolicy()
        result = policy.evaluate("echo", {"text": "hello"})
        assert result.is_allowed
        assert result.decision == PolicyDecision.allow

    def test_denylist_rejects(self):
        policy = ToolPolicy(denylist={"dangerous_tool"})
        result = policy.evaluate("dangerous_tool")
        assert not result.is_allowed
        assert "denylist" in result.reason

    def test_denylist_others_allowed(self):
        policy = ToolPolicy(denylist={"bad"})
        result = policy.evaluate("good_tool")
        assert result.is_allowed

    def test_allowlist_grants(self):
        policy = ToolPolicy(allowlist={"safe_tool", "echo"})
        result = policy.evaluate("echo", {"text": "hi"})
        assert result.is_allowed

    def test_allowlist_rejects_unknown(self):
        policy = ToolPolicy(allowlist={"safe_tool"})
        result = policy.evaluate("unknown_tool")
        assert not result.is_allowed
        assert "allowlist" in result.reason

    def test_empty_allowlist_allows_all(self):
        policy = ToolPolicy(allowlist=set())
        result = policy.evaluate("any_tool")
        assert result.is_allowed

    def test_high_risk_rejected_in_maintenance(self):
        policy = ToolPolicy()
        tool = ToolDef(
            name="danger",
            description="Dangerous",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args, ctx: "done",
            risk_level="high",
        )
        result = policy.evaluate("danger", tool_def=tool, agent_mode="maintenance")
        assert not result.is_allowed
        assert "High-risk" in result.reason

    def test_high_risk_allowed_in_reactive(self):
        policy = ToolPolicy()
        tool = ToolDef(
            name="danger",
            description="Dangerous",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args, ctx: "done",
            risk_level="high",
        )
        result = policy.evaluate("danger", tool_def=tool, agent_mode="reactive")
        assert result.is_allowed

    def test_low_risk_allowed_in_maintenance(self):
        policy = ToolPolicy()
        tool = ToolDef(
            name="safe",
            description="Safe",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args, ctx: "done",
            risk_level="low",
        )
        result = policy.evaluate("safe", tool_def=tool, agent_mode="maintenance")
        assert result.is_allowed
