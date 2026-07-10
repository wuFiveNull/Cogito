"""ToolExecutor — 执行工具调用。

TOOL-SANDBOX / 1. 执行链：
ToolRequest → Registry resolve → input schema → Policy → persist → execute → persist → return ToolResult

当前阶段实现：
- Registry resolve
- Policy evaluation（TOOL-SANDBOX / 3）
- 参数校验（JSON Schema / TypeAdapter）
- Handler 调度
- 结果格式化
- ToolCallRepository 持久化（TOOL-SANDBOX / 2）
- 并发执行（asyncio.gather）
- 输出大小限制（TOOL-SANDBOX / 10）
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cogito.capability.models import ToolCallState, ToolContext, ToolDef, ToolResult
from cogito.capability.policy import ToolPolicy
from cogito.capability.registry import CapabilityRegistry
from cogito.contracts.clock import epoch_ms

# 最大输出字符数
MAX_TOOL_OUTPUT_CHARS = 100_000


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
    - 策略评估（allow/deny）
    - 校验参数
    - 执行 handler
    - 持久化 ToolCall 记录
    - 格式化 ToolResult
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        policy: ToolPolicy | None = None,
        sink: Any | None = None,  # ToolCallSink (PLAN-09 M4a)
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy or ToolPolicy()
        self._sink = sink  # ToolCallSink — 由组合根注入
        self._on_event = on_event  # DomainEvent 回调（D8）

    async def execute(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """执行单个工具调用。

        执行链：
        1. Registry resolve
        2. Policy evaluation
        3. 持久化 "executing" 状态
        4. 参数校验
        5. Handler 执行
        6. 结果截断
        7. 持久化最终状态
        """
        # 1. Resolve
        tool = self._registry.get(tool_name)
        if tool is None:
            return ToolResult(
                tool_call_id, tool_name, "error",
                error_message=f"Tool '{tool_name}' not found in registry",
            )

        # 2. Policy evaluation
        decision = self._policy.evaluate(tool_name, arguments, tool)
        if not decision.is_allowed:
            self._emit_event("ToolDenied", tool_name, "denied",
                             decision.reason, 0)
            return ToolResult(
                tool_call_id, tool_name, "error",
                error_message=f"Policy denied: {decision.reason}",
            )

        # 3. 持久化 executing + 计算幂等键（副作用 Tool 复用）
        self._persist_start(tool_call_id, context.attempt_id, tool_name, arguments)
        request_hash = _hash_arguments(tool_name, arguments)

        # 4. 参数校验
        try:
            validated = self._validate(tool, arguments)
        except ToolValidationError as e:
            self._persist_end(tool_call_id, "failed")
            return ToolResult(
                tool_call_id, tool_name, "error",
                error_message=str(e),
            )

        # 5. 执行
        started_at = datetime.now(UTC)
        try:
            result_text = await tool.handler(validated, context)
            duration = int((datetime.now(UTC) - started_at).total_seconds() * 1000)

            # 6. 结果截断
            result_text = self._truncate_output(result_text)

            self._persist_end(tool_call_id, "succeeded", result=result_text,
                              request_hash=request_hash)
            self._emit_event("ToolExecuted", tool_name, "success",
                             result_text, duration)
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status="success",
                result=result_text,
                duration_ms=duration,
            )
        except Exception as e:
            duration = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            self._persist_end(tool_call_id, "failed")
            self._emit_event("ToolExecuted", tool_name, "error",
                             str(e), duration)
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
        """并发执行多个工具调用。

        使用 asyncio.gather 并发执行无依赖的工具。
        结果按原始调用顺序稳定合并。
        """
        async def execute_one(call: ToolCallState) -> ToolResult:
            return await self.execute(
                call.tool_call_id,
                call.tool_name,
                call.arguments,
                context,
            )

        results = await asyncio.gather(
            *[execute_one(c) for c in calls],
            return_exceptions=True,
        )

        # 将异常转换为 ToolResult
        final: list[ToolResult] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                final.append(ToolResult(
                    tool_call_id=calls[i].tool_call_id,
                    tool_name=calls[i].tool_name,
                    status="error",
                    error_message=str(r),
                ))
            else:
                final.append(r)
        return final

    # ── 持久化 ──

    def _persist_start(
        self,
        tool_call_id: str,
        attempt_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        if not self._sink:
            return

        try:
            self._sink.insert({
                "tool_call_id": tool_call_id,
                "attempt_id": attempt_id,
                "attempt_type": "run",
                "tool_name": tool_name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
                "status": "executing",
                "started_at": epoch_ms(datetime.now(UTC)),
            })
        except Exception:
            pass  # 持久化失败不阻断主流程

    def _persist_end(
        self, tool_call_id: str, status: str,
        result: str = "", request_hash: str = "",
    ) -> None:
        if not self._sink:
            return
        try:
            self._sink.insert({
                "tool_call_id": tool_call_id,
                "status": status,
                "result_summary": result[:500] if result else "",
                "completed_at": epoch_ms(datetime.now(UTC)),
            })
        except Exception:
            pass

    # ── 参数校验 ──

    def _validate(
        self, tool: ToolDef, arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """使用 JSON Schema 校验参数。"""
        from pydantic import TypeAdapter

        schema = tool.input_schema
        adapter = TypeAdapter(dict[str, Any])

        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # 检查必填字段
        for field in required:
            if field not in arguments:
                raise ToolValidationError(
                    f"Tool '{tool.name}': validation error — missing required parameter '{field}'"
                )

        # 检查枚举约束
        for key, value in arguments.items():
            prop = properties.get(key, {})
            if "enum" in prop and value not in prop["enum"]:
                raise ToolValidationError(
                    f"Tool '{tool.name}': validation error — parameter '{key}' must be one of "
                    f"{prop['enum']}, got '{value}'"
                )

        # 类型校验通过 TypeAdapter
        try:
            adapter.validate_python(arguments)
        except Exception as e:
            raise ToolValidationError(
                f"Tool '{tool.name}': validation error — {e}"
            ) from e

        return arguments

    # ── 输出截断 ──

    @staticmethod
    def _truncate_output(text: str) -> str:
        if len(text) > MAX_TOOL_OUTPUT_CHARS:
            return (
                text[:MAX_TOOL_OUTPUT_CHARS]
                + f"\n... (truncated, {len(text)} chars)"
            )
        return text

    # ── 事件发布（D8） ──

    def _emit_event(
        self,
        event_type: str,
        tool_name: str,
        status: str,
        summary: str,
        duration_ms: int,
    ) -> None:
        if not self._on_event:
            return
        try:
            self._on_event({
                "event_type": event_type,
                "tool_name": tool_name,
                "status": status,
                "summary": summary[:200],
                "duration_ms": duration_ms,
            })
        except Exception:
            pass

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


def _hash_arguments(tool_name: str, arguments: dict[str, Any]) -> str:
    """计算副作用幂等键 hash（稳定序列化 + sha256）。"""
    import hashlib

    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()[:16]
