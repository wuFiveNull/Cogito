# cogito/agent/ports/mcp/manager.py
#
# MCP Manager Port — manages lifecycle of MCP server connections.

from __future__ import annotations

from typing import Protocol

from cogito.agent.ports.mcp.config import MCPServerConfig
from cogito.agent.ports.tools.registry import ToolProvider


class MCPManagerPort(ToolProvider, Protocol):
    """Manages MCP server connections and lifecycle."""

    async def add_server(self, config: MCPServerConfig) -> None:
        ...

    async def remove_server(self, name: str) -> None:
        ...

    async def get_server_status(self, name: str) -> str:
        ...

    async def list_servers(self) -> list[MCPServerConfig]:
        ...
