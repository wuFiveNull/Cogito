"""ToolExecutor — 执行工具调用。

TOOL-SANDBOX / 1. 执行链：
ToolRequest → Registry resolve → input schema → Policy → execute → return ToolResult

当前阶段实现：
- Registry resolve
- 参数校验（JSON Schema / TypeAdapter）
- Handler 调度
- 结果格式化

Policy 集成见 Phase 6。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from cogito.capability.models import ToolCallState, ToolContext, ToolDef, ToolResult
from cogito.capability.registry import CapabilityRegistry


class ToolValidationError(Exception):
    """参数校验失败。"""
    pass


class ToolExecutionError(Exception):
    """工具执行失败。"""
    pass


class ToolExecutor:
    """工具执行器。

    职责：
    - 按名称解析 ToolDef
    - 校验参数
    - 执行 handler
    - 格式化 ToolResult
    """

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    async def execute(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """执行单个工具调用。

        Args:
            tool_call_id: 本次调用的 ID（来自模型响应）。
            tool_name: 工具名称。
            arguments: 参数（JSON 对象）。
            context: 执行上下文（attempt_id, trace_id）。

        Returns: ToolResult（成功或异常）。

        Raises:
            KeyError: 工具未注册。
            ToolValidationError: 参数校验失败。
        """
        # 1. Resolve
        tool = self._registry.get(tool_name)
        if tool is None:
            raise KeyError(f"Tool '{tool_name}' not found in registry")

        # 2. 参数校验
        validated = self._validate(tool, arguments)

        # 3. 执行
        started_at = datetime.now(UTC)
        try:
            result_text = await tool.handler(validated, context)
            duration = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status="success",
                result=result_text,
                duration_ms=duration,
            )
        except Exception as e:
            duration = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status="error",
                error_message=str(e),
                duration_ms=duration,
            )

    async def execute_many(
        self,
        calls: list[ToolCallState],
        context: ToolContext,
    ) -> list[ToolResult]:
        """顺序执行多个工具调用。

        并行执行需在调用方通过 asyncio.gather 编排。
        """
        results: list[ToolResult] = []
        for call in calls:
            result = await self.execute(
                call.tool_call_id,
                call.tool_name,
                call.arguments,
                context,
            )
            results.append(result)
        return results

    # ── 参数校验 ──

    def _validate(
        self, tool: ToolDef, arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """使用 JSON Schema 校验参数。"""
        from pydantic import TypeAdapter

        schema = tool.input_schema
        adapter = TypeAdapter(dict[str, Any])

        # 简单参数校验：检查必填字段和类型
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # 检查必填字段
        for field in required:
            if field not in arguments:
                raise ToolValidationError(
                    f"Tool '{tool.name}': missing required parameter '{field}'"
                )

        # 检查枚举约束
        for key, value in arguments.items():
            prop = properties.get(key, {})
            if "enum" in prop and value not in prop["enum"]:
                raise ToolValidationError(
                    f"Tool '{tool.name}': parameter '{key}' must be one of "
                    f"{prop['enum']}, got '{value}'"
                )

        # 类型校验通过 TypeAdapter
        try:
            adapter.validate_python(arguments)
        except Exception as e:
            raise ToolValidationError(
                f"Tool '{tool.name}': argument validation failed: {e}"
            ) from e

        return arguments

    # ── 结果格式化 ──

    @staticmethod
    def format_tool_message(
        tool_call_id: str,
        result: ToolResult,
    ) -> dict[str, Any]:
        """格式化为 tool role 消息，供下一轮模型请求使用。"""
        content = result.result if result.status == "success" else (
            f"Error: {result.error_message}"
        )
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }

    @staticmethod
    def format_tool_results(
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        """批量格式化 tool result 消息。"""
        return [
            ToolExecutor.format_tool_message(r.tool_call_id, r)
            for r in results
        ]
