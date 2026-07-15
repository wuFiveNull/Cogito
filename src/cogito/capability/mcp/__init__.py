"""MCP 配置模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MCPServerConfig:
    """单个 MCP Server 的配置。"""

    name: str = ""
    transport: str = "stdio"  # stdio | sse | streamable_http
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    enabled: bool = True
    toolset: str = "mcp"
    cwd: str = ""
    include_tools: list[str] = field(default_factory=list)
    exclude_tools: list[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    max_output_chars: int = 50_000
    allow_resources: bool = False
    allow_prompts: bool = False
    allow_roots: bool = False
    allow_sampling: bool = False
    # Direct programmatic clients retain the historical trusted-host default.
    isolation: str = "disabled"
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    oauth_enabled: bool = False
    oauth_token_file: str = ""
    oauth_redirect_uri: str = "http://127.0.0.1:33418/callback"
    oauth_scope: str = ""
    secret_root: str = ""
    tool_policy: dict[str, dict[str, object]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
