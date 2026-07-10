"""Forget memory tool — 删除长期记忆。

支持：
1. 按 memory_id 精确删除
2. 按 subject + predicate 模糊删除
3. 自然语言搜索 → 返回候选让用户确认

边界（PLAN-09 M4a）：工具通过 MemoryReader / MemoryWriter 端口操作，
不直接依赖 SqliteMemoryService。组合根注入具体实现。
"""
from __future__ import annotations

from cogito.capability.models import ToolContext, ToolDef
from cogito.contracts.memory import MemoryReader, MemoryWriter

TOOL_NAME = "forget_memory"


def _make_handler(
    reader: MemoryReader | None = None,
    writer: MemoryWriter | None = None,
):
    """创建 handler 闭包。

    Args:
        reader: MemoryReader 端口（用于读取候选 / 校验所有权）。
        writer: MemoryWriter 端口（用于 forget / forget_by_canonical_key）。
    """
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

        # ── 按 memory_id 精确删除 ──
        if memory_id:
            if writer is None:
                return "[forget_memory] Cannot forget: memory writer not available."

            # 校验所有权（reader 存在时预检查）
            if reader is not None:
                mem = reader.get(memory_id)
                if mem is None:
                    return (
                        f"[forget_memory] No memory found with id '{memory_id}'."
                    )
                if mem.principal_id and mem.principal_id != principal_id:
                    return (
                        f"[forget_memory] Memory '{memory_id}' does not "
                        f"belong to current principal."
                    )

            ok = writer.forget(memory_id, principal_id=principal_id)
            if ok:
                return f"Forgot memory with id='{memory_id}'."
            # forget 返回 False 时可能是已不存在（并发或已删除）
            return (
                f"[forget_memory] No memory found with id '{memory_id}'."
                if reader is None else
                f"[forget_memory] Failed to forget memory '{memory_id}'."
            )

        # ── 按 subject + predicate 删除 ──
        if subject and predicate:
            if writer is None:
                return "[forget_memory] Cannot forget: memory writer not available."

            ok = writer.forget_by_canonical_key(principal_id, subject, predicate)
            if ok:
                return (
                    f"Forgot memory: subject='{subject}', "
                    f"predicate='{predicate}'."
                )
            return (
                f"[forget_memory] No active memory found for "
                f"subject='{subject}', predicate='{predicate}'."
            )

        # ── 按自然语言搜索后提示 ──
        if query:
            if reader is None:
                return (
                    f"[forget_memory] Search for '{query}': "
                    "memory reader not available."
                )
            try:
                candidates = reader.retrieve(
                    principal_id=principal_id,
                    query=query,
                    limit=10,
                )
            except Exception as e:
                return f"[forget_memory] Error searching memory: {e}"

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
    reader: MemoryReader | None = None,
    writer: MemoryWriter | None = None,
) -> ToolDef:
    """创建 forget_memory 工具定义。

    Args:
        reader: MemoryReader 端口实例。
        writer: MemoryWriter 端口实例。
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
        handler=_make_handler(reader=reader, writer=writer),
        risk_level="low",
    )
