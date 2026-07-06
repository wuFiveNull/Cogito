"""Tool Policy — 工具调用策略。

TOOL-SANDBOX / 3. Policy：
Policy 输入包含 Principal、运行模式、Trust Label、目标资源、参数、权限、风险、预算。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from cogito.capability.models import ToolDef


class PolicyDecision(StrEnum):
    """策略决策结果。"""
    allow = "allow"
    deny = "deny"


class PolicyResult:
    """策略评估结果。"""
    def __init__(
        self,
        decision: PolicyDecision = PolicyDecision.allow,
        reason: str = "",
    ) -> None:
        self.decision = decision
        self.reason = reason

    @property
    def is_allowed(self) -> bool:
        return self.decision == PolicyDecision.allow


class ToolPolicy:
    """工具策略 —— 基于名称和风险的允许/拒绝策略。

    当前阶段实现简单的 allowlist/denylist 策略。
    后续可扩展为更细粒度的参数级策略。
    """

    def __init__(
        self,
        allowlist: set[str] | None = None,
        denylist: set[str] | None = None,
    ) -> None:
        self._allowlist = allowlist or set()
        self._denylist = denylist or set()

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        tool_def: ToolDef | None = None,
        agent_mode: str = "reactive",
    ) -> PolicyResult:
        """评估工具调用是否允许。

        规则优先级：
        1. denylist 匹配 → deny
        2. allowlist 非空且不匹配 → deny
        3. 高风险且 maintenance 模式 → deny
        4. 其他 → allow
        """
        # denylist
        if tool_name in self._denylist:
            return PolicyResult(
                PolicyDecision.deny,
                f"Tool '{tool_name}' is in denylist",
            )

        # allowlist
        if self._allowlist and tool_name not in self._allowlist:
            return PolicyResult(
                PolicyDecision.deny,
                f"Tool '{tool_name}' is not in allowlist",
            )

        # 高风险工具在 maintenance 模式下拒绝
        if agent_mode == "maintenance" and tool_def and tool_def.risk_level == "high":
            return PolicyResult(
                PolicyDecision.deny,
                f"High-risk tool '{tool_name}' not allowed in maintenance mode",
            )

        return PolicyResult(PolicyDecision.allow)
