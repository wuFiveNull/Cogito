"""
cogito.database.service.event_service — EventService

对应设计文档第 9、10 节。

职责：
- 保存用户消息、Agent 回复和工具事件
- 管理提取组的分配和状态流转
- 保证事务边界（第 23 节）
"""

from __future__ import annotations

import json
from typing import Any

from cogito.database.connection import AsyncDatabase
from cogito.database.ids import new_uuid
from cogito.database.repository.events import EventRepository
from cogito.database.utils import json_list, json_obj


class EventService:
    """Event 业务服务。"""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db
        self._repo = EventRepository(db)

    # ── 保存事件 ──────────────────────────────────────────────────

    async def save_event(
        self,
        *,
        user_id: str,
        session_id: str,
        role: str,
        event_type: str,
        content: str = "",
        content_json: dict[str, Any] | None = None,
        trace_id: str | None = None,
        created_by_span_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """保存一条事件记录。

        自动递增 seq_no。
        """
        seq_no = (await self._repo.get_last_seq_no(user_id, session_id)) + 1

        params: dict[str, Any] = {
            "id": event_id or new_uuid(),
            "user_id": user_id,
            "session_id": session_id,
            "seq_no": seq_no,
            "role": role,
            "event_type": event_type,
            "content": content,
            "content_json": json.dumps(content_json or {}, ensure_ascii=False),
            "trace_id": trace_id,
            "created_by_span_id": created_by_span_id,
        }
        return await self._repo.insert(params)

    async def save_user_message(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        trace_id: str,
        created_by_span_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """快捷方法：保存用户消息。"""
        return await self.save_event(
            user_id=user_id,
            session_id=session_id,
            role="user",
            event_type="user_message",
            content=content,
            trace_id=trace_id,
            created_by_span_id=created_by_span_id,
            event_id=event_id,
        )

    async def save_assistant_message(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        content_json: dict[str, Any] | None = None,
        trace_id: str,
        created_by_span_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """快捷方法：保存 Agent 回复。"""
        return await self.save_event(
            user_id=user_id,
            session_id=session_id,
            role="assistant",
            event_type="assistant_message",
            content=content,
            content_json=content_json,
            trace_id=trace_id,
            created_by_span_id=created_by_span_id,
            event_id=event_id,
        )

    async def save_tool_request(
        self,
        *,
        user_id: str,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        trace_id: str,
        created_by_span_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """快捷方法：保存工具请求事件。"""
        content = f"调用工具: {tool_name}"
        content_json = {
            "tool_name": tool_name,
            "tool_version": arguments.get("_tool_version") if arguments else None,
            "arguments": arguments or {},
        }
        return await self.save_event(
            user_id=user_id,
            session_id=session_id,
            role="tool",
            event_type="tool_request",
            content=content,
            content_json=content_json,
            trace_id=trace_id,
            created_by_span_id=created_by_span_id,
            event_id=event_id,
        )

    async def save_tool_result(
        self,
        *,
        user_id: str,
        session_id: str,
        tool_name: str,
        result: dict[str, Any],
        trace_id: str,
        created_by_span_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """快捷方法：保存工具结果事件。"""
        return await self.save_event(
            user_id=user_id,
            session_id=session_id,
            role="tool",
            event_type="tool_result",
            content=f"工具 {tool_name} 返回结果",
            content_json=result,
            trace_id=trace_id,
            created_by_span_id=created_by_span_id,
            event_id=event_id,
        )

    async def save_tool_error(
        self,
        *,
        user_id: str,
        session_id: str,
        tool_name: str,
        error_code: str,
        error_message: str,
        trace_id: str,
        created_by_span_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """快捷方法：保存工具错误事件。"""
        content_json = {
            "tool_name": tool_name,
            "error_code": error_code,
            "message": error_message,
        }
        return await self.save_event(
            user_id=user_id,
            session_id=session_id,
            role="tool",
            event_type="tool_error",
            content=f"工具 {tool_name} 错误: {error_message}",
            content_json=content_json,
            trace_id=trace_id,
            created_by_span_id=created_by_span_id,
            event_id=event_id,
        )

    # ── 提取组管理 ────────────────────────────────────────────────

    async def claim_extraction_group(
        self,
        user_id: str,
        session_id: str,
        start_seq: int,
        end_seq: int,
        group_id: str | None = None,
    ) -> str | None:
        """事务安全地领取一个提取组。

        对应文档第 10.3 节，需要在 `BEGIN IMMEDIATE` 事务中调用。

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            start_seq: 起始 seq_no
            end_seq: 结束 seq_no
            group_id: 可选，不提供则自动生成

        Returns:
            领取成功返回 group_id，失败返回 None
        """
        actual_group_id = group_id or new_uuid()

        claimed = await self._repo.claim_extraction(
            user_id, session_id, start_seq, end_seq, actual_group_id,
        )

        if claimed == 0:
            return None
        return actual_group_id

    async def complete_extraction(
        self,
        group_id: str,
    ) -> int:
        """完成提取组。"""
        return await self._repo.complete_extraction(group_id)

    async def fail_extraction(
        self,
        group_id: str,
        error_message: str | None = None,
    ) -> int:
        """标记提取组失败。"""
        return await self._repo.fail_extraction(group_id, error_message)

    async def retry_failed_extraction(
        self,
        group_id: str,
        max_attempts: int = 3,
    ) -> int:
        """重试失败的提取组。"""
        return await self._repo.retry_failed_extraction(group_id, max_attempts)

    async def get_extraction_events(
        self,
        group_id: str,
    ) -> list[dict[str, Any]]:
        """读取提取组中的事件列表。"""
        return await self._repo.get_by_extraction_group(group_id)

    async def get_extraction_context(
        self,
        user_id: str,
        session_id: str,
        before_seq: int,
        context_count: int = 4,
    ) -> list[dict[str, Any]]:
        """读取提取时的重叠上下文。"""
        return await self._repo.get_context_events(
            user_id, session_id, before_seq, context_count,
        )

    # ── 查询 ──────────────────────────────────────────────────────

    async def get_session_events(
        self,
        user_id: str,
        session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询 session 的事件列表。"""
        return await self._repo.get_session_events(user_id, session_id, limit)

    async def get_event(
        self,
        event_id: str,
    ) -> dict[str, Any] | None:
        """根据 ID 查询事件。"""
        return await self._repo.get_by_id(event_id)

    async def get_pending_summary(self) -> list[dict[str, Any]]:
        """待提取会话摘要。"""
        return await self._repo.get_pending_extraction_summary()

    async def get_failed_groups(self) -> list[dict[str, Any]]:
        """失败提取组列表。"""
        return await self._repo.get_failed_extraction_groups()
