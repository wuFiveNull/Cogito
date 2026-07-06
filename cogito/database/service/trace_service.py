"""
cogito.database.service.trace_service — TraceService

对应设计文档第 8、17、18、20 节。

职责：
- 创建/完成 Span（步骤级链路追踪）
- 记录工具调用过程和重试
- 记录记忆检索链路
- 记录最终回答
"""

from __future__ import annotations

import time
from typing import Any

from cogito.database.connection import AsyncDatabase
from cogito.database.ids import new_uuid
from cogito.database.repository.trace_events import TraceEventRepository
from cogito.database.utils import utcnow, json_list, json_obj


class TraceService:
    """Trace / Span 业务服务。"""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db
        self._repo = TraceEventRepository(db)

    # ── Span 创建 ──────────────────────────────────────────────────

    async def create_span(
        self,
        *,
        trace_id: str,
        parent_span_id: str | None = None,
        user_id: str,
        session_id: str | None = None,
        step_type: str,
        step_name: str,
        span_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """创建一个新的 span（步骤开始）。

        对应文档第 8.4 节「Span 写入模式 — 开始时插入」。
        """
        params = {
            "id": span_id or new_uuid(),
            "trace_id": trace_id,
            "parent_span_id": parent_span_id,
            "user_id": user_id,
            "session_id": session_id,
            "step_type": step_type,
            "step_name": step_name,
            "status": "running",
        }
        params.update(extra)
        return await self._repo.insert(params)

    # ── Span 完成 ──────────────────────────────────────────────────

    async def complete_span(
        self,
        span_id: str,
        *,
        status: str = "success",
        output_event_ids: list[str] | None = None,
        output_memory_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        output_hash: str | None = None,
        latency_ms: int | None = None,
        **extra: Any,
    ) -> dict[str, Any] | None:
        """完成一个 span（标记成功）。

        对应文档第 8.4 节「Span 写入模式 — 完成时更新」。
        """
        now = utcnow()
        updates: dict[str, Any] = {
            "status": status,
            "ended_at": now,
            "latency_ms": latency_ms or 0,
        }
        if output_event_ids is not None:
            updates["output_event_ids_json"] = json_list(output_event_ids)
        if output_memory_ids is not None:
            updates["output_memory_ids_json"] = json_list(output_memory_ids)
        if metadata is not None:
            updates["metadata_json"] = json_obj(metadata)
        if output_hash is not None:
            updates["output_hash"] = output_hash
        updates.update(extra)

        return await self._repo.update_status(span_id, updates)

    async def fail_span(
        self,
        span_id: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        status: str = "failed",
        **extra: Any,
    ) -> dict[str, Any] | None:
        """标记 span 为失败。

        对应文档第 8.4 节「Span 写入模式 — 失败时」。
        """
        now = utcnow()
        updates: dict[str, Any] = {
            "status": status,
            "ended_at": now,
            "latency_ms": latency_ms or 0,
        }
        if error_code is not None:
            updates["error_code"] = error_code
        if error_message is not None:
            updates["error_message"] = error_message
        if metadata is not None:
            updates["metadata_json"] = json_obj(metadata)
        updates.update(extra)

        return await self._repo.update_status(span_id, updates)

    # ── Trace 查询 ────────────────────────────────────────────────

    async def get_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """获取完整 trace（所有 span 按时间排序）。"""
        return await self._repo.get_by_trace(trace_id)

    async def get_trace_tree(self, trace_id: str) -> list[dict[str, Any]]:
        """获取 trace 并组装成父子结构。"""
        spans = await self._repo.get_by_trace(trace_id)
        return _build_tree(spans)

    async def get_response_info(self, trace_id: str) -> dict[str, Any] | None:
        """获取最终回答 span 的信息。"""
        return await self._repo.get_response_span(trace_id)

    # ── 清理 ──────────────────────────────────────────────────────

    async def clean_old_traces(self, before: str) -> int:
        """删除指定时间之前的 trace 记录。"""
        return await self._repo.delete_old_traces(before)


def _build_tree(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将扁平 span 列表转换为父子树结构（增加 children 字段）。"""
    span_map: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []

    for span in spans:
        span_id = span["id"]
        span["children"] = []
        span_map[span_id] = span

    for span in spans:
        parent_id = span.get("parent_span_id")
        if parent_id and parent_id in span_map:
            span_map[parent_id]["children"].append(span)
        else:
            roots.append(span)

    return roots
