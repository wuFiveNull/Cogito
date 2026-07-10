"""工具注册表与自动发现。

CAPABILITY-PLUGINS / 4.1 路径 A — 自动发现（内置 Tool）：
启动时通过 discover_builtin_tools() 扫描 tools/*.py 并注册。

PLAN-09 M4a: registry 不再依赖 SqliteMemoryService；记忆工具
由组合根通过 MemoryReader / MemoryWriter 端口注入（工厂或实例）。
"""
from __future__ import annotations

from collections.abc import Callable

from cogito.capability.registry import CapabilityRegistry
from cogito.contracts.memory import MemoryReader, MemoryWriter

# 全局默认注册表
registry: CapabilityRegistry = CapabilityRegistry()


def discover_builtin_tools(
    target: CapabilityRegistry | None = None,
    *,
    memory_reader: MemoryReader | None = None,
    memory_writer: MemoryWriter | None = None,
    make_memory_writer: Callable[[], MemoryWriter] | None = None,
    make_memory_reader: Callable[[], MemoryReader] | None = None,
) -> CapabilityRegistry:
    """发现并注册所有内置工具。

    对于 MVP 使用显式导入的方式。
    后续可升级为 AST 扫描 tools/ 目录的自动发现。

    Args:
        target: 目标注册表，未传时使用全局默认。
        memory_reader: MemoryReader 端口实例（供 recall_memory）。
        memory_writer: MemoryWriter 端口实例（供 remember/forget_memory）。
        make_memory_writer: 按需创建 MemoryWriter 的工厂（推荐，独立事务）。
        make_memory_reader: 按需创建 MemoryReader 的工厂（兜底）。
    """
    r = target if target is not None else registry

    # 显式导入并注册每个内置工具
    # 每个工具模块在顶层定义 tool_def 变量，或提供 create_tool_def 工厂
    from cogito.tools import echo, now

    for tool in [echo.tool_def, now.tool_def]:
        r.register(tool)

    # ── 记忆工具（依赖 MemoryReader / MemoryWriter 端口）──
    from cogito.tools.forget_memory import create_tool_def as _create_forget
    from cogito.tools.recall_memory import create_tool_def as _create_recall
    from cogito.tools.remember_memory import create_tool_def as _create_remember

    # 优先用 make_writer 工厂（独立事务），其次用共享 writer 实例
    r.register(_create_remember(
        writer=memory_writer,
        make_writer=make_memory_writer,
    ))
    r.register(_create_forget(
        reader=memory_reader,
        writer=memory_writer,
    ))
    # 读工具使用共享 reader 或工厂
    r.register(_create_recall(reader=memory_reader))

    return r


def assemble_default_registry(
    *,
    memory_reader: MemoryReader | None = None,
    memory_writer: MemoryWriter | None = None,
    make_memory_writer: Callable[[], MemoryWriter] | None = None,
    make_memory_reader: Callable[[], MemoryReader] | None = None,
) -> CapabilityRegistry:
    """创建 CapabilityRegistry 并注册所有内置工具。

    组合根（application.py / 测试）在调用
    service.agent_runner.build_agent_runner 之前，先调用此函数拿到
    CapabilityRegistry，再传入。避免 service 反向依赖 cogito.tools。
    """
    registry = CapabilityRegistry()
    discover_builtin_tools(
        registry,
        memory_reader=memory_reader,
        memory_writer=memory_writer,
        make_memory_writer=make_memory_writer,
        make_memory_reader=make_memory_reader,
    )
    return registry
