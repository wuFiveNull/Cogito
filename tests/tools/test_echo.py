"""Tests for builtin tools.

覆盖场景：
- echo 工具正确回显
- now 工具返回时间字符串
- recall_memory 返回占位消息
- discover_builtin_tools 注册所有内置工具
"""

from __future__ import annotations

import pytest

from cogito.capability.models import ToolContext
from cogito.tools.registry import discover_builtin_tools
from cogito.tools.recall_memory import create_tool_def


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(
        attempt_id="a1",
        trace_id="t1",
        tool_call_id="tc1",
    )


class TestEchoTool:
    @pytest.mark.asyncio
    async def test_echo_text(self, ctx):
        from cogito.tools.echo import handler

        result = await handler({"text": "hello world"}, ctx)
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_echo_empty(self, ctx):
        from cogito.tools.echo import handler

        result = await handler({"text": ""}, ctx)
        assert result == ""

    @pytest.mark.asyncio
    async def test_echo_missing_key(self, ctx):
        from cogito.tools.echo import handler

        result = await handler({}, ctx)
        assert result == ""


class TestNowTool:
    @pytest.mark.asyncio
    async def test_now_returns_string(self, ctx):
        from cogito.tools.now import handler

        result = await handler({}, ctx)
        assert isinstance(result, str)
        assert "UTC" in result
        assert "time" in result.lower()


class TestRecallMemoryTool:
    @pytest.mark.asyncio
    async def test_recall_memory_returns_no_service_message(self, ctx):
        tool_def = create_tool_def()  # no repo
        result = await tool_def.handler({"query": "test query"}, ctx)
        assert "not available" in result.lower()

    @pytest.mark.asyncio
    async def test_recall_memory_empty_query(self, ctx):
        tool_def = create_tool_def()
        result = await tool_def.handler({"query": ""}, ctx)
        assert "provide a query" in result.lower()

    @pytest.mark.asyncio
    async def test_create_tool_def_schema(self):
        tool_def = create_tool_def()
        assert tool_def.name == "recall_memory"
        assert tool_def.risk_level == "low"
        assert "query" in tool_def.input_schema.get("required", [])


class TestToolRegistry:
    def test_discover_registers_all_builtins(self):
        from cogito.capability import CapabilityRegistry

        r = CapabilityRegistry()
        discover_builtin_tools(r)

        assert "echo" in r
        assert "now" in r
        assert "recall_memory" in r
        assert len(r) >= 3

    def test_builtin_tool_schemas_are_valid(self):
        from cogito.capability import CapabilityRegistry

        r = CapabilityRegistry()
        discover_builtin_tools(r)

        schemas = r.get_openai_schemas()
        assert len(schemas) >= 3

        for s in schemas:
            assert s["type"] == "function"
            fn = s["function"]
            assert fn["name"]
            assert fn["description"]
            assert "parameters" in fn
            assert fn["parameters"]["type"] == "object"

    def test_builtin_tool_toolsets(self):
        from cogito.capability import CapabilityRegistry

        r = CapabilityRegistry()
        discover_builtin_tools(r)

        # echo and now are in "core" toolset
        core_tools = {t.name for t in r.list_by_toolset("core")}
        assert "echo" in core_tools
        assert "now" in core_tools

        # recall_memory is in both "core" and "memory"
        memory_tools = {t.name for t in r.list_by_toolset("memory")}
        assert "recall_memory" in memory_tools
