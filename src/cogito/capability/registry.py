"""CapabilityRegistry — 中心工具注册表。

CAPABILITY-PLUGINS / 4. Capability Registry：
- 自动发现（内置 Tool）和插件注册共享同一个 Registry
- Agent 按运行模式和 Toolset 获取可见的工具
"""

from __future__ import annotations

from typing import Any

from cogito.capability.models import ToolDef

MAX_DESCRIPTION_LENGTH = 512


class CapabilityRegistry:
    """中心工具注册表。

    - 所有工具统一注册，提供查询
    - 支持按 Toolset、模式过滤
    - 提供 ModelRequest.tools 格式的 schema 输出
    - 命名冲突检测（name + version）
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    # ── 注册 ──

    def register(self, tool: ToolDef) -> None:
        """注册一个工具定义。

        按 name 索引。同名工具后注册覆盖先注册（发出警告）。
        """
        if tool.name in self._tools:
            import warnings

            warnings.warn(
                f"Tool '{tool.name}' already registered, overwriting",
                stacklevel=2,
            )
        self._tools[tool.name] = tool

    # ── 查询 ──

    def get(self, name: str) -> ToolDef | None:
        """按名称获取工具定义。"""
        return self._tools.get(name)

    def resolve(self, name: str) -> ToolDef:
        """按名称解析，不存在时抛出 KeyError。"""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Tool '{name}' not found in registry")
        return tool

    def all_tools(self) -> list[ToolDef]:
        """返回所有已注册的工具。"""
        return list(self._tools.values())

    def list_by_toolset(self, toolset: str) -> list[ToolDef]:
        """返回属于指定 Toolset 的所有工具。"""
        return [t for t in self._tools.values() if toolset in t.toolset]

    def list_by_toolsets(self, toolsets: set[str]) -> list[ToolDef]:
        """返回属于任一指定 Toolset 的所有工具。"""
        return [t for t in self._tools.values() if set(t.toolset) & toolsets]

    def list_by_mode(self, mode: str) -> list[ToolDef]:
        """返回指定模式下可见的所有工具。

        空 supported_modes 表示该工具在所有模式均可见。
        """
        return [
            t for t in self._tools.values()
            if not t.supported_modes or mode in t.supported_modes
        ]

    # ── Schema 输出 ──

    @staticmethod
    def _sanitize_description(desc: str) -> str:
        """截断过长的 description（MODEL-ADAPTER / 6）。"""
        if len(desc) > MAX_DESCRIPTION_LENGTH:
            return desc[:MAX_DESCRIPTION_LENGTH] + "..."
        return desc

    def get_openai_schemas(self, toolsets: set[str] | None = None) -> list[dict[str, Any]]:
        """获取 OpenAI function calling 格式的工具 Schema 列表。

        Args:
            toolsets: 可选，只返回指定 Toolset 的工具。None 返回全部。

        Returns:
            [{type: "function", function: {name, description, parameters}}]
        """
        tools = (
            self.list_by_toolsets(toolsets)
            if toolsets is not None
            else self.all_tools()
        )

        result = []
        for t in tools:
            result.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": self._sanitize_description(t.description),
                    "parameters": t.input_schema,
                },
            })
        return result

    def get_schemas_by_mode(self, mode: str) -> list[dict[str, Any]]:
        """返回指定模式下可见的工具 Schema。"""
        tools = self.list_by_mode(mode)
        result = []
        for t in tools:
            result.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": self._sanitize_description(t.description),
                    "parameters": t.input_schema,
                },
            })
        return result

    # ── 统计 ──

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    # ── 取消注册 ──

    def unregister(self, name: str) -> None:
        """按名称移除一个工具。"""
        self._tools.pop(name, None)

    def unregister_by_prefix(self, prefix: str) -> list[str]:
        """移除所有名称以指定前缀开头的工具。

        Returns: 被移除的工具名列表。
        """
        removed = [n for n in self._tools if n.startswith(prefix)]
        for n in removed:
            del self._tools[n]
        return removed

    def __repr__(self) -> str:
        return f"CapabilityRegistry({len(self._tools)} tools)"
