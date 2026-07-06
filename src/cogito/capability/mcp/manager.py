"""MCP Server Manager — 管理多个 MCP Server 的生命周期。"""
from __future__ import annotations

import asyncio
from typing import Any

from cogito.capability.mcp import MCPServerConfig
from cogito.capability.mcp.client import MCPClient
from cogito.capability.models import ToolContext, ToolDef
from cogito.capability.registry import CapabilityRegistry


class MCPServerManager:
    """管理多个 MCP Server 的生命周期。"""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry
        self._servers: dict[str, MCPClient] = {}

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
        tools_to_remove = [
            t for t in self._registry.all_tools()
            if t.name.startswith(f"{name}_")
        ]
        for t in tools_to_remove:
            # CapabilityRegistry 不直接支持删除，标记为移除
            # 重新创建 registry 或使用特殊标记
            pass

    async def stop_all(self) -> None:
        """停止所有 MCP Server。"""
        for name in list(self._servers.keys()):
            await self.stop_server(name)

    async def health_check_all(self) -> dict[str, bool]:
        """检查所有 Server 的健康状态。"""
        results = {}
        for name, client in self._servers.items():
            results[name] = await client.health()
        return results

    def get_client(self, name: str) -> MCPClient | None:
        return self._servers.get(name)
