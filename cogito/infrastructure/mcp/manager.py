# cogito/infrastructure/mcp/manager.py
#
# MCPClientManager — manages multiple MCP server connections.

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable, Sequence

from cogito.agent.ports.mcp.config import MCPCapability, MCPServerConfig, MCPTransportType
from cogito.agent.ports.tools.registry import ToolHandler, ToolProvider
from cogito.infrastructure.mcp.adapter import MCPToolHandler
from cogito.infrastructure.mcp.client import MCPClient
from cogito.infrastructure.mcp.resource_adapter import MCPPromptHandler, MCPResourceHandler
from cogito.infrastructure.mcp.transport import (
    SSEMCPTransport,
    StdioMCPTransport,
    StreamableHTTPTransport,
)
from cogito.infrastructure.sandbox.network_policy import DefaultNetworkPolicy

logger = logging.getLogger(__name__)


class ServerState(StrEnum):
    DISABLED = "disabled"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    BLOCKED = "blocked"


class MCPClientManager:
    """Manages MCP server connections, tool discovery, and lifecycle.

    Design rules (tool-system-spec §20):
      - Atomic tool replacement per provider.
      - Reconnection with exponential backoff.
      - Tool list change detection with debounced refresh.
      - Concurrent connection limiting.
    """

    def __init__(
        self,
        *,
        max_concurrent_connects: int = 4,
        refresh_debounce_ms: int = 300,
        on_tools_changed: Callable[[str, list[ToolHandler]], None] | None = None,
        network_policy: DefaultNetworkPolicy | None = None,
    ) -> None:
        self._max_concurrent = max_concurrent_connects
        self._refresh_debounce = refresh_debounce_ms / 1000.0
        self._semaphore = asyncio.Semaphore(max_concurrent_connects)
        self._on_tools_changed = on_tools_changed
        self._network_policy = network_policy

        # Server state
        self._servers: dict[str, _ServerEntry] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        self._refresh_tasks: dict[str, asyncio.Task] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    @property
    def name(self) -> str:
        return "mcp"

    async def load(self) -> list[ToolHandler]:
        """Load all configured MCP servers and return their tool handlers."""
        all_handlers: list[ToolHandler] = []

        async def connect_server(config: MCPServerConfig) -> list[ToolHandler]:
            async with self._semaphore:
                return await self._connect_server(config)

        tasks = [
            connect_server(config)
            for config in self._configs.values()
            if config.enabled
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error("MCP server connection failed: %s", result)
            elif isinstance(result, list):
                all_handlers.extend(result)

        return all_handlers

    async def close(self) -> None:
        """Close all MCP server connections."""
        for name, entry in self._servers.items():
            if entry.state in (ServerState.CONNECTED, ServerState.RECONNECTING, ServerState.DEGRADED):
                entry.state = ServerState.STOPPING
                # Cancel reconnect task if running
                if entry.reconnect_task is not None and not entry.reconnect_task.done():
                    entry.reconnect_task.cancel()
                try:
                    if entry.client:
                        await entry.client.close()
                except Exception as exc:
                    logger.warning("Error closing MCP %s: %s", name, exc)
                entry.state = ServerState.DISCONNECTED

    async def add_server(self, config: MCPServerConfig) -> None:
        """Add a new MCP server configuration."""
        self._configs[config.name] = config

    async def remove_server(self, name: str) -> None:
        """Remove and disconnect an MCP server."""
        self._configs.pop(name, None)
        entry = self._servers.pop(name, None)
        if entry is not None and entry.state is ServerState.CONNECTED:
            if entry.reconnect_task and not entry.reconnect_task.done():
                entry.reconnect_task.cancel()
            if entry.client:
                await entry.client.close()

    async def reconnect_server(self, name: str) -> bool:
        """Force reconnection of a specific server.

        Returns True if reconnection succeeded.
        """
        config = self._configs.get(name)
        if config is None:
            return False
        try:
            async with self._semaphore:
                handlers = await self._connect_server(config, allow_reconnect=True)
            if handlers and self._on_tools_changed:
                self._on_tools_changed(name, handlers)
            return True
        except Exception as exc:
            logger.error("MCP reconnect failed: %s: %s", name, exc)
            return False

    async def refresh_server(self, name: str) -> bool:
        """Refresh tools for a specific server without reconnecting.

        Returns True if the server is connected and tools were refreshed.
        """
        entry = self._servers.get(name)
        if entry is None or entry.client is None or entry.state is not ServerState.CONNECTED:
            return False
        await self.notify_tools_changed(name)
        return True

    async def get_server_status(self, name: str) -> str:
        entry = self._servers.get(name)
        return entry.state.value if entry else "unconfigured"

    async def list_servers(self) -> list[MCPServerConfig]:
        return list(self._configs.values())

    async def notify_tools_changed(self, server_name: str) -> None:
        """Handle tools/list_changed notification with debounced refresh."""
        if server_name not in self._refresh_locks:
            self._refresh_locks[server_name] = asyncio.Lock()

        async with self._refresh_locks[server_name]:
            if server_name in self._refresh_tasks:
                self._refresh_tasks[server_name].cancel()

            async def debounced_refresh():
                await asyncio.sleep(self._refresh_debounce)
                entry = self._servers.get(server_name)
                if entry is None or entry.client is None:
                    return
                try:
                    tools_data = await entry.client.list_tools()
                    config = self._configs.get(server_name)
                    if config is None:
                        return
                    filtered = self._filter_tools(tools_data, config)
                    handlers = []
                    for td in filtered:
                        handlers.append(MCPToolHandler(
                            server_name=server_name,
                            server_version=entry.client.server_version,
                            mcp_tool=td,
                            client=entry.client,
                            tool_timeout=config.tool_timeout_seconds,
                        ))
                    if self._on_tools_changed:
                        self._on_tools_changed(server_name, handlers)
                    logger.info("MCP tools refreshed: %s (%d tools)", server_name, len(handlers))
                except Exception as exc:
                    logger.error("MCP refresh failed: %s: %s", server_name, exc)

            self._refresh_tasks[server_name] = asyncio.create_task(debounced_refresh())

    # ── Internal ──────────────────────────────────────────────────────

    async def _connect_server(
        self,
        config: MCPServerConfig,
        allow_reconnect: bool = False,
    ) -> list[ToolHandler]:
        """Connect to one MCP server and discover its tools.

        When *allow_reconnect* is True, the connection is attempted even
        if a previous attempt left the server in BLOCKED state.
        """
        entry = self._servers.get(config.name)
        if entry is not None and entry.state is ServerState.CONNECTED:
            return []
        if entry is not None and entry.state is ServerState.BLOCKED and not allow_reconnect:
            return []

        try:
            entry = _ServerEntry(state=ServerState.CONNECTING)
            self._servers[config.name] = entry

            # Security: validate server config
            warnings = self._validate_server_config(config)
            for w in warnings:
                logger.warning("MCP security warning: %s", w)

            transport = self._create_transport(config)
            client = MCPClient(transport)
            await client.initialize()

            tools_data = await client.list_tools()
            entry.client = client
            entry.state = ServerState.CONNECTED
            entry.last_connected = datetime.now(timezone.utc)
            entry.retry_count = 0

            # Apply include/exclude filters
            filtered_tools = self._filter_tools(tools_data, config)

            handlers = []
            for tool_data in filtered_tools:
                handler = MCPToolHandler(
                    server_name=config.name,
                    server_version=client.server_version,
                    mcp_tool=tool_data,
                    client=client,
                    tool_timeout=config.tool_timeout_seconds,
                )
                handlers.append(handler)

            # Discover resources if enabled
            if MCPCapability.RESOURCES in config.enabled_features and client.has_resources:
                try:
                    resources = await client.list_resources()
                    for resource in resources:
                        handlers.append(MCPResourceHandler(
                            server_name=config.name,
                            server_version=client.server_version,
                            resource=resource,
                            client=client,
                            tool_timeout=config.tool_timeout_seconds,
                        ))
                except Exception as exc:
                    logger.warning("MCP resource discovery failed: %s: %s", config.name, exc)

            # Discover prompts if enabled
            if MCPCapability.PROMPTS in config.enabled_features and client.has_prompts:
                try:
                    prompts = await client.list_prompts()
                    for prompt in prompts:
                        handlers.append(MCPPromptHandler(
                            server_name=config.name,
                            server_version=client.server_version,
                            prompt=prompt,
                            client=client,
                            tool_timeout=config.tool_timeout_seconds,
                        ))
                except Exception as exc:
                    logger.warning("MCP prompt discovery failed: %s: %s", config.name, exc)

            logger.info(
                "MCP connected: %s (%d handlers, %d tools)",
                config.name, len(handlers), len(filtered_tools),
            )
            return handlers

        except Exception as exc:
            logger.error("MCP connection failed: %s: %s", config.name, exc)
            if config.name in self._servers:
                entry = self._servers[config.name]
                entry.state = ServerState.BLOCKED
                entry.last_error = str(exc)[:200]
                entry.retry_count += 1

                # Auto-reconnect with exponential backoff (if configured)
                if config.max_reconnect_attempts > 0 and entry.retry_count <= config.max_reconnect_attempts:
                    self._start_reconnect(config)

            return []

    def _start_reconnect(self, config: MCPServerConfig) -> None:
        """Start an async reconnection task with exponential backoff."""
        entry = self._servers.get(config.name)
        if entry is None:
            return

        # Calculate delay: 2^retry seconds, capped at 60s
        delay = min(2 ** entry.retry_count, 60.0)
        logger.info("MCP reconnect scheduled for %s in %.0fs (attempt %d/%d)",
                    config.name, delay, entry.retry_count, config.max_reconnect_attempts)

        async def _reconnect_loop():
            await asyncio.sleep(delay)
            if entry.state is ServerState.STOPPING:
                return
            entry.state = ServerState.RECONNECTING
            try:
                async with self._semaphore:
                    handlers = await self._connect_server(config, allow_reconnect=True)
                if handlers and self._on_tools_changed:
                    self._on_tools_changed(config.name, handlers)
            except Exception as exc:
                logger.warning("MCP reconnection attempt failed: %s: %s", config.name, exc)

        entry.reconnect_task = asyncio.create_task(_reconnect_loop())

    def _validate_server_config(self, config: MCPServerConfig) -> list[str]:
        """Validate an MCP server config for security concerns.

        Checks for shell interpreters with network egress patterns
        (hermes-agent pattern).

        Returns a list of warning messages (empty = no issues).
        """
        warnings: list[str] = []
        if config.transport is MCPTransportType.STDIO and config.command:
            # Check for shell interpreter + egress patterns
            import os
            import shlex

            _SHELL_INTERPRETERS = frozenset({
                "bash", "sh", "zsh", "dash", "fish",
                "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
            })
            _EGRESS_RE = re.compile(
                r"(?<![\w.-])(?:curl|wget|nc|ncat|socat)(?![\w.-])"
                r"|/dev/tcp/"
                r"|\bInvoke-WebRequest\b"
                r"|\bInvoke-RestMethod\b",
                re.IGNORECASE,
            )

            basename = os.path.basename(config.command).lower()
            if basename in _SHELL_INTERPRETERS:
                args_str = " ".join(config.args) if config.args else ""
                if _EGRESS_RE.search(args_str):
                    warnings.append(
                        f"MCP server '{config.name}' uses shell '{config.command}' "
                        f"with network egress in args"
                    )
        return warnings

    def _create_transport(
        self,
        config: MCPServerConfig,
    ) -> StdioMCPTransport | SSEMCPTransport | StreamableHTTPTransport:
        """Create a transport for the given config."""
        if config.transport is MCPTransportType.STDIO:
            return StdioMCPTransport(
                command=config.command or "",
                args=config.args,
                cwd=config.cwd,
                env=dict(config.env) if config.env else None,
                connect_timeout=config.connect_timeout_seconds,
            )
        elif config.transport is MCPTransportType.SSE:
            if not config.url:
                raise ValueError("SSE transport requires a URL")
            return SSEMCPTransport(
                url=config.url,
                network_policy=self._network_policy,
                connect_timeout=config.connect_timeout_seconds,
            )
        elif config.transport is MCPTransportType.STREAMABLE_HTTP:
            if not config.url:
                raise ValueError("Streamable HTTP requires a URL")
            return StreamableHTTPTransport(
                url=config.url,
                network_policy=self._network_policy,
                connect_timeout=config.connect_timeout_seconds,
            )
        raise ValueError(f"Unsupported transport: {config.transport}")

    @staticmethod
    def _filter_tools(tools: list[dict], config: MCPServerConfig) -> list[dict]:
        """Apply include/exclude filters to tool list."""
        result = []
        for tool in tools:
            name = tool.get("name", "")
            if config.include_tools != frozenset({"*"}):
                if name not in config.include_tools:
                    continue
            if name in config.exclude_tools:
                continue
            result.append(tool)
        return result


class _ServerEntry:
    """Internal per-server state."""
    def __init__(self, state: ServerState = ServerState.DISCONNECTED) -> None:
        self.state = state
        self.client: MCPClient | None = None
        self.last_error: str | None = None
        self.last_connected: datetime | None = None
        self.retry_count: int = 0
        self.reconnect_task: asyncio.Task | None = None
