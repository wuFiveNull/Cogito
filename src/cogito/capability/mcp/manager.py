"""MCP Server Manager —— 管理多个 MCP Server 的生命周期。"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from cogito.capability.mcp import MCPServerConfig
from cogito.capability.mcp.client import MCPCallResult, MCPClient
from cogito.capability.models import ToolContext, ToolDef
from cogito.capability.registry import CapabilityRegistry


class _MCPRunner:
    """把 MCP 调用调度到持久化后台 loop 运行。

    解决 stdio_client 的 anyio task_group 与外部 event loop（例如 pytest-asyncio
    或 TaskWorker 主 loop）不兼容的问题。所有 MCP IO 在单个持久 loop 串行。
    """

    _instance: _MCPRunner | None = None
    _lock = threading.Lock()

    def __new__(cls) -> _MCPRunner:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._loop = None
                inst._thread = None
                inst._ready = threading.Event()
                cls._instance = inst
            return cls._instance

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._entry, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    def _entry(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def run(self, coro: Any, timeout: float = 30) -> Any:
        if self._loop is None:
            raise RuntimeError("MCP runner not ready")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)


class MCPServerManager:
    """管理多个 MCP Server 的生命周期。"""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry
        self._servers: dict[str, MCPClient] = {}
        self._runner = _MCPRunner()
        self._runner.start()

    async def start_server(self, config: MCPServerConfig) -> None:
        """启动一个 MCP Server 并注册其工具。"""
        client = MCPClient(config.name, config)
        try:
            await client.start()
        except Exception as e:
            raise RuntimeError(
                f"Failed to start MCP server '{config.name}': {e}"
            ) from e

        # 获取工具列表并注册
        tools = await client.list_tools()
        for tool_info in tools:
            tool_name = f"{config.name}_{tool_info['name']}"

            # 创建适配器 handler
            async def make_handler(name=tool_info["name"], client=client):
                async def handler(args: dict, ctx: ToolContext) -> str:
                    return await client.call_tool(name, args)
                return handler

            tool_def = ToolDef(
                name=tool_name,
                description=tool_info.get("description", ""),
                input_schema=tool_info.get("input_schema", {"type": "object", "properties": {}}),
                toolset=(config.toolset,),
                handler=await make_handler(),
                risk_level="medium",
            )
            self._registry.register(tool_def)

        self._servers[config.name] = client

    async def stop_server(self, name: str) -> None:
        """停止一个 MCP Server 并注销其工具。"""
        client = self._servers.pop(name, None)
        if client:
            await client.stop()

        # 注销所有以 server_name 前缀的工具
        self._registry.unregister_by_prefix(f"{name}_")

    async def stop_all(self) -> None:
        """停止所有 MCP Server（在 runner loop 里串行安全关闭）。"""
        for name in list(self._servers.keys()):
            client = self._servers.get(name)
            if client is not None:
                # 串行 close，避免并发 __aexit__ 跨 loop 造成 race
                try:
                    self._runner.run(client.stop(), timeout=10)
                except Exception:
                    pass
                self._registry.unregister_by_prefix(f"{name}_")
        self._servers.clear()

    async def health_check_all(self) -> dict[str, bool]:
        """检查所有 Server 的健康状态。"""
        results = {}
        for name, client in self._servers.items():
            results[name] = await client.health()
        return results

    def get_client(self, name: str) -> MCPClient | None:
        return self._servers.get(name)

    # ── 同步封装：供 Connector Handler 在同步 TaskHandler 内调用 ──

    def call_tool_structured_sync(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        max_output_bytes: int = 1048576,
        timeout: float = 30,
    ) -> MCPCallResult:
        """同步地在 runner loop 内调用 MCP Tool（用于同步 TaskHandler）。

        解决 MCP 的 anyio task_group 与外部 loop 冲突问题。
        """
        client = self._servers.get(server_name)
        if client is None:
            return MCPCallResult(
                server_name=server_name,
                tool_name=tool_name,
                structured_content=None,
                text_content="",
                is_error=True,
            )
        return self._runner.run(
            client.call_tool_structured(tool_name, arguments,
                                        max_output_bytes=max_output_bytes),
            timeout=timeout,
        )
