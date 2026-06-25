"""
cogito.tools.executor — 工具目录与执行器的基础实现

当前实现空壳：Catalog 返回空列表，Executor 返回错误。
后续增加真实工具注册和执行。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ToolCatalog:
    """工具目录 — 返回当前 actor/session 可见的工具列表。"""

    async def list_available_tools(
        self,
        *,
        actor_id: str,
        session_id: str,
    ) -> list[object]:
        """返回可用工具列表，当前为空。

        TODO: 从注册表加载真实工具。
        """
        return []


class NullToolExecutor:
    """工具执行器 — 所有工具调用返回错误。"""

    async def execute(
        self,
        *,
        tool_call: object,
        context: object = None,
    ) -> object:
        """执行工具调用。

        Args:
            tool_call: ToolCall 对象，含 id / name / arguments。
            context: 可选的执行上下文。

        Returns:
            工具结果 dict，含 content / succeeded 字段。
        """
        name = getattr(tool_call, "name", "unknown")
        call_id = getattr(tool_call, "id", "unknown")

        logger.warning(
            "Tool not implemented: %s (call_id=%s)",
            name,
            call_id,
        )

        return {
            "call_id": call_id,
            "tool_name": name,
            "content": f"Tool '{name}' is not implemented.",
            "succeeded": False,
        }


NullToolCatalog = ToolCatalog
