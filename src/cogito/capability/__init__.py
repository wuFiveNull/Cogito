"""Capability — 工具能力注册、执行与 MCP 集成。

CAPABILITY-PLUGINS / 3. Toolset：工具分组与按模式分发。
TOOL-SANDBOX / 1. 执行链：从 Registry resolve 到 ToolResult。
"""

from __future__ import annotations

from cogito.capability.models import ToolDef, ToolCallState, ToolResult, ToolContext
from cogito.capability.policy import ToolPolicy, PolicyDecision, PolicyResult
from cogito.capability.registry import CapabilityRegistry

__all__ = [
    "ToolDef",
    "ToolCallState",
    "ToolResult",
    "ToolContext",
    "ToolPolicy",
    "PolicyDecision",
    "PolicyResult",
    "CapabilityRegistry",
]
