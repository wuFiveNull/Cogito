"""Always-visible tool discovery and per-Turn todo tools."""

from __future__ import annotations

import json
from typing import Any

from cogito.capability.models import ToolContext, ToolDef
from cogito.capability.registry import CapabilityRegistry


def create_tool_defs(registry: CapabilityRegistry) -> list[ToolDef]:
    async def tool_search(args: dict[str, Any], ctx: ToolContext) -> str:
        tools = registry.search(
            str(args.get("query", "")),
            limit=int(args.get("limit", 10)),
        )
        if ctx.capability_snapshot_ids:
            allowed = set(ctx.capability_snapshot_ids)
            tools = [tool for tool in tools if tool.capability_id in allowed]
        items = []
        for tool in tools:
            activated = bool(ctx.expose_tool and ctx.expose_tool(tool.capability_id))
            items.append(
                {
                    "name": tool.name,
                    "capability_id": tool.capability_id,
                    "description": tool.description,
                    "toolsets": tool.toolset,
                    "risk_level": tool.risk_level,
                    "source": tool.namespace,
                    "activated": activated,
                }
            )
        return json.dumps({"tools": items}, ensure_ascii=False)

    async def tool_describe(args: dict[str, Any], ctx: ToolContext) -> str:
        result = []
        for name in args.get("names", []):
            tool = registry.get(str(name))
            if tool is None:
                continue
            if ctx.expose_tool:
                ctx.expose_tool(tool.capability_id)
            result.append(
                {
                    "name": tool.name,
                    "capability_id": tool.capability_id,
                    "description": tool.description,
                    "schema": tool.input_schema,
                    "permissions": tool.permissions,
                    "risk_level": tool.risk_level,
                    "side_effect_class": tool.side_effect_class,
                    "toolsets": tool.toolset,
                    "source": tool.namespace,
                }
            )
        return json.dumps({"tools": result}, ensure_ascii=False)

    async def todo_read(_: dict[str, Any], ctx: ToolContext) -> str:
        return json.dumps({"items": ctx.tool_state.get("todos", [])}, ensure_ascii=False)

    async def todo_write(args: dict[str, Any], ctx: ToolContext) -> str:
        normalized = []
        for index, item in enumerate(args.get("items", [])):
            normalized.append(
                {
                    "id": str(item.get("id") or index + 1),
                    "content": str(item.get("content", ""))[:1_000],
                    "status": str(item.get("status", "pending")),
                }
            )
        ctx.tool_state["todos"] = normalized[:100]
        return json.dumps({"items": ctx.tool_state["todos"]}, ensure_ascii=False)

    object_schema = {"type": "object", "additionalProperties": False}
    output_schema = {"type": "object"}
    return [
        ToolDef(
            "tool_search",
            "Search and activate tools relevant to the current task.",
            {
                **object_schema,
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
            tool_search,
            toolset=("core",),
            output_schema=output_schema,
        ),
        ToolDef(
            "tool_describe",
            "Inspect schemas and security metadata for tools.",
            {
                **object_schema,
                "properties": {"names": {"type": "array", "items": {"type": "string"}}},
                "required": ["names"],
            },
            tool_describe,
            toolset=("core",),
            output_schema=output_schema,
        ),
        ToolDef(
            "todo_read",
            "Read the current Turn's todo list.",
            object_schema,
            todo_read,
            toolset=("core",),
            output_schema=output_schema,
        ),
        ToolDef(
            "todo_write",
            "Replace the current Turn's todo list.",
            {
                **object_schema,
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["items"],
            },
            todo_write,
            toolset=("core",),
            risk_level="low",
            side_effect_class="idempotent",
            output_schema=output_schema,
        ),
    ]
