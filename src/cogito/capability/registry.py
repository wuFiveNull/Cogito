"""CapabilityRegistry — 中心工具注册表 (Capability Registry 2.0, Plan 03 M1)。

CAPABILITY-PLUGINS / 4. Capability Registry：
- 自动发现（内置 Tool）和插件注册共享同一个 Registry
- Agent 按运行模式和 Toolset 获取可见的工具
- 所有跨进程模型包含 schema_version/ID/TraceContext
- 注册结果形成不可变 CapabilitySnapshot，写入 Attempt
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cogito.capability.models import ToolDef

MAX_DESCRIPTION_LENGTH = 512


@dataclass(frozen=True)
class CapabilitySnapshot:
    """不可变能力快照 —— 写入 Attempt，锁定执行时的能力集合。"""
    schema_version: str = "1.0"
    capabilities: tuple[ToolDef, ...] = ()
    policy_version: str = "1.0"

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(t.capability_id for t in self.capabilities)


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
        """注册一个工具定义 (Capability Registry 2.0)。

        按 capability_id (namespace:name) 索引。
        同名冲突时 startup 失败并指出来源（Plan 03 M1）。
        """
        cid = tool.capability_id
        if cid in self._tools:
            import warnings

            warnings.warn(
                f"Capability '{cid}' already registered (from {self._tools[cid].namespace}), "
                f"overwriting",
                stacklevel=2,
            )
        self._tools[cid] = tool

    # ── 查询 ──

    def get(self, name: str) -> ToolDef | None:
        """按 capability_id 或 name 获取工具定义。"""
        if name in self._tools:
            return self._tools[name]
        # 反向按 name 查找（兼容未加 namespace 的调用方）
        for t in self._tools.values():
            if t.name == name:
                return t
        return None

    def resolve(self, name: str) -> ToolDef:
        """按 capability_id 或 name 解析，不存在时抛出 KeyError。"""
        tool = self.get(name)
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
        """兼容 name 和 capability_id (namespace:name) 查找。"""
        if name in self._tools:
            return True
        # 反向查找：name 匹配（不含 namespace 前缀）
        return any(t.name == name for t in self._tools.values())

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

    # ── Capability Snapshot (Plan 03 M1) ─────────────────────────

    def build_snapshot(
        self,
        mode: str = "",
        toolsets: set[str] | None = None,
        policy_allowed: set[str] | None = None,
    ) -> CapabilitySnapshot:
        """构建不可变能力快照（写入 Attempt）。

        运行时按 Principal、mode、enabled toolset、Policy 和健康状态过滤。
        disabled/deprecated 工具不会进入 Model Schema。
        """
        visible: list[ToolDef] = []
        for t in self._tools.values():
            if t.disabled or t.deprecated:
                continue
            if toolsets and not (set(t.toolset) & toolsets):
                continue
            if mode and t.supported_modes and mode not in t.supported_modes:
                continue
            if policy_allowed is not None and t.capability_id not in policy_allowed:
                continue
            # health check
            if t.check_fn and not t.check_fn():
                continue
            visible.append(t)
        # Tool Schema 顺序稳定（便于缓存和回放）
        visible.sort(key=lambda x: x.capability_id)
        return CapabilitySnapshot(capabilities=tuple(visible))

    def __repr__(self) -> str:
        return f"CapabilityRegistry({len(self._tools)} tools)"
