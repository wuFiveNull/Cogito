"""Remember memory tool — 保存长期记忆。

用户明确要求记住偏好、事实、约束或目标时，由模型主动调用。
通过 MemoryWriter 幂等写入，同值返回已有，不同值覆盖旧记忆。

边界（PLAN-09 M4a）：工具文件不直接依赖 SqliteMemoryService，
只依赖 contracts.memory.MemoryWriter 端口。组合根负责把具体实现
注入 writer (或 make_writer 工厂)。
"""
from __future__ import annotations

from collections.abc import Callable

from cogito.capability.models import ToolContext, ToolDef
from cogito.contracts.memory import MemoryWriter

TOOL_NAME = "remember_memory"


def _make_handler(
    writer: MemoryWriter | None = None,
    make_writer: Callable[[], MemoryWriter] | None = None,
):
    """创建 handler 闭包。

    Args:
        writer: 推荐的 MemoryWriter 实例（组合根注入的具体实现）。
        make_writer: 按需创建 writer 的工厂（每次写操作 fresh writer，
                    隐含独立事务语义）。优先于此参数。
    """
    async def handler(args: dict, ctx: ToolContext) -> str:
        """保存一个条目到长期记忆。"""
        kind = args.get("kind", "fact")
        subject = args.get("subject", "")
        predicate = args.get("predicate", "")
        value = args.get("value", "")
        scope_type = args.get("scope_type", "")
        scope_id = args.get("scope_id", "")
        confidence = float(args.get("confidence", 1.0))
        importance = float(args.get("importance", 0.7))
        explicitness = args.get("explicitness", "explicit_user_statement")

        principal_id = ctx.principal_id or ""
        if not principal_id:
            return (
                "[remember_memory] Cannot save memory: "
                "principal not available in current context."
            )

        if not subject and not predicate and not value:
            return (
                "[remember_memory] Cannot save memory: "
                "at least one of subject, predicate, or value is required."
            )

        source_type = "message"
        source_id = getattr(ctx, "input_message_id", ctx.trace_id) or ctx.trace_id

        # 解析 writer：优先用 make_writer（独立事务），其次用共享 writer
        w = None
        if make_writer is not None:
            try:
                w = make_writer()
            except Exception as e:
                return f"[remember_memory] Cannot create memory writer: {e}"
        else:
            w = writer

        if w is None:
            return (
                "[remember_memory] Cannot save memory: "
                "memory writer not available."
            )

        try:
            memory = w.remember(
                kind=kind,
                subject=subject,
                predicate=predicate,
                value=value,
                principal_id=principal_id,
                scope_type=scope_type,
                scope_id=scope_id,
                source_type=source_type,
                source_id=source_id,
                explicitness=explicitness,
                confidence=min(confidence, 1.0),
                importance=min(importance, 1.0),
            )
        except Exception as e:
            return f"[remember_memory] Error saving memory: {e}"

        return (
            f"Saved memory: [{memory.kind}] {memory.subject}/{memory.predicate} = "
            f"'{memory.value}' (confidence={memory.confidence:.1f}, "
            f"memory_id={memory.memory_id})"
        )

    return handler


def create_tool_def(
    writer: MemoryWriter | None = None,
    make_writer: Callable[[], MemoryWriter] | None = None,
) -> ToolDef:
    """创建 remember_memory 工具定义。

    Args:
        writer: MemoryWriter 端口实例。
        make_writer: 工厂，每次写操作创建 fresh writer（独立事务）。
    """
    return ToolDef(
        name=TOOL_NAME,
        description=(
            "Save or update a piece of information in long-term memory. "
            "Use when the user explicitly asks you to remember a fact, "
            "preference, constraint, or goal."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["fact", "preference", "episode", "goal", "constraint"],
                    "description": "Type of memory.",
                },
                "subject": {
                    "type": "string",
                    "description": "The subject this memory is about (e.g. 'user', 'project').",
                },
                "predicate": {
                    "type": "string",
                    "description": "The attribute or relation (e.g. 'preferred_language').",
                },
                "value": {
                    "type": "string",
                    "description": "The value or content of the memory.",
                },
                "scope_type": {
                    "type": "string",
                    "enum": ["", "global", "user", "conversation", "session", "task"],
                    "description": "Scope type for isolation.",
                },
                "scope_id": {
                    "type": "string",
                    "description": "Scope identifier.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence level (0.0-1.0).",
                    "default": 1.0,
                },
                "importance": {
                    "type": "number",
                    "description": "Importance level (0.0-1.0).",
                    "default": 0.7,
                },
                "explicitness": {
                    "type": "string",
                    "enum": [
                        "explicit_user_statement",
                        "confirmed_inference",
                        "model_inference",
                        "external_source",
                        "system_generated",
                    ],
                    "description": "How explicit the user was.",
                },
            },
            "required": ["subject", "predicate", "value"],
        },
        toolset=("core", "memory"),
        handler=_make_handler(writer=writer, make_writer=make_writer),
        risk_level="low",
    )
