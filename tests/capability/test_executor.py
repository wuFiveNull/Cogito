"""Tests for ToolExecutor.

覆盖场景：
- 简单工具执行
- 参数校验（必填、枚举）
- 未知工具
- handler 异常处理
- 批量执行
- 结果格式化
"""

from __future__ import annotations

import pytest

from cogito.capability import CapabilityRegistry
from cogito.capability.executor import ToolExecutor
from cogito.capability.models import (
    ToolCallState,
    ToolContext,
    ToolDef,
    ToolResult,
)


def _async_handler(fn):
    """Wrap a sync lambda as an async handler."""

    async def wrapper(args, context):
        return fn(args, context)

    return wrapper


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(attempt_id="a1", trace_id="t1", tool_call_id="tc1")


@pytest.fixture
def registry() -> CapabilityRegistry:
    r = CapabilityRegistry()

    r.register(
        ToolDef(
            name="greet",
            description="Greet someone",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name to greet"},
                },
                "required": ["name"],
            },
            handler=_async_handler(lambda args, _: f"Hello, {args['name']}!"),
            risk_level="low",
        )
    )

    r.register(
        ToolDef(
            name="add",
            description="Add two numbers",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
            handler=_async_handler(lambda args, _: str(args["a"] + args["b"])),
            risk_level="low",
        )
    )

    r.register(
        ToolDef(
            name="fail_tool",
            description="Always fails",
            input_schema={"type": "object", "properties": {}},
            handler=_async_handler(
                lambda args, _: (_ for _ in ()).throw(RuntimeError("Something broke"))
            ),
            risk_level="medium",
        )
    )

    r.register(
        ToolDef(
            name="enum_tool",
            description="Tool with enum constraint",
            input_schema={
                "type": "object",
                "properties": {
                    "choice": {
                        "type": "string",
                        "enum": ["a", "b", "c"],
                    },
                },
                "required": ["choice"],
            },
            handler=_async_handler(lambda args, _: f"Chose {args['choice']}"),
            risk_level="low",
        )
    )

    return r


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_execute_simple(self, registry, ctx):
        executor = ToolExecutor(registry)
        result = await executor.execute("tc1", "greet", {"name": "Alice"}, ctx)

        assert result.tool_call_id == "tc1"
        assert result.tool_name == "greet"
        assert result.status == "success"
        assert result.result == "Hello, Alice!"
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_execute_with_numbers(self, registry, ctx):
        executor = ToolExecutor(registry)
        result = await executor.execute("tc1", "add", {"a": 3, "b": 5}, ctx)

        assert result.status == "success"
        assert result.result == "8"

    @pytest.mark.asyncio
    async def test_tool_not_found(self, registry, ctx):
        executor = ToolExecutor(registry)
        result = await executor.execute("tc1", "nonexistent", {}, ctx)
        assert result.status == "error"
        assert "not found" in result.error_message

    @pytest.mark.asyncio
    async def test_missing_required_param(self, registry, ctx):
        executor = ToolExecutor(registry)
        result = await executor.execute("tc1", "greet", {}, ctx)
        assert result.status == "error"
        assert "missing required" in result.error_message

    @pytest.mark.asyncio
    async def test_enum_validation(self, registry, ctx):
        executor = ToolExecutor(registry)

        # 合法值
        result = await executor.execute("tc1", "enum_tool", {"choice": "a"}, ctx)
        assert result.status == "success"

        # 非法值返回 error
        result = await executor.execute("tc1", "enum_tool", {"choice": "x"}, ctx)
        assert result.status == "error"
        assert "validation error" in result.error_message

    @pytest.mark.asyncio
    async def test_handler_error_returns_error_status(self, registry, ctx):
        executor = ToolExecutor(registry)
        result = await executor.execute("tc1", "fail_tool", {}, ctx)

        assert result.status == "error"
        assert "Something broke" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_many_sequential(self, registry, ctx):
        executor = ToolExecutor(registry)
        calls = [
            ToolCallState(tool_call_id="tc1", tool_name="greet", arguments={"name": "Alice"}),
            ToolCallState(tool_call_id="tc2", tool_name="add", arguments={"a": 1, "b": 2}),
        ]

        results = await executor.execute_many(calls, ctx)
        assert len(results) == 2
        assert results[0].status == "success"
        assert results[0].result == "Hello, Alice!"
        assert results[1].status == "success"
        assert results[1].result == "3"

    @pytest.mark.asyncio
    async def test_execute_many_with_error(self, registry, ctx):
        executor = ToolExecutor(registry)
        calls = [
            ToolCallState(tool_call_id="tc1", tool_name="greet", arguments={"name": "Alice"}),
            ToolCallState(tool_call_id="tc2", tool_name="fail_tool", arguments={}),
        ]

        results = await executor.execute_many(calls, ctx)
        assert len(results) == 2
        assert results[0].status == "success"
        assert results[1].status == "error"

    def test_format_tool_message(self):
        result = ToolResult(
            tool_call_id="tc1",
            tool_name="echo",
            status="success",
            result="Hello",
        )
        msg = ToolExecutor.format_tool_message("tc1", result)
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "tc1"
        assert msg["content"] == "Hello"

    def test_format_tool_message_error(self):
        result = ToolResult(
            tool_call_id="tc1",
            tool_name="fail",
            status="error",
            error_message="Something broke",
        )
        msg = ToolExecutor.format_tool_message("tc1", result)
        assert msg["role"] == "tool"
        assert "Error:" in msg["content"]
        assert "Something broke" in msg["content"]

    def test_format_tool_results(self):
        results = [
            ToolResult(tool_call_id="tc1", tool_name="a", status="success", result="OK"),
            ToolResult(tool_call_id="tc2", tool_name="b", status="error", error_message="Fail"),
        ]
        msgs = ToolExecutor.format_tool_results(results)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "tool"
        assert msgs[1]["role"] == "tool"
