"""Tests for CapabilityRegistry.

覆盖场景：
- 注册和查询
- 按 toolset 过滤
- 按模式过滤
- OpenAI schema 输出格式
- 命名冲突
- KeyError 处理
"""

from __future__ import annotations

import pytest

from cogito.capability.models import ToolDef
from cogito.capability.registry import CapabilityRegistry


def _make_tool(
    name: str = "test_tool",
    description: str = "A test tool",
    toolsets: tuple[str, ...] = ("core",),
    modes: tuple[str, ...] = (),
) -> ToolDef:
    return ToolDef(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
        toolset=toolsets,
        handler=lambda args, ctx: "ok",
        supported_modes=modes,
    )


class TestCapabilityRegistry:
    def test_register_and_get(self):
        registry = CapabilityRegistry()
        tool = _make_tool("echo")
        registry.register(tool)

        assert registry.get("echo") is tool
        assert "echo" in registry

    def test_get_nonexistent_returns_none(self):
        registry = CapabilityRegistry()
        assert registry.get("nonexistent") is None

    def test_resolve_returns_tool(self):
        registry = CapabilityRegistry()
        tool = _make_tool("my_tool")
        registry.register(tool)

        assert registry.resolve("my_tool") is tool

    def test_resolve_nonexistent_raises(self):
        registry = CapabilityRegistry()
        with pytest.raises(KeyError, match="not found"):
            registry.resolve("ghost_tool")

    def test_register_overwrite_warns(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("dup"))
        with pytest.warns(UserWarning, match="overwriting"):
            registry.register(_make_tool("dup"))

    def test_all_tools(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("a"))
        registry.register(_make_tool("b"))

        assert len(registry.all_tools()) == 2

    def test_list_by_toolset(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("core_tool", toolsets=("core",)))
        registry.register(_make_tool("mem_tool", toolsets=("memory",)))

        core_tools = registry.list_by_toolset("core")
        assert len(core_tools) == 1
        assert core_tools[0].name == "core_tool"

        mem_tools = registry.list_by_toolset("memory")
        assert len(mem_tools) == 1
        assert mem_tools[0].name == "mem_tool"

        search_tools = registry.list_by_toolset("search")
        assert len(search_tools) == 0

    def test_list_by_toolsets(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("a", toolsets=("core",)))
        registry.register(_make_tool("b", toolsets=("memory",)))
        registry.register(_make_tool("c", toolsets=("search",)))

        result = registry.list_by_toolsets({"core", "memory"})
        names = {t.name for t in result}
        assert names == {"a", "b"}

    def test_list_by_mode(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("all_mode"))  # no supported_modes → all
        registry.register(_make_tool(
            "reactive_only", modes=("reactive",),
        ))

        reactive = registry.list_by_mode("reactive")
        assert {t.name for t in reactive} == {"all_mode", "reactive_only"}

        maintenance = registry.list_by_mode("maintenance")
        assert {t.name for t in maintenance} == {"all_mode"}

    def test_get_openai_schemas_all(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("tool_a", description="Tool A"))
        registry.register(_make_tool("tool_b", description="Tool B"))

        schemas = registry.get_openai_schemas()
        assert len(schemas) == 2

        for s in schemas:
            assert s["type"] == "function"
            assert "function" in s
            assert s["function"]["name"] in ("tool_a", "tool_b")
            assert isinstance(s["function"]["parameters"], dict)

    def test_get_openai_schemas_filtered(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("core_tool", toolsets=("core",)))
        registry.register(_make_tool("mem_tool", toolsets=("memory",)))

        schemas = registry.get_openai_schemas({"core"})
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "core_tool"

    def test_get_openai_schemas_none_for_unknown_toolset(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("core_tool", toolsets=("core",)))

        schemas = registry.get_openai_schemas({"nonexistent"})
        assert schemas == []

    def test_get_schemas_by_mode(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("all_mode"))
        registry.register(_make_tool("mode_only", modes=("reactive",)))

        schemas = registry.get_schemas_by_mode("reactive")
        assert len(schemas) == 2

        schemas_maintenance = registry.get_schemas_by_mode("maintenance")
        assert len(schemas_maintenance) == 1
        assert schemas_maintenance[0]["function"]["name"] == "all_mode"

    def test_len(self):
        registry = CapabilityRegistry()
        assert len(registry) == 0
        registry.register(_make_tool("a"))
        assert len(registry) == 1

    def test_dunder_contains(self):
        registry = CapabilityRegistry()
        assert "a" not in registry
        registry.register(_make_tool("a"))
        assert "a" in registry

    def test_repr(self):
        registry = CapabilityRegistry()
        registry.register(_make_tool("a"))
        r = repr(registry)
        assert "1 tools" in r

    def test_description_truncation(self):
        """超过 512 字符的 description 被截断。"""
        registry = CapabilityRegistry()
        long_desc = "x" * 1000
        tool = ToolDef(
            name="long_desc_tool",
            description=long_desc,
            input_schema={"type": "object", "properties": {}},
            handler=lambda args, ctx: "ok",
        )
        registry.register(tool)

        schemas = registry.get_openai_schemas()
        desc = schemas[0]["function"]["description"]
        assert len(desc) <= 515  # 512 + "..."
        assert desc.endswith("...")
