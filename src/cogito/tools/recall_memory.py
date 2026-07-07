"""Recall memory tool — 检索长期记忆。

使用 MemoryService 或 MemoryRepository 查询有效记忆。
"""

from __future__ import annotations

from cogito.capability.models import ToolContext, ToolDef
from cogito.service.memory_service import SqliteMemoryService
from cogito.store.memory_repo import MemoryRepository

TOOL_NAME = "recall_memory"


def _make_handler(
    service: SqliteMemoryService | None = None,
    repo: MemoryRepository | None = None,
):
    """创建 handler 闭包，捕获 service/repo 依赖。"""
    async def handler(args: dict, ctx: ToolContext) -> str:
        """搜索记忆存储中的条目。"""
        query = args.get("query", "")
        limit = int(args.get("limit", 5))
        principal_id = ctx.principal_id or ""

        if not query:
            return "Please provide a query."

        if not principal_id:
            return f"[recall_memory] Search for '{query}': principal not available."

        # 优先使用 service，其次 repo
        if service:
            items = service.retrieve(
                principal_id=principal_id,
                query=query,
                limit=min(limit, 20),
            )
        elif repo:
            items = repo.search(
                principal_id=principal_id,
                query=query,
                limit=min(limit, 20),
            )
        else:
            return (
                f"[recall_memory] Search for '{query}': "
                "memory service not available."
            )

        if not items:
            return f"No memories found matching '{query}'."

        lines = [f"Found {len(items)} memory result(s) for '{query}':"]
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. [{item.kind}] {item.subject} "
                f"{item.predicate} {item.value} "
                f"(confidence: {item.confidence:.1f})"
            )
        return "\n".join(lines)

    return handler


def create_tool_def(
    repo: MemoryRepository | None = None,
    service: SqliteMemoryService | None = None,
) -> ToolDef:
    """创建 recall_memory 工具定义。

    Args:
        repo: 可选的 MemoryRepository。
        service: 可选的 MemoryService（优先于 repo）。
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
        handler=_make_handler(service=service, repo=repo),
        risk_level="low",
    )
