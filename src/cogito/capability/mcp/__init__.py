"""MCP 配置模型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MCPServerConfig:
    """单个 MCP Server 的配置。"""
    name: str = ""
    transport: str = "stdio"  # stdio | sse
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    enabled: bool = True
    toolset: str = "mcp"
