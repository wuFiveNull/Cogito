"""Recall memory tool — 检索长期记忆。

使用 MemoryReader.retrieve 检索相关记忆。
返回带评分的结果，让模型和用户了解可信度。

边界（PLAN-09 M4a）：工具文件不直接依赖 SqliteMemoryService 或
MemoryRepository，只依赖 contracts.memory.MemoryReader 端口。
"""
from __future__ import annotations

from cogito.capability.models import ToolContext, ToolDef
from cogito.contracts.memory import MemoryReader

TOOL_NAME = "recall_memory"


def _make_handler(reader: MemoryReader | None = None):
    """创建 handler 闭包，捕获 reader 依赖。"""
    async def handler(args: dict, ctx: ToolContext) -> str:
        """搜索记忆存储中的条目，返回带评分的结果。"""
        query = args.get("query", "")
        limit = int(args.get("limit", 5))
        principal_id = ctx.principal_id or ""

        if not query:
            return "Please provide a query."

        if not principal_id:
            return f"[recall_memory] Search for '{query}': principal not available."

        if reader is None:
            return (
                f"[recall_memory] Search for '{query}': "
                "memory reader not available."
            )

        try:
            items = reader.retrieve(
                principal_id=principal_id,
                query=query,
                limit=min(limit, 20),
            )
        except Exception as e:
            return f"[recall_memory] Error searching memory: {e}"

        if not items:
            return f"No memories found matching '{query}'."

        lines = [f"Found {len(items)} memory result(s) for '{query}':"]
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. [{item.kind}] {item.subject}/{item.predicate} = "
                f"'{item.value}' "
                f"(confidence: {item.confidence:.1f})"
            )
        return "\n".join(lines)

    return handler


def create_tool_def(reader: MemoryReader | None = None) -> ToolDef:
    """创建 recall_memory 工具定义。

    Args:
        reader: MemoryReader 端口实例。
    """
    return ToolDef(
        name=TOOL_NAME,
        description=(
            "Search and retrieve relevant information from long-term memory. "
            "Memories include facts, preferences, past episodes, and goals."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to find in memory.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (1-20).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        toolset=("core", "memory"),
        handler=_make_handler(reader=reader),
        risk_level="low",
    )
