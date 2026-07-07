"""TaskHandlerRegistry — Task 类型到处理函数的映射。

首批 Handler：
- memory.extract: 从会话中提取记忆候选
- summary.generate: 生成/更新会话摘要
- memory.consolidate: 记忆合并与归档（预留）
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from cogito.domain.task import Task

_LOGGER = logging.getLogger(__name__)

# Handler 签名：异步函数，接收 Task 和 SQLite 连接，返回结果文本
TaskHandler = Callable[[Task, sqlite3.Connection], str]


class TaskHandlerRegistry:
    """Task 处理器注册表。"""

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler
        _LOGGER.info("Registered task handler: %s", task_type)

    def get(self, task_type: str) -> TaskHandler | None:
        return self._handlers.get(task_type)

    def has(self, task_type: str) -> bool:
        return task_type in self._handlers

    def registered_types(self) -> list[str]:
        return list(self._handlers.keys())


def _build_registry(conn: sqlite3.Connection) -> TaskHandlerRegistry:
    """构建默认注册表，注册所有内置 Handler。"""
    registry = TaskHandlerRegistry()
    registry.register("memory.extract", _handle_memory_extract)
    registry.register("memory.consolidate", _handle_memory_consolidate)
    registry.register("summary.generate", _handle_summary_generate)
    return registry


# ── memory.extract ──


def _handle_memory_extract(task: Task, conn: sqlite3.Connection) -> str:
    """从会话消息范围提取记忆候选。

    暂为占位：后续集成 MemoryExtractor 的完整提取逻辑。
    当前仅标记任务完成，实际提取由 MemoryExtractor 在线处理。
    """
    _LOGGER.info("Task memory.extract: %s (stub)", task.task_id)
    return "extracted (stub)"


# ── memory.consolidate ──


def _handle_memory_consolidate(task: Task, conn: sqlite3.Connection) -> str:
    """记忆合并与归档（预留）。"""
    _LOGGER.info("Task memory.consolidate: %s (stub)", task.task_id)
    return "consolidated (stub)"


# ── summary.generate ──


def _handle_summary_generate(task: Task, conn: sqlite3.Connection) -> str:
    """生成/更新会话摘要。

    暂为占位：后续集成真正的摘要生成。
    """
    _LOGGER.info("Task summary.generate: %s (stub)", task.task_id)
    return "summary generated (stub)"
