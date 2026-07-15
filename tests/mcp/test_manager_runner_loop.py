"""MCP lifecycle must stay on the manager's persistent runner loop."""
from __future__ import annotations

import asyncio

from cogito.capability.mcp import MCPServerConfig
from cogito.capability.mcp.client import MCPCallResult
from cogito.capability.mcp.manager import MCPServerManager
from cogito.capability.registry import CapabilityRegistry


def test_start_call_health_and_stop_share_one_runner_loop(monkeypatch):
    loop_ids: list[int] = []
    sampling_scopes: list[str] = []

    class FakeClient:
        def __init__(self, name, config, **kwargs):
            self.name = name
            self.connected = False

        async def start(self):
            loop_ids.append(id(asyncio.get_running_loop()))
            self.connected = True

        async def list_tools(self):
            loop_ids.append(id(asyncio.get_running_loop()))
            return [{"name": "items", "description": "", "input_schema": {}}]

        async def call_tool_structured(
            self,
            tool_name,
            arguments,
            max_output_bytes=1048576,
            sampling_scope="",
        ):
            loop_ids.append(id(asyncio.get_running_loop()))
            sampling_scopes.append(sampling_scope)
            return MCPCallResult(
                server_name=self.name,
                tool_name=tool_name,
                structured_content={"items": []},
                text_content="",
                is_error=False,
            )

        async def health(self):
            loop_ids.append(id(asyncio.get_running_loop()))
            return True

        async def stop(self):
            loop_ids.append(id(asyncio.get_running_loop()))
            self.connected = False

    monkeypatch.setattr("cogito.capability.mcp.manager.MCPClient", FakeClient)
    manager = MCPServerManager(CapabilityRegistry())

    asyncio.run(manager.start_server(MCPServerConfig(name="fake")))
    result = manager.call_tool_structured_sync(
        "fake", "items", {}, sampling_scope="attempt-1",
    )
    assert result.is_error is False
    assert asyncio.run(manager.health_check_all()) == {"fake": True}
    asyncio.run(manager.stop_all())

    assert len(loop_ids) == 5
    assert len(set(loop_ids)) == 1
    assert sampling_scopes == ["attempt-1"]
