# cogito/agent/domain/execution_level.py
#
# Execution levels for tool call security.
#
# Reference: QwenPaw ToolExecutionLevel + gemini-cli ApprovalMode

from __future__ import annotations

from enum import StrEnum


class ExecutionLevel(StrEnum):
    """Tool execution security level.

    Controls how aggressively tool calls are guarded and approved.

    OFF:
        No checks. All tools are allowed. Use only for development.

    AUTO:
        Check guarded_tools list only. Tools not in the list are auto-allowed.

    SMART:
        Risk-based auto-allow. INFO/LOW/MEDIUM severity findings auto-pass;
        HIGH/CRITICAL require approval. Recommended default.

    STRICT:
        All tool calls require approval. Every invocation is subject to
        user confirmation.
    """

    OFF = "off"
    AUTO = "auto"
    SMART = "smart"
    STRICT = "strict"

    def requires_approval(self, risk_score: float | None = None) -> bool:
        """Return True when the given risk score requires user approval.

        Args:
            risk_score: 0.0 (safe) to 1.0 (critical). None = unknown risk.
        """
        if self is ExecutionLevel.STRICT:
            return True
        if self is ExecutionLevel.OFF:
            return False
        if self is ExecutionLevel.AUTO:
            return False  # Only guarded_tools need approval
        # SMART: require approval for risk_score >= 0.7 or unknown
        if self is ExecutionLevel.SMART:
            return risk_score is None or risk_score >= 0.7
        return False

    def is_tool_guarded(self, tool_name: str, guarded_tools: frozenset[str]) -> bool:
        """Return True when *tool_name* is in the guarded set.

        In OFF/STRICT modes, the guarded set is irrelevant.
        """
        if self is ExecutionLevel.OFF:
            return False
        if self is ExecutionLevel.STRICT:
            return True  # ALL tools are guarded
        if self is ExecutionLevel.AUTO:
            return tool_name in guarded_tools
        # SMART: guarded_tools is only for tools with elevated default risk
        # that want approval even when risk_score is low
        return tool_name in guarded_tools

    @staticmethod
    def default() -> ExecutionLevel:
        return ExecutionLevel.SMART
