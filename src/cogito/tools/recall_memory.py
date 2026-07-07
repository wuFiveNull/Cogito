"""Recall memory tool — 检索长期记忆。

使用 MemoryRepository 查询已确认的 memory_items。
"""

from __future__ import annotations

from cogito.capability.models import ToolContext, ToolDef
from cogito.store.memory_repo import MemoryRepository

TOOL_NAME = "recall_memory"

# 在注册时通过闭包注入 repo
_repo: MemoryRepository | None = None


def _set_repo(repo: MemoryRepository) -> None:
    global _repo
    _repo = repo


async def handler(args: dict, ctx: ToolContext) -> str:
    """搜索记忆存储中的条目。"""
    query = args.get("query", "")
    limit = args.get("limit", 5)

    if not query:
        return "Please provide a query."

    if _repo is None:
        return (
            f"[recall_memory] Search for '{query}': "
            "memory service not available."
        )

    items = _repo.search(query, limit=int(limit))
    if not items:
        return (
            f"No memories found matching '{query}'."
        )

    lines = [f"Found {len(items)} memory result(s) for '{query}':"]
    for i, item in enumerate(items, 1):
        lines.append(
            f"{i}. [{item['kind']}] {item.get('subject', '')} "
            f"{item.get('predicate', '')} {item.get('value', '')} "
            f"(confidence: {item.get('confidence', 0):.1f})"
        )
    return "\n".join(lines)


def create_tool_def(repo: MemoryRepository | None = None) -> ToolDef:
    """创建 recall_memory 工具定义。

    Args:
        repo: 可选的 MemoryRepository。传入后在 handler 中使用真实查询。
    """
    if repo is not None:
        _set_repo(repo)

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
        handler=handler,
        risk_level="low",
    )
