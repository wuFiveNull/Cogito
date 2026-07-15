"""MCP Connector 摄取管道测试（M2/M3/M4）。

为避免 MCP 子进程 stdout/stderr pipe 与 pytest-asyncio 主 loop 冲突，
MCP 调用通过 `asyncio.run` 在独立线程内执行，每个 test function 独占自己的
in-memory DB + fake MCP 子进程。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cogito.capability.mcp import MCPServerConfig
from cogito.capability.mcp.client import MCPCallResult, MCPClient
from cogito.domain.connector import Connector
from cogito.domain.mcp_connector import MCPConnectorConfig
from cogito.domain.task import Task, TaskStatus
from cogito.service.mcp_connector_handler import handle_mcp_connector_poll
from cogito.service.task_handlers import TaskHandlerContext
from cogito.store.connector_repo import ConnectorRepository
from cogito.store.mcp_connector_repo import MCPConnectorConfigRepository
from cogito.store.migration import migrate


class _FakeManager:
    """handler 使用的 mcp_manager 占位——把 MCP 调用路由到独立 loop 运行。"""

    def __init__(self, client: MCPClient) -> None:
        self._client = client
        # 使用独立线程跑 MCP 调用，避免 anyio task_group 冲突
        # 对 start() 调用后的 MCPClient，所有 call_tool 必须在同一 loop 跑

    def get_client(self, name: str) -> MCPClient | None:
        return self._client if name == self._client._server_name else None

    def call_tool_structured_sync(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        max_output_bytes: int = 1048576,
        timeout: float = 30,
    ) -> MCPCallResult:
        async def _call():
            return await self._client.call_tool_structured(
                tool_name,
                arguments,
                max_output_bytes=max_output_bytes,
            )

        # 使用 MCP 专用 persistent loop
        return _mcp_loop_runner.run(_call(), timeout=timeout)


class _MCPPersistentRunner:
    """持久化 MCP loop —— 整个测试 session 单例。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        import threading

        loop = asyncio.new_event_loop()
        self._loop = loop

        def _entry():
            asyncio.set_event_loop(loop)
            self._ready.set()
            loop.run_forever()

        t = threading.Thread(target=_entry, daemon=True)
        t.start()
        self._ready.wait(timeout=5)
        self._started = True

    def run(self, coro, timeout: float = 30):
        import asyncio

        if self._loop is None:
            raise RuntimeError("loop not started")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


import threading
import sys

_mcp_loop_runner = _MCPPersistentRunner()


@pytest.fixture(scope="session", autouse=True)
def _session_mcp_loop():
    _mcp_loop_runner.start()
    yield
    _mcp_loop_runner.stop()


# ── Fake MCP Server 管理（保持 session 启动，避免重复 subprocess） ────────


@pytest.fixture(scope="session")
def fake_server_path() -> str:
    return str(Path(__file__).parent / "fake_data_server.py")


@pytest.fixture(scope="session")
def fake_mcp_config(fake_server_path: str) -> MCPServerConfig:
    return MCPServerConfig(
        name="fake-data-server",
        command=sys.executable,
        args=[fake_server_path],
        transport="stdio",
        isolation="host_trusted",
        toolset="connector",
    )


@pytest.fixture(scope="session")
def persistent_mcp_client(fake_mcp_config: MCPServerConfig) -> MCPClient:
    """session 内所有测试共用一个 MCP 子进程 + MCPClient 连接。

    子进程 stdout 不被重定向（避免 pipe drain 问题）。
    每次 call_tool 来自同一 loop。
    """
    client = MCPClient(fake_mcp_config.name, fake_mcp_config)
    _mcp_loop_runner.run(client.start(), timeout=15)
    return client


@pytest.fixture
def mcp_manager(persistent_mcp_client: MCPClient) -> _FakeManager:
    return _FakeManager(persistent_mcp_client)


@pytest.fixture
def isolated_memory_db():
    """In-memory DB with no-auto-close wrapper。

    handler 会调用 conn.close() 释放连接由它管理的连接生命周期；
    但 connection 对象被后续 test body 仍在用（assert 阶段），
    所以 wrap 成 no-op close 的代理。
    """
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    raw.execute("PRAGMA foreign_keys=ON;")
    raw.execute("PRAGMA journal_mode=WAL;")
    raw.execute("PRAGMA busy_timeout=5000;")
    raw.row_factory = sqlite3.Row
    migrate(raw)

    class _NoCloseConn:
        """代理连接：close() 是 no-op（底层连接由 fixture 管理）。"""

        def __init__(self, real):
            self._r = real

        def execute(self, *a, **kw):
            return self._r.execute(*a, **kw)

        def commit(self):
            return self._r.commit()

        def close(self):
            pass  # no-op，防止 handler 关闭 fixture 的底层连接

        @property
        def row_factory(self):
            return self._r.row_factory

        @row_factory.setter
        def row_factory(self, v):
            self._r.row_factory = v

    conn = _NoCloseConn(raw)
    yield conn
    raw.close()


@pytest.fixture
def ctx_factory():
    def _make(mcp_manager: _FakeManager, conn: sqlite3.Connection) -> TaskHandlerContext:
        return TaskHandlerContext(
            connection_factory=lambda p=conn: p,
            workspace_path="",
            mcp_manager=mcp_manager,
        )

    return _make


def _seed_connector_and_mapping(
    conn: sqlite3.Connection,
    connector_id: str = "conn-test-mcp",
) -> None:
    connector = Connector(
        connector_id=connector_id,
        connector_type="mcp",
        name="test-mcp-feed",
        url="mcp://fake-data-server/list_items",
        site_link="",
    )
    ConnectorRepository(conn).insert(connector)
    mapping = MCPConnectorConfig(
        connector_id=connector_id,
        server_name="fake-data-server",
        tool_name="list_items",
        arguments_template={"limit": 50, "cursor": ""},
        items_path="items",
        next_cursor_path="nextCursor",
        has_more_path="hasNext",
        stable_id_path="id",
        updated_at_path="publishedAt",
        title_path="title",
        body_path="summary",
        url_path="url",
        topic_path="category",
        max_pages_per_poll=5,
        max_items_per_poll=200,
        max_output_bytes=1048576,
        config_version=1,
    )
    MCPConnectorConfigRepository(conn).save(mapping)
    conn.commit()


def _make_task(connector_id: str, task_id: str = "task-1") -> Task:
    return Task(
        task_id=task_id,
        task_type="mcp_connector.poll",
        payload_ref=connector_id,
        status=TaskStatus.queued,
    )


# ── 测试 ──────────────────────────────────────────────────────────────────────


def test_full_pipeline_two_pages(
    isolated_memory_db,
    mcp_manager: _FakeManager,
    ctx_factory,
):
    _seed_connector_and_mapping(isolated_memory_db)
    ctx = ctx_factory(mcp_manager, isolated_memory_db)
    result = handle_mcp_connector_poll(_make_task("conn-test-mcp"), ctx)

    assert "pages=2" in result, result
    assert "fetched=8" in result, result
    assert "new=8" in result, result
    assert "dup=0" in result, result

    cnt = isolated_memory_db.execute(
        "SELECT COUNT(*) FROM connector_items WHERE connector_id=?",
        ("conn-test-mcp",),
    ).fetchone()[0]
    assert cnt == 8
    statuses = {
        row[0]
        for row in isolated_memory_db.execute(
            "SELECT DISTINCT status FROM connector_items WHERE connector_id=?",
            ("conn-test-mcp",),
        ).fetchall()
    }
    assert statuses <= {"digest", "silent"}
    assert "new" not in statuses


def test_model_enrichment_runs_outside_write_transaction(
    isolated_memory_db,
    mcp_manager: _FakeManager,
    ctx_factory,
    monkeypatch,
):
    import cogito.service.mcp_connector_handler as handler_module

    _seed_connector_and_mapping(isolated_memory_db)
    ctx = ctx_factory(mcp_manager, isolated_memory_db)
    ctx.model_router = object()
    transaction_states: list[bool] = []

    async def fake_summary(title, body, model_router):
        transaction_states.append(isolated_memory_db._r.in_transaction)
        return f"summary:{title}"

    monkeypatch.setattr(handler_module, "summarize_item", fake_summary)
    handle_mcp_connector_poll(_make_task("conn-test-mcp"), ctx)

    assert transaction_states
    assert transaction_states == [False] * len(transaction_states)


def test_idempotent_repoll(
    isolated_memory_db,
    mcp_manager: _FakeManager,
    ctx_factory,
):
    _seed_connector_and_mapping(isolated_memory_db)
    ctx = ctx_factory(mcp_manager, isolated_memory_db)
    handle_mcp_connector_poll(_make_task("conn-test-mcp", "t1"), ctx)
    result2 = handle_mcp_connector_poll(_make_task("conn-test-mcp", "t2"), ctx)

    # cursor 已推到最后；第二次不再有新 item
    assert "new=0" in result2

    cnt = isolated_memory_db.execute(
        "SELECT COUNT(*) FROM connector_items WHERE connector_id=?",
        ("conn-test-mcp",),
    ).fetchone()[0]
    assert cnt == 8


def test_outbox_events_emitted(
    isolated_memory_db,
    mcp_manager: _FakeManager,
    ctx_factory,
):
    _seed_connector_and_mapping(isolated_memory_db)
    ctx = ctx_factory(mcp_manager, isolated_memory_db)
    handle_mcp_connector_poll(_make_task("conn-test-mcp"), ctx)

    cnt = isolated_memory_db.execute(
        "SELECT COUNT(*) FROM outbox_events "
        "WHERE event_type='SourceEventIngested' "
        "AND origin LIKE 'mcp:fake-data-server:list_items'",
    ).fetchone()[0]
    assert cnt == 8


def test_source_metadata_preserved(
    isolated_memory_db,
    mcp_manager: _FakeManager,
    ctx_factory,
):
    _seed_connector_and_mapping(isolated_memory_db)
    ctx = ctx_factory(mcp_manager, isolated_memory_db)
    handle_mcp_connector_poll(_make_task("conn-test-mcp"), ctx)

    row = isolated_memory_db.execute(
        "SELECT source_metadata_json FROM connector_items WHERE source_item_id='fake-01'",
    ).fetchone()
    assert row is not None
    meta = json.loads(row[0])
    assert meta.get("id") == "fake-01"
    assert meta.get("title", "").startswith("Fake item")


def test_ingestion_batch_logging(
    isolated_memory_db,
    mcp_manager: _FakeManager,
    ctx_factory,
):
    _seed_connector_and_mapping(isolated_memory_db)
    ctx = ctx_factory(mcp_manager, isolated_memory_db)
    handle_mcp_connector_poll(_make_task("conn-test-mcp"), ctx)

    cnt = isolated_memory_db.execute(
        "SELECT COUNT(*) FROM ingestion_batches WHERE connector_id=? AND status='committed'",
        ("conn-test-mcp",),
    ).fetchone()[0]
    assert cnt >= 1
