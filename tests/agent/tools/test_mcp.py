"""Tests for MCP adapter — schema conversion and tool name generation."""

from __future__ import annotations

import pytest

from cogito.infrastructure.mcp.adapter import mcp_tool_name, convert_mcp_schema


class TestMCPToolName:
    def test_basic_naming(self) -> None:
        name = mcp_tool_name("filesystem", "read_file")
        assert name == "mcp_filesystem_read_file"

    def test_special_chars(self) -> None:
        name = mcp_tool_name("my-server!", "hello-world")
        assert "mcp_" in name
        assert " " not in name
        assert name.islower()

    def test_length_limit(self) -> None:
        long_name = mcp_tool_name("a" * 20, "b" * 20)
        assert len(long_name) <= 64


class TestConvertMCPschema:
    def test_enforces_object_root(self) -> None:
        schema = {"type": "string"}
        result = convert_mcp_schema(schema)
        assert result["type"] == "object"
        assert "input" in result["properties"]

    def test_preserves_object_schema(self) -> None:
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        result = convert_mcp_schema(schema)
        assert result["type"] == "object"
        assert "path" in result["properties"]

    def test_adds_additional_properties(self) -> None:
        schema = {"type": "object", "properties": {}}
        result = convert_mcp_schema(schema)
        assert result.get("additionalProperties") is False

    def test_removes_remote_ref(self) -> None:
        schema = {"$ref": "https://example.com/schema.json", "type": "object", "properties": {}}
        result = convert_mcp_schema(schema)
        assert "$ref" not in result
