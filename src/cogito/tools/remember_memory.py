"""Remember memory tool — 保存长期记忆。

用户明确要求记住偏好、事实、约束或目标时，由模型主动调用。
通过 MemoryService 幂等写入，同值返回已有，不同值覆盖旧记忆。
"""

from __future__ import annotations

from cogito.capability.models import ToolContext, ToolDef
from cogito.service.memory_service import SqliteMemoryService

TOOL_NAME = "remember_memory"


def _make_handler(service: SqliteMemoryService | None = None):
    """创建 handler 闭包，捕获 service 依赖。"""
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

        if service is None:
            return (
                "[remember_memory] Cannot save memory: "
                "memory service not available."
            )

        # source 来自当前 Turn
        source_type = "turn"
        source_id = ctx.trace_id

        try:
            memory = service.remember(
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
    service: SqliteMemoryService | None = None,
) -> ToolDef:
    """创建 remember_memory 工具定义。

    Args:
        service: 可选的 MemoryService 实例。
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
        handler=_make_handler(service=service),
        risk_level="low",
    )
