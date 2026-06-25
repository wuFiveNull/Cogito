# cogito/infrastructure/mcp/resource_adapter.py
#
# MCP Resource and Prompt wrappers.
#
# Wraps MCP resources and prompts as ToolHandler instances so they
# appear alongside regular tools in the registry.

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

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


# ── Resource wrapper ───────────────────────────────────────────────────

class MCPResourceHandler:
    """Wraps an MCP resource as a ToolHandler for the registry.

    Each resource becomes a tool named ``read_mcp_{server}_{resource}``
    that calls ``resources/read`` on the MCP server.
    """

    def __init__(
        self,
        *,
        server_name: str,
        server_version: str,
        resource: dict[str, Any],
        client: object,
        tool_timeout: float = 120.0,
    ) -> None:
        res_name = resource.get("name", "unknown")
        res_uri = resource.get("uri", "")
        sanitized_name = self._sanitize_name(res_name) or "unnamed"

        self._definition = ToolDefinition(
            name=f"read_mcp_{server_name}_{sanitized_name}",
            description=resource.get("description", f"Read MCP resource: {res_uri}")[:2_000],
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.LOW,
            timeout_seconds=tool_timeout,
            idempotent=True,
            parallel_safe=True,
            kind=ToolKind.READ,
            risk=ToolRisk.EXTERNAL_READ,
            source=ToolSource(
                type=ToolSourceType.MCP,
                provider="mcp",
                version=server_version,
                server_name=server_name,
            ),
            tags=frozenset({"mcp", "resource"}),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_TOOL,
            limits=ToolLimits(timeout_seconds=tool_timeout, max_result_chars=50_000),
        )
        self._client = client
        self._res_uri = res_uri

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        try:
            result = await self._client.read_resource(self._res_uri)
            content = result.get("contents", [])
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if "text" in block:
                        texts.append(block["text"])
                    elif "blob" in block:
                        texts.append(f"[Binary resource: {len(block['blob'])} bytes]")
            output = "\n".join(texts)
            return {"content": output}
        except Exception as exc:
            return {"error": {"code": "MCP_RESOURCE_ERROR", "message": str(exc)[:500]}}

    @staticmethod
    def _sanitize_name(name: str) -> str:
        import re
        s = re.sub(r"[^a-zA-Z0-9]", "_", name)
        s = re.sub(r"_+", "_", s)
        return s.strip("_").lower()[:40]


# ── Prompt wrapper ────────────────────────────────────────────────────

class MCPPromptHandler:
    """Wraps an MCP prompt as a ToolHandler for the registry.

    Each prompt becomes a tool named ``get_mcp_{server}_{prompt}``
    that calls ``prompts/get`` on the MCP server.
    """

    def __init__(
        self,
        *,
        server_name: str,
        server_version: str,
        prompt: dict[str, Any],
        client: object,
        tool_timeout: float = 120.0,
    ) -> None:
        prompt_name = prompt.get("name", "unknown")
        sanitized_name = self._sanitize_name(prompt_name) or "unnamed"

        # Build schema from prompt's argument definitions
        prompt_args = prompt.get("arguments", [])
        properties = {}
        required = []
        for arg in prompt_args:
            arg_name = arg.get("name", "")
            if arg_name:
                properties[arg_name] = {
                    "type": "string",
                    "description": arg.get("description", ""),
                }
                if arg.get("required", False):
                    required.append(arg_name)

        self._definition = ToolDefinition(
            name=f"get_mcp_{server_name}_{sanitized_name}",
            description=prompt.get("description", f"Get MCP prompt: {prompt_name}")[:2_000],
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.NONE,
            risk_level=ToolRiskLevel.LOW,
            timeout_seconds=tool_timeout,
            idempotent=True,
            parallel_safe=True,
            kind=ToolKind.READ,
            risk=ToolRisk.EXTERNAL_READ,
            source=ToolSource(
                type=ToolSourceType.MCP,
                provider="mcp",
                version=server_version,
                server_name=server_name,
            ),
            tags=frozenset({"mcp", "prompt"}),
            concurrency_mode=ToolConcurrencyMode.SERIAL_PER_TOOL,
            limits=ToolLimits(timeout_seconds=tool_timeout, max_result_chars=50_000),
        )
        self._client = client
        self._prompt_name = prompt_name

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, *, arguments: Mapping[str, object], context: Mapping[str, object]) -> dict:
        try:
            result = await self._client.get_prompt(self._prompt_name, dict(arguments))
            messages = result.get("messages", [])
            texts = []
            for msg in messages:
                if isinstance(msg, dict):
                    content = msg.get("content", {})
                    if isinstance(content, dict) and content.get("type") == "text":
                        texts.append(content.get("text", ""))
            output = "\n".join(texts)
            return {"content": output}
        except Exception as exc:
            return {"error": {"code": "MCP_PROMPT_ERROR", "message": str(exc)[:500]}}

    @staticmethod
    def _sanitize_name(name: str) -> str:
        import re
        s = re.sub(r"[^a-zA-Z0-9]", "_", name)
        s = re.sub(r"_+", "_", s)
        return s.strip("_").lower()[:40]
