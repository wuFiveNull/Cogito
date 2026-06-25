# cogito/infrastructure/mcp/adapter.py
#
# MCPToolHandler — wraps an MCP tool as a ToolHandler for the registry.

from __future__ import annotations

import json
import re
import logging
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
from cogito.agent.ports.tools.registry import ToolHandler

logger = logging.getLogger(__name__)


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Generate a namespaced MCP tool name: mcp_{server}_{tool}."""
    def slugify(s: str) -> str:
        s = re.sub(r"[^a-zA-Z0-9]", "_", s)
        s = re.sub(r"_+", "_", s)
        s = s.strip("_").lower()
        return s[:40]
    return f"mcp_{slugify(server_name)}_{slugify(tool_name)}"


def convert_mcp_schema(mcp_schema: dict) -> dict:
    """Convert an MCP tool input schema to a safe JSON Schema subset."""
    schema = dict(mcp_schema)
    # Ensure root is object
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {"input": schema}, "required": ["input"]}
    # Remove remote $ref
    schema.pop("$ref", None)
    schema.pop("$defs", None)
    # Default additionalProperties
    schema.setdefault("additionalProperties", False)
    return schema


class MCPToolHandler:
    """Wraps an MCP-discovered tool as a ToolHandler."""

    def __init__(
        self,
        *,
        server_name: str,
        server_version: str,
        mcp_tool: dict,
        client: object,
        tool_timeout: float = 120.0,
    ) -> None:
        mcp_name = mcp_tool.get("name", "unknown")
        self._definition = ToolDefinition(
            name=mcp_tool_name(server_name, mcp_name),
            description=mcp_tool.get("description", "")[:2_000],
            input_schema=convert_mcp_schema(mcp_tool.get("inputSchema", {"type": "object", "properties": {}})),
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.LOW,
            timeout_seconds=tool_timeout,
            idempotent=False,
            parallel_safe=True,
            kind=ToolKind.READ,
            risk=ToolRisk.EXTERNAL_READ,
            source=ToolSource(type=ToolSourceType.MCP, provider="mcp", version=server_version, server_name=server_name),
            tags=frozenset({"mcp"}),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_TOOL,
            limits=ToolLimits(timeout_seconds=tool_timeout, max_result_chars=50_000),
        )
        self._client = client
        self._mcp_name = mcp_name

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        try:
            result = await self._client.call_tool(self._mcp_name, dict(arguments))
            content = result.get("content", [])
            is_error = result.get("isError", False)
            texts = []
            for block in content:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "resource":
                    texts.append(f"[Resource: {block.get('resource', {}).get('uri', 'unknown')}]")
            output = "\n".join(texts)
            return {"content": output, "isError": is_error}
        except Exception as exc:
            return {"error": {"code": "MCP_EXECUTION_ERROR", "message": str(exc)[:500]}}
