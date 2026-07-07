"""工具注册表与自动发现。

CAPABILITY-PLUGINS / 4.1 路径 A — 自动发现（内置 Tool）：
启动时通过 discover_builtin_tools() 扫描 tools/*.py 并注册。
"""

from __future__ import annotations

from cogito.capability.registry import CapabilityRegistry
from cogito.service.memory_service import SqliteMemoryService


# 全局默认注册表
registry: CapabilityRegistry = CapabilityRegistry()


def discover_builtin_tools(
    target: CapabilityRegistry | None = None,
    memory_service: SqliteMemoryService | None = None,
) -> CapabilityRegistry:
    """发现并注册所有内置工具。

    对于 MVP 使用显式导入的方式。
    后续可升级为 AST 扫描 tools/ 目录的自动发现。

    Args:
        target: 目标注册表，未传时使用全局默认。
        memory_service: 可选的 MemoryService，供记忆工具使用。
    """
    r = target if target is not None else registry

    # 显式导入并注册每个内置工具
    # 每个工具模块在顶层定义 tool_def 变量，或提供 create_tool_def 工厂
    from cogito.tools import echo, now

    for tool in [echo.tool_def, now.tool_def]:
        r.register(tool)

    # ── 记忆工具（依赖 MemoryService）──
    from cogito.tools.recall_memory import create_tool_def as _create_recall
    from cogito.tools.remember_memory import create_tool_def as _create_remember
    from cogito.tools.forget_memory import create_tool_def as _create_forget

    for factory in [_create_recall, _create_remember, _create_forget]:
        r.register(factory(service=memory_service))

    return r
