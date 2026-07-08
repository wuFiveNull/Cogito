"""MCP Client — 连接 MCP Server 并调用工具。

使用官方 mcp SDK。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cogito.capability.mcp import MCPServerConfig

# ── 结构化结果 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MCPCallResult:
    """MCP Tool 调用的结构化结果。

    - structured_content 始终为 JSON 对象（dict），否则为 None
    - text_content 始终为字符串（展示用）
    - MCP 数据天然不可信，trust_label 固定为 external_untrusted
    """

    server_name: str
    tool_name: str
    structured_content: dict[str, Any] | list[Any] | None
    text_content: str
    is_error: bool
    raw_size_bytes: int = 0
    schema_hash: str = ""
    trust_label: str = "external_untrusted"


class MCPResultError(RuntimeError):
    """MCP 调用失败或输出超限。"""


class MCPClient:
    """MCP Server 连接客户端。

    支持 stdio 和 SSE（streamable HTTP）传输。
    """

    def __init__(self, server_name: str, config: MCPServerConfig) -> None:
        self._server_name = server_name
        self._config = config
        self._session = None
        self._stdio_ctx = None
        self._sse_ctx = None
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
        """通过 stdio 启动 MCP Server。

        兼容新版 mcp SDK：stdio_client 已经是 @asynccontextmanager，
        必须用 async with 获取 (read, write)，不能用旧式 await。
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self._config.command,
            args=list(self._config.args),
        )

        self._stdio_ctx = stdio_client(params)
        read, write = await self._stdio_ctx.__aenter__()

        # mcp >= 1.x：ClientSession(read, write, read_timeout_seconds?, ...)
        # 不再接受 ClientCapabilities 作为第三位参数
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        self._connected = True

    async def _start_sse(self) -> None:
        """通过 SSE 连接 MCP Server。"""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        self._sse_ctx = sse_client(url=self._config.url)
        read, write = await self._sse_ctx.__aenter__()

        self._session = ClientSession(read, write)
        await self._session.__aenter__()
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
        result = await self.call_tool_structured(name, arguments)
        return result.text_content

    async def call_tool_structured(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        max_output_bytes: int = 1048576,
    ) -> MCPCallResult:
        """调用 MCP 工具并返回结构化结果。

        - 解析 text content 为 JSON；解析失败仍返回原始文本（is_error=True）
        - 输出大小超限抛出 MCPResultError（不入库）
        - schema_hash 用 content 前 4KB 的 MD5（对 Connector 数据漂移审计有用）
        """
        if not self._session:
            raise RuntimeError("MCP client not connected")

        result = await self._session.call_tool(name, arguments)

        parts = []
        for content in result.content:
            if hasattr(content, "text"):
                parts.append(content.text)
            else:
                parts.append(str(content))

        text = "\n".join(parts)
        raw_bytes = len(text.encode("utf-8"))

        if raw_bytes > max_output_bytes:
            raise MCPResultError(
                f"tool output too large: {raw_bytes} bytes (limit {max_output_bytes})",
            )

        structured: dict[str, Any] | list[Any] | None = None
        parse_error = False
        try:
            structured = json.loads(text)
        except (ValueError, TypeError):
            parse_error = True

        schema_hash = hashlib.md5(text[:4096].encode("utf-8", errors="ignore")).hexdigest()

        return MCPCallResult(
            server_name=self._server_name,
            tool_name=name,
            structured_content=structured,
            text_content=text,
            is_error=result.isError or parse_error,
            raw_size_bytes=raw_bytes,
            schema_hash=schema_hash,
        )

    async def tool_info(self, name: str) -> dict[str, Any] | None:
        """获取单个 Tool 的 Schema 信息（用于审计）。"""
        for t in self._tools:
            if t["name"] == name:
                return t
        return None

    @property
    def tools_info(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def health(self) -> bool:
        """检查连接是否健康。"""
        if not self._session or not self._connected:
            return False
        try:
            await self._session.send_ping()
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
        if self._stdio_ctx is not None:
            try:
                await self._stdio_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._stdio_ctx = None
        if self._sse_ctx is not None:
            try:
                await self._sse_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._sse_ctx = None

    @property
    def connected(self) -> bool:
        return self._connected
