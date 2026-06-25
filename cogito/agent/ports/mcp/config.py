# cogito/agent/ports/mcp/config.py
#
# MCP server configuration model.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class MCPTransportType(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


class MCPCapability(StrEnum):
    TOOLS = "tools"
    RESOURCES = "resources"
    PROMPTS = "prompts"
    SAMPLING = "sampling"
    ROOTS = "roots"


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str
    transport: MCPTransportType
    enabled: bool = True
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None
    connect_timeout_seconds: float = 30.0
    tool_timeout_seconds: float = 120.0
    keepalive_seconds: float = 120.0
    max_reconnect_attempts: int = 5
    include_tools: frozenset[str] = frozenset({"*"})
    exclude_tools: frozenset[str] = frozenset()
    enabled_features: frozenset[MCPCapability] = frozenset({MCPCapability.TOOLS})
