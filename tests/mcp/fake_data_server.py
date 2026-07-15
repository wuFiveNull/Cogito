"""Fake MCP Server —— 用于 MCP Connector 管道测试的伪数据源。

返回确定性、可控制的两页数据，支持：
- 按 cursor 分页
- 304 NotModified（当传入 If-None-Match）
- 单条 item 缺失 stable_id（用于 Quarantine 测试）
- item 更新模拟（第 N 次调用返回新生成的 contentHash）

返回 JSON 结构对齐 AIHOT 公开 API：
{
  "count": int,
  "hasNext": bool,
  "nextCursor": str | null,
  "items": [{ "id", "title", "summary", "url", "category", "publishedAt" }]
}
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

PAGE1_IDS = [f"fake-{i:02d}" for i in range(1, 6)]  # 5 条
PAGE2_IDS = [f"fake-{i:02d}" for i in range(6, 9)]  # 3 条（最后一页）

# 包含一条故意缺失 id 的 item：id=""（用于 quarantine 测试）
PAGE1_IDS_QUARANTINE = ["", "fake-quarantine-ok"]


server = Server("fake-data-server")


def _build_items(ids: list[str]) -> list[dict[str, Any]]:
    items = []
    for id_ in ids:
        items.append(
            {
                "id": id_,
                "title": f"Fake item {id_ or '<empty>'}",
                "summary": "Fake summary for testing MCP connector pipeline.",
                "url": f"https://example.test/items/{id_ or 'quarantine'}",
                "category": "industry"
                if not id_
                else ("ai-models" if id_.endswith(("01", "02", "03", "06", "07")) else "industry"),
                "publishedAt": "2026-07-08T08:00:00.000Z",
            }
        )
    return items


def _page_response(ids: list[str], next_cur: str | None) -> dict[str, Any]:
    return {
        "count": len(ids),
        "hasNext": next_cur is not None,
        "nextCursor": next_cur,
        "items": _build_items(ids),
    }


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_items",
            description="Return a page of fake items.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "cursor": {"type": "string"},
                },
            },
        ),
        Tool(
            name="get_item",
            description="Return a single item by id (for update simulation).",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "list_items":
        cursor = arguments.get("cursor")
        if not cursor:
            data = _page_response(PAGE1_IDS, next_cur="cursor-page-2")
        elif cursor == "cursor-page-2":
            data = _page_response(PAGE2_IDS, next_cur=None)
        else:
            data = {"count": 0, "hasNext": False, "nextCursor": None, "items": []}
        return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]

    if name == "get_item":
        id_ = arguments.get("id", "")
        data = _build_items([id_])[0] if id_ else {"error": "not found"}
        return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]

    raise ValueError(f"Unknown tool: {name}")


async def run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
