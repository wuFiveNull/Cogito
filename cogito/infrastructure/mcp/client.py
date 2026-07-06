# cogito/infrastructure/mcp/client.py
#
# MCPClient — implements the MCP protocol for tool discovery and execution.

from __future__ import annotations

import json
import logging
from typing import Any

from cogito.infrastructure.mcp.transport import StdioMCPTransport, MCPTransport, MCPTransportError

logger = logging.getLogger(__name__)


class MCPClient:
    """MCP protocol client wrapping a transport.

    Handles:
      - initialize handshake
      - tools/list, tools/call
      - resources/list, resources/read
      - prompts/list, prompts/get
      - notifications (tools/list_changed)
    """

    def __init__(self, transport: MCPTransport) -> None:
        self._transport = transport
        self._server_name = "unknown"
        self._server_version = "0.0.0"
        self._capabilities: dict[str, Any] = {}

    async def initialize(self) -> dict[str, Any]:
        """Perform MCP initialize handshake."""
        await self._transport.connect()
        result = await self._transport.send_request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "clientInfo": {"name": "cogito-agent", "version": "0.1.0"},
        })
        self._server_name = result.get("serverInfo", {}).get("name", "unknown")
        self._server_version = result.get("serverInfo", {}).get("version", "0.0.0")
        self._capabilities = result.get("capabilities", {})
        await self._transport.send_request("notifications/initialized")
        logger.info("MCP initialized: %s v%s", self._server_name, self._server_version)
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """Fetch the tool list from the MCP server."""
        result = await self._transport.send_request("tools/list")
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on the MCP server."""
        result = await self._transport.send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        return result

    async def list_resources(self) -> list[dict[str, Any]]:
        """Fetch the resource list from the MCP server."""
        try:
            result = await self._transport.send_request("resources/list")
            return result.get("resources", [])
        except MCPTransportError:
            return []

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read a specific resource from the MCP server."""
        result = await self._transport.send_request("resources/read", {
            "uri": uri,
        })
        return result

    async def list_prompts(self) -> list[dict[str, Any]]:
        """Fetch the prompt list from the MCP server."""
        try:
            result = await self._transport.send_request("prompts/list")
            return result.get("prompts", [])
        except MCPTransportError:
            return []

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Get a specific prompt from the MCP server."""
        result = await self._transport.send_request("prompts/get", {
            "name": name,
            "arguments": arguments or {},
        })
        return result

    @property
    def has_resources(self) -> bool:
        return "resources" in self._capabilities

    @property
    def has_prompts(self) -> bool:
        return "prompts" in self._capabilities

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def server_version(self) -> str:
        return self._server_version

    async def close(self) -> None:
        await self._transport.close()
