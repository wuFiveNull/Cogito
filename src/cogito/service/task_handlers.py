"""TaskHandlerRegistry — Task 类型到处理函数的映射。

首批 Handler：
- memory.extract: 从会话中提取记忆候选
- summary.generate: 生成/更新会话摘要
- memory.consolidate: 记忆合并与归档（计算 retention_score，过期低分记忆，刷新标记视图）

DOMAIN-CONTRACTS / 1.13 MemoryItem：状态转换规则
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from cogito.domain.task import Task
from cogito.service.memory_views import MemoryViewsGenerator
from cogito.store.time_utils import epoch_ms

_LOGGER = logging.getLogger(__name__)

# Handler 签名：同步函数，接收 Task 和 SQLite 连接，返回结果文本
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


RETENTION_IMPORTANCE = 0.30
RETENTION_RETRIEVAL = 0.20
RETENTION_EXPLICITNESS = 0.15
RETENTION_CONFIDENCE = 0.15
RETENTION_RECENCY = 0.10
RETENTION_SCOPE = 0.10

RETENTION_ACTIVE_THRESHOLD = 0.70
RETENTION_ARCHIVE_THRESHOLD = 0.45
RETENTION_CANDIDATE_THRESHOLD = 0.25


def _compute_retention_score(
    importance: float,
    confidence: float,
    retrieval_count: int,
    age_days: float,
    explicitness: str,
) -> float:
    """计算记忆保留分数（0.0 ~ 1.0）。

    决定记忆是否应保留活跃、归档或进入删除候选。
    """
    recency = max(0.0, 1.0 - age_days / 365.0)
    expl_map = {
        "explicit_user_statement": 1.0,
        "confirmed_inference": 0.9,
        "external_source": 0.7,
        "system_generated": 0.6,
        "model_inference": 0.4,
    }
    expl_score = expl_map.get(explicitness, 0.5)
    retrieval_freq = min(1.0, retrieval_count / 50.0)

    return (
        RETENTION_IMPORTANCE * importance
        + RETENTION_RETRIEVAL * retrieval_freq
        + RETENTION_EXPLICITNESS * expl_score
        + RETENTION_CONFIDENCE * confidence
        + RETENTION_RECENCY * recency
    )


def _handle_memory_consolidate(task: Task, conn: sqlite3.Connection) -> str:
    """记忆合并与归档。

    计算所有 confirmed 记忆的 retention_score：
    - >= 0.70：保留活跃（不做变更）
    - 0.45 ~ 0.70：保留但不操作（第一版暂不降级）
    - 0.25 ~ 0.45：标记为 expired
    - < 0.25：软删除

    执行后刷新 Markdown 视图。
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    # 加载所有 confirmed 非删除记忆
    rows = conn.execute("""
        SELECT memory_id, importance, confidence, retrieval_count,
               explicitness, created_at, half_life_days
        FROM memory_items
        WHERE status='confirmed' AND deleted_at IS NULL
    """).fetchall()

    archived = 0
    deleted = 0

    for r in rows:
        created = r["created_at"]
        if created:
            try:
                created_dt = datetime.fromisoformat(str(created)) if isinstance(created, str) else created
                age_days = abs((now - created_dt).total_seconds() / 86400)
            except (ValueError, TypeError):
                age_days = 365.0
        else:
            age_days = 365.0

        score = _compute_retention_score(
            importance=r["importance"],
            confidence=r["confidence"],
            retrieval_count=r["retrieval_count"] or 0,
            age_days=age_days,
            explicitness=r["explicitness"],
        )

        mid = r["memory_id"]

        if score < RETENTION_CANDIDATE_THRESHOLD:
            # 删除候选
            conn.execute(
                "UPDATE memory_items SET deleted_at=?, updated_at=?, version=version+1 "
                "WHERE memory_id=? AND deleted_at IS NULL",
                (now_iso, now_iso, mid),
            )
            deleted += 1
        elif score < RETENTION_ARCHIVE_THRESHOLD:
            # 归档：标记为 expired
            conn.execute(
                "UPDATE memory_items SET status='expired', updated_at=?, version=version+1 "
                "WHERE memory_id=? AND status='confirmed'",
                (now_iso, mid),
            )
            archived += 1

    # 刷新 Markdown 视图
    try:
        generator = MemoryViewsGenerator(conn)
        generator.generate_all()
    except Exception as e:
        _LOGGER.warning("Failed to refresh views after consolidation: %s", e)

    result = f"consolidated: {archived} archived, {deleted} deleted"
    _LOGGER.info("memory.consolidate %s: %s", task.task_id, result)
    return result


# ── summary.generate ──


def _handle_summary_generate(task: Task, conn: sqlite3.Connection) -> str:
    """生成/更新会话摘要。

    暂为占位：后续集成真正的摘要生成。
    """
    _LOGGER.info("Task summary.generate: %s (stub)", task.task_id)
    return "summary generated (stub)"
