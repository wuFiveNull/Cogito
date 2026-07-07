"""Forget memory tool — 删除长期记忆。

支持：
1. 按 memory_id 精确删除
2. 按 subject + predicate 模糊删除
3. 存在歧义时返回候选，不直接批量删除
"""

from __future__ import annotations

from cogito.capability.models import ToolContext, ToolDef
from cogito.service.memory_service import SqliteMemoryService

TOOL_NAME = "forget_memory"


def _make_handler(service: SqliteMemoryService | None = None):
    """创建 handler 闭包，捕获 service 依赖。"""
    async def handler(args: dict, ctx: ToolContext) -> str:
        """从长期记忆中删除条目。"""
        memory_id = args.get("memory_id", "")
        subject = args.get("subject", "")
        predicate = args.get("predicate", "")
        query = args.get("query", "")

        principal_id = ctx.principal_id or ""
        if not principal_id:
            return (
                "[forget_memory] Cannot forget: "
                "principal not available in current context."
            )

        if service is None:
            return (
                "[forget_memory] Cannot forget: "
                "memory service not available."
            )

        # ── 按 memory_id 精确删除 ──
        if memory_id:
            memory = service.get(memory_id)
            if memory is None:
                return f"[forget_memory] No memory found with id '{memory_id}'."

            ok = service.forget(memory_id)
            if ok:
                return (
                    f"Forgot memory: [{memory.kind}] {memory.subject}/"
                    f"{memory.predicate} = '{memory.value}' (memory_id={memory_id})"
                )
            return f"[forget_memory] Failed to forget memory '{memory_id}'."

        # ── 按 subject + predicate 删除 ──
        if subject and predicate:
            ok = service.forget_by_canonical_key(principal_id, subject, predicate)
            if ok:
                return (
                    f"Forgot memory: subject='{subject}', predicate='{predicate}'."
                )
            return (
                f"[forget_memory] No active memory found for "
                f"subject='{subject}', predicate='{predicate}'."
            )

        # ── 按自然语言搜索后提示 ──
        if query:
            candidates = service.retrieve(
                principal_id=principal_id,
                query=query,
                limit=10,
            )
            if not candidates:
                return (
                    f"[forget_memory] No memories found matching '{query}'."
                )

            lines = [f"Found {len(candidates)} memory candidate(s) for '{query}':"]
            for i, m in enumerate(candidates, 1):
                lines.append(
                    f"  {i}. memory_id={m.memory_id}: "
                    f"[{m.kind}] {m.subject}/{m.predicate} = '{m.value}'"
                )
            lines.append(
                "Specify the memory_id to forget, "
                "or refine your search with more specific terms."
            )
            return "\n".join(lines)

        return (
            "[forget_memory] Please specify one of: "
            "memory_id, subject+predicate, or query."
        )

    return handler


def create_tool_def(
    service: SqliteMemoryService | None = None,
) -> ToolDef:
    """创建 forget_memory 工具定义。

    Args:
        service: 可选的 MemoryService 实例。
    """
    return ToolDef(
        name=TOOL_NAME,
        description=(
            "Forget/delete a piece of information from long-term memory. "
            "Use when the user asks to forget a fact, preference, or any "
            "previously remembered information."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Exact memory_id to forget.",
                },
                "subject": {
                    "type": "string",
                    "description": "Subject of the memory to forget (paired with predicate).",
                },
                "predicate": {
                    "type": "string",
                    "description": "Predicate of the memory to forget (paired with subject).",
                },
                "query": {
                    "type": "string",
                    "description": "Natural language search to find memories to forget.",
                },
            },
            "anyOf": [
                {"required": ["memory_id"]},
                {"required": ["subject", "predicate"]},
                {"required": ["query"]},
            ],
        },
        toolset=("core", "memory"),
        handler=_make_handler(service=service),
        risk_level="low",
    )
