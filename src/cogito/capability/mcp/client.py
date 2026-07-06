"""MCP Client — 连接 MCP Server 并调用工具。

使用官方 mcp SDK。
"""

from __future__ import annotations

import asyncio
from typing import Any

from cogito.capability.mcp import MCPServerConfig


class MCPClient:
    """MCP Server 连接客户端。

    支持 stdio 和 SSE（streamable HTTP）传输。
    """

    def __init__(self, server_name: str, config: MCPServerConfig) -> None:
        self._server_name = server_name
        self._config = config
        self._session = None
        self._process = None
        self._tools: list[dict[str, Any]] = []
        self._connected = False

    async def start(self) -> None:
        """启动并初始化 MCP 连接。"""
        if self._connected:
            return

        if self._config.transport == "stdio":
            await self._start_stdio()
        elif self._config.transport == "sse":
            await self._start_sse()
        else:
            raise ValueError(f"Unknown transport: {self._config.transport}")

    async def _start_stdio(self) -> None:
        """通过 stdio 启动 MCP Server。"""
        import mcp.types as types
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self._config.command,
            args=list(self._config.args),
        )

        stdio_transport = await stdio_client(params)
        read, write = stdio_transport

        self._session = await ClientSession(read, write, types.ClientCapabilities())
        await self._session.initialize()
        self._connected = True

    async def _start_sse(self) -> None:
        """通过 SSE 连接 MCP Server。"""
        import mcp.types as types
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        sse_transport = await sse_client(url=self._config.url)
        read, write = sse_transport

        self._session = await ClientSession(read, write, types.ClientCapabilities())
        await self._session.initialize()
        self._connected = True

    async def list_tools(self) -> list[dict[str, Any]]:
        """获取 Server 的工具列表。"""
        if not self._session:
            return []

        result = await self._session.list_tools()
        tools = []
        for tool in result.tools:
            tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            })
        self._tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用 MCP 工具并返回结果文本。"""
        if not self._session:
            raise RuntimeError("MCP client not connected")

        result = await self._session.call_tool(name, arguments)

        # 拼接结果内容
        parts = []
        for content in result.content:
            if hasattr(content, "text"):
                parts.append(content.text)
            else:
                parts.append(str(content))

        return "\n".join(parts)

    async def health(self) -> bool:
        """检查连接是否健康。"""
        if not self._session or not self._connected:
            return False
        try:
            from mcp import PingRequest
            await self._session.send_request(PingRequest())
            return True
        except Exception:
            return False

    async def stop(self) -> None:
        """关闭连接。"""
        self._connected = False
        if self._session:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None

    @property
    def connected(self) -> bool:
        return self._connected
