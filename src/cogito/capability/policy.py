"""Tool Policy — 工具调用策略。

TOOL-SANDBOX / 3. Policy：
Policy 输入包含 Principal、运行模式、Trust Label、目标资源、参数、权限、风险、预算。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from cogito.capability.models import ConstraintSet, ToolDef


class PolicyDecision(StrEnum):
    """策略决策结果。"""

    allow = "allow"
    deny = "deny"
    require_approval = "require_approval"
    allow_with_constraints = "allow_with_constraints"


class PolicyResult:
    """策略评估结果。"""

    def __init__(
        self,
        decision: PolicyDecision = PolicyDecision.allow,
        reason: str = "",
        constraints: ConstraintSet | dict[str, Any] | None = None,
    ) -> None:
        self.decision = decision
        self.reason = reason
        self.constraints = (
            constraints
            if isinstance(constraints, ConstraintSet)
            else ConstraintSet.from_dict(constraints)
        )

    @property
    def is_allowed(self) -> bool:
        return self.decision in (
            PolicyDecision.allow,
            PolicyDecision.allow_with_constraints,
        )

    @property
    def requires_approval(self) -> bool:
        return self.decision == PolicyDecision.require_approval


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

        if tool_def and tool_def.approval_policy == "always":
            return PolicyResult(
                PolicyDecision.require_approval,
                f"Tool '{tool_name}' requires explicit approval",
            )

        if (
            tool_def
            and tool_def.namespace.startswith("mcp:")
            and tool_def.risk_level == "high"
        ):
            return PolicyResult(
                PolicyDecision.require_approval,
                f"High-risk MCP Tool '{tool_name}' requires explicit approval",
            )

        # 参数级确定性危险规则。模型分类只能在这些规则之后运行。
        if tool_name in {"skill_manage"} and str(
            (arguments or {}).get("action", ""),
        ) in {"archive", "delete"}:
            return PolicyResult(
                PolicyDecision.require_approval,
                "archiving or deleting a skill requires approval",
            )

        if tool_name == "apply_patch" and "patch" in (arguments or {}):
            patch = str((arguments or {}).get("patch", ""))
            file_count = max(patch.count("diff --git "), patch.count("\n--- "))
            if "/dev/null" in patch or file_count > 10:
                return PolicyResult(
                    PolicyDecision.require_approval,
                    "deleting or bulk-modifying workspace files requires approval",
                )

        requirements = dict(tool_def.resource_requirements) if tool_def else {}
        file_reads = {"read_file", "list_directory", "glob", "grep"}
        file_writes = {"write_file", "edit_file", "apply_patch"}
        if tool_name in file_reads | file_writes:
            path = str((arguments or {}).get("path", "."))
            requirements.setdefault("allowed_paths", [path])
            requirements.setdefault("mount_mode", "rw" if tool_name in file_writes else "ro")
        elif tool_name == "web_fetch":
            requirements.setdefault("network_enabled", True)
        constraints = ConstraintSet.from_dict(requirements)
        constrained_tools = {
            "read_file",
            "list_directory",
            "glob",
            "grep",
            "write_file",
            "edit_file",
            "apply_patch",
            "web_fetch",
        }
        return PolicyResult(
            PolicyDecision.allow_with_constraints
            if tool_name in constrained_tools
            else PolicyDecision.allow,
            constraints=constraints,
        )
