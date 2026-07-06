# cogito/agent/tools/builtin/memory_tools.py
#
# Built-in tools: recall_memory, memorize, forget_memory — long-term memory.

from __future__ import annotations

from typing import Mapping

from cogito.agent.domain.tools import (
    ToolConcurrencyMode,
    ToolDefinition,
    ToolKind,
    ToolLimits,
    ToolRisk,
    ToolRiskLevel,
    ToolSideEffect,
    ToolSource,
    ToolSourceType,
)


class RecallMemoryHandler:
    """Handler for recall_memory — retrieves memories by key or search."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="recall_memory",
            description="Recall stored memories. Returns memories matching the query key or content.",
            input_schema={
                "type": "object", "properties": {
                    "key": {"type": "string", "description": "Memory key to look up"},
                    "query": {"type": "string", "description": "Search text for memory content"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE, risk_level=ToolRiskLevel.LOW,
            timeout_seconds=10.0, idempotent=True, parallel_safe=True,
            kind=ToolKind.MEMORY, risk=ToolRisk.READ_ONLY,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.PARALLEL_SAFE,
            limits=ToolLimits(timeout_seconds=10.0, max_result_chars=10_000),
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        key = arguments.get("key")
        query = arguments.get("query", "")
        return {"memories": [], "note": "recall_memory requires a database connection"}


class MemorizeHandler:
    """Handler for memorize — stores a memory."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="memorize",
            description="Store a memory for future recall. The assistant can remember facts, preferences, and information.",
            input_schema={
                "type": "object", "properties": {
                    "key": {"type": "string", "minLength": 1, "description": "Unique memory key"},
                    "content": {"type": "string", "minLength": 1, "description": "Content to remember"},
                    "type": {"type": "string", "enum": ["fact", "preference", "rule"], "description": "Memory type"},
                },
                "required": ["key", "content"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.LOCAL_MUTATION, risk_level=ToolRiskLevel.MEDIUM,
            timeout_seconds=10.0, idempotent=True, parallel_safe=False,
            kind=ToolKind.MEMORY, risk=ToolRisk.LOCAL_WRITE,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_SESSION,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        return {"note": "memorize requires a database connection"}


class ForgetMemoryHandler:
    """Handler for forget_memory — soft-deletes a memory."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="forget_memory",
            description="Delete a stored memory by key. The memory will no longer be recalled.",
            input_schema={
                "type": "object", "properties": {
                    "key": {"type": "string", "minLength": 1, "description": "Memory key to forget"},
                },
                "required": ["key"], "additionalProperties": False,
            },
            side_effect=ToolSideEffect.LOCAL_MUTATION, risk_level=ToolRiskLevel.MEDIUM,
            timeout_seconds=10.0, idempotent=False, parallel_safe=False,
            kind=ToolKind.MEMORY, risk=ToolRisk.LOCAL_WRITE,
            source=ToolSource(type=ToolSourceType.BUILTIN, provider="builtin"),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_SESSION,
        )

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        return {"note": "forget_memory requires a database connection"}
