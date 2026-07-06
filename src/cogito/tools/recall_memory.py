"""Recall memory tool — 检索长期记忆。

当前为暂存实现（返回"功能尚未实现"）。
TODO: 在 MEMORY-LIFECYCLE 实现后接入真实检索。
"""

from __future__ import annotations

from cogito.capability.models import ToolDef, ToolContext

TOOL_NAME = "recall_memory"


async def handler(args: dict, context: ToolContext) -> str:
    """搜索记忆存储中的条目。

    TODO: 接入 MemoryRetriever 真实实现。
    """
    query = args.get("query", "")
    if not query:
        return "Please provide a query."

    # 暂存实现
    return (
        f"[recall_memory] Search for '{query}' — "
        "memory retrieval not yet implemented."
    )


tool_def = ToolDef(
    name=TOOL_NAME,
    description="Search the long-term memory store for relevant past information.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to find in memory.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results.",
                "default": 5,
            },
        },
        "required": ["query"],
    },
    toolset=("core", "memory"),
    handler=handler,
    risk_level="low",
)
