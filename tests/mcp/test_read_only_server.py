from __future__ import annotations

import sqlite3
import json
import os
import sys
from pathlib import Path

import pytest

from cogito.config import Config
from cogito.service import read_only_mcp_server
from cogito.domain.task import Task
from cogito.service.agent_tool_commands import AgentToolCommandService
from cogito.service.api.query_service import QueryService
from cogito.store.task_repo import TaskRepository


@pytest.mark.asyncio
async def test_read_only_mcp_exposes_only_explicit_query_allowlist(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(read_only_mcp_server, "get_connection", lambda _path: conn)

    server, returned_conn = read_only_mcp_server.build_read_only_mcp_server(Config())
    tools = await server.list_tools()
    names = {tool.name for tool in tools}

    assert returned_conn is conn
    assert names == {
        "get_system_info",
        "list_capabilities",
        "get_capability",
        "list_tasks",
        "get_task_status",
        "list_schedules",
        "search_memory",
        "search_knowledge",
        "list_skills",
        "get_skill",
    }
    assert not any(
        marker in name
        for name in names
        for marker in ("create", "update", "delete", "approve", "send", "command")
    )


@pytest.mark.asyncio
async def test_read_only_mcp_stdio_protocol_round_trip(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    workspace = tmp_path / "workspace"
    config_path = tmp_path / "cogito.toml"
    config_path.write_text(
        f'workspace_path = "{workspace.as_posix()}"\n'
        '[storage]\n'
        'db_path = "cogito.db"\n'
        '[capability.read_only_mcp]\n'
        'principal_id = "owner"\n'
        'page_size = 10\n',
        encoding="utf-8",
    )
    from cogito.store.connection import get_connection
    from cogito.store.migration import migrate

    workspace.mkdir()
    db = get_connection(workspace / "cogito.db")
    migrate(db)
    db.close()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(Path(__file__).parents[2] / "src"), env.get("PYTHONPATH", "")])
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cogito", "mcp-serve", "--config", str(config_path)],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            result = await session.call_tool("get_system_info", {})

    assert "get_system_info" in names
    assert not result.isError


def test_read_only_mcp_redacts_nested_secrets():
    value = read_only_mcp_server._redact(
        {"token": "plain", "nested": [{"password": "plain", "safe": "ok"}]}
    )

    assert value == {
        "token": "<redacted>",
        "nested": [{"password": "<redacted>", "safe": "ok"}],
    }


def test_query_facade_filters_tasks_and_schedules_by_fixed_principal(in_memory_db):
    TaskRepository(in_memory_db).insert(
        Task(
            task_type="agent.delegate",
            payload_ref=json.dumps({"principal_id": "owner", "prompt": "private"}),
            origin="agent_tool",
        )
    )
    TaskRepository(in_memory_db).insert(
        Task(
            task_type="agent.delegate",
            payload_ref=json.dumps({"principal_id": "other", "prompt": "secret"}),
            origin="agent_tool",
        )
    )
    commands = AgentToolCommandService(in_memory_db)
    commands.create_schedule(
        {
            "prompt": "owner prompt", "expression": "5m", "timezone": "UTC",
            "schedule_type": "interval", "session_id": "session",
        },
        actor="owner",
        tool_call_id="owner-schedule",
    )
    commands.create_schedule(
        {
            "prompt": "other prompt", "expression": "5m", "timezone": "UTC",
            "schedule_type": "interval", "session_id": "session",
        },
        actor="other",
        tool_call_id="other-schedule",
    )

    query = QueryService(in_memory_db, Config())
    tasks = query.list_tasks_for_principal("owner", limit=50, offset=0)
    schedules = query.list_schedules_for_principal("owner", limit=50, offset=0)

    assert tasks["total"] == 1
    assert "payload_ref" not in tasks["items"][0]
    assert schedules["total"] == 1
    assert "task_payload" not in schedules["items"][0]
