"""Search the durable knowledge base through the KnowledgeReader port."""
from __future__ import annotations

from cogito.capability.models import ToolContext, ToolDef
from cogito.contracts.context import KnowledgeReader

TOOL_NAME = "search_knowledge"


def _make_handler(reader: KnowledgeReader | None = None):
    async def handler(args: dict, ctx: ToolContext) -> str:
        query = str(args.get("query", "")).strip()
        limit = max(1, min(int(args.get("limit", 5)), 20))
        principal_id = ctx.principal_id or ""
        if not query:
            return "Please provide a query."
        if not principal_id:
            return "[search_knowledge] principal not available."
        if reader is None:
            return "[search_knowledge] knowledge reader not available."
        try:
            items = reader.retrieve(
                principal_id=principal_id,
                query=query,
                limit=limit,
            )
        except Exception as exc:
            return f"[search_knowledge] Error: {exc}"
        if not items:
            return f"No knowledge found matching '{query}'."
        lines = [f"Found {len(items)} knowledge result(s) for '{query}':"]
        for index, item in enumerate(items, 1):
            if isinstance(item, dict):
                text = item.get("text", "")
                title = item.get("title") or item.get("resource_id", "knowledge")
                score = float(item.get("score", 0.0))
            else:
                text = getattr(item, "text", "")
                title = getattr(item, "title", None) or getattr(item, "resource_id", "knowledge")
                score = float(getattr(item, "score", 0.0))
            lines.append(f"{index}. [{title}] {text} (score: {score:.3f})")
        return "\n".join(lines)

    return handler


def create_tool_def(reader: KnowledgeReader | None = None) -> ToolDef:
    return ToolDef(
        name=TOOL_NAME,
        description="Search durable documents and external knowledge available to the current principal.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Knowledge search query."},
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
        toolset=("core", "memory", "knowledge"),
        handler=_make_handler(reader),
        risk_level="low",
    )
