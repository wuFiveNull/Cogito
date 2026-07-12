"""工具注册表与自动发现。

CAPABILITY-PLUGINS / 4.1 路径 A — 自动发现（内置 Tool）：
启动时通过 discover_builtin_tools() 扫描 tools/*.py 并注册。

PLAN-09 M4a: registry 不再依赖 SqliteMemoryService；记忆工具
由组合根通过 MemoryReader / MemoryWriter 端口注入（工厂或实例）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from cogito.capability.registry import CapabilityRegistry

_LOGGER = logging.getLogger("cogito.tools.registry")

from cogito.contracts.memory import MemoryReader, MemoryWriter
from cogito.contracts.context import KnowledgeReader
from cogito.contracts.multimodal import StickerService, VisionToolService

# 全局默认注册表
registry: CapabilityRegistry = CapabilityRegistry()


def discover_builtin_tools(
    target: CapabilityRegistry | None = None,
    *,
    memory_reader: MemoryReader | None = None,
    memory_writer: MemoryWriter | None = None,
    make_memory_writer: Callable[[], MemoryWriter] | None = None,
    make_memory_reader: Callable[[], MemoryReader] | None = None,
    make_vision_service: Callable[[], VisionToolService] | None = None,
    make_sticker_service: Callable[[], StickerService] | None = None,
    make_task_service: Callable[[], Any] | None = None,
    make_memory_service: Callable[[], Any] | None = None,
    knowledge_reader: KnowledgeReader | None = None,
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
        make_task_service=make_task_service,
    ))
    r.register(_create_forget(
        reader=memory_reader,
        writer=memory_writer,
    ))
    # 读工具使用共享 reader 或工厂；召回命中后写 exposed 信号（MEM-02）
    # MEM-02: 工具召回命中 → exposed 信号（可观察）
    r.register(_create_recall(
        reader=memory_reader,
        on_exposed=_make_on_exposed_handler(make_memory_service=make_memory_service),
    ))

    if knowledge_reader is not None:
        from cogito.tools.search_knowledge import create_tool_def as _create_knowledge_search

        r.register(_create_knowledge_search(reader=knowledge_reader))

    if make_vision_service is not None:
        from cogito.tools.analyze_multimodal_asset import create_tool_def as _create_vision

        r.register(_create_vision(make_service=make_vision_service))

    if make_sticker_service is not None:
        from cogito.tools.sticker import (
            create_save_sticker_def as _create_save,
        )
        from cogito.tools.sticker import (
            create_save_sticker_from_url_def as _create_save_url,
        )
        from cogito.tools.sticker import (
            create_send_sticker_def as _create_send,
        )

        r.register(_create_save(make_service=make_sticker_service))
        r.register(_create_save_url(make_service=make_sticker_service))
        r.register(_create_send(make_service=make_sticker_service))

    return r


def assemble_default_registry(
    *,
    memory_reader: MemoryReader | None = None,
    memory_writer: MemoryWriter | None = None,
    make_memory_writer: Callable[[], MemoryWriter] | None = None,
    make_memory_reader: Callable[[], MemoryReader] | None = None,
    make_vision_service: Callable[[], VisionToolService] | None = None,
    make_sticker_service: Callable[[], StickerService] | None = None,
    make_task_service: Callable[[], Any] | None = None,
    make_memory_service: Callable[[], Any] | None = None,
    knowledge_reader: KnowledgeReader | None = None,
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
        make_vision_service=make_vision_service,
        make_sticker_service=make_sticker_service,
        make_task_service=make_task_service,
        make_memory_service=make_memory_service,
        knowledge_reader=knowledge_reader,
    )
    return registry


def _make_on_exposed_handler(
    make_memory_service: Callable[[], Any] | None,
) -> Callable[[list[str]], None] | None:
    """构建 recall_memory 的 exposed 信号回调（PLAN-16 M3 MEM-02）。

    每次命中以独立连接写信号，失败仅记录日志，不影响召回结果本身。
    """
    if make_memory_service is None:
        return None

    def _on_exposed(memory_ids: list[str]) -> None:
        svc = make_memory_service()
        from cogito.service.memory_signals import SignalWriter
        writer = SignalWriter(svc.conn if hasattr(svc, "conn") else svc._conn)
        try:
            for mid in memory_ids:
                try:
                    writer.record_exposed(
                        mid,
                        idempotency_key=f"recall-exposed:{mid}",
                        algorithm_version="2",
                    )
                except Exception as e:
                    _LOGGER.warning("recall_memory exposed signal failed for %s: %s", mid, e)
            # 完整：提交 Signal + Outbox，确保 exposed 可观察
            svc.conn.commit()
        finally:
            # 独立连接用完即关，避免泄漏
            try:
                svc.conn.close()
            except Exception:
                pass

    return _on_exposed
