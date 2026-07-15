"""interaction-web 测试 fixtures：构建一个充填测试数据的临时 SQLite DB。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest

from cogito.config import Config
from cogito.capability.models import ToolDef
from cogito.capability.plugin_runtime import PluginManifest, SqlitePluginRuntime
from cogito.capability.registry import CapabilityRegistry
from cogito.interaction_web.server import create_app
from cogito.store.migration import migrate


@pytest.fixture
def client():
    db_dir = tempfile.mkdtemp()
    db_path = os.path.join(db_dir, "test.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    migrate(conn)
    conn.execute(
        "INSERT INTO principals (principal_id,principal_type,status,created_at) VALUES ('owner','owner','active','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO endpoints (endpoint_id,channel_type,channel_instance_id,platform_account_id,principal_id,endpoint_ref,capabilities,status,verified_at) VALUES ('ep1','web','web-main','web-main','owner','ep_ref','[]','active',NULL)"
    )
    conn.execute(
        "INSERT INTO conversations (conversation_id,conversation_endpoint_id,platform_conversation_id,conversation_type,status,principal_scope,context_partition_policy) VALUES ('c1','ep1','web:chat1','private','active','owner','isolated')"
    )
    conn.execute(
        "INSERT INTO sessions (session_id,conversation_id,context_partition_key,reset_generation,status,created_at) VALUES ('s1','c1','c1',0,'active','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO turns (turn_id,session_id,input_message_id,status,priority,version,created_at) VALUES ('t1','s1','m1','completed',80,1,1700000000000)"
    )
    conn.execute(
        "INSERT INTO tasks (task_id,task_type,status,priority,idempotency_key,origin,created_at) VALUES ('task1','connector.poll','failed',40,'k1','system',1700000000000)"
    )
    conn.execute(
        "INSERT INTO memory_items (memory_id,kind,subject,predicate,value,principal_id,status,confidence,importance,created_at) VALUES ('mem1','fact','site','uptime','99%','owner','candidate',0.7,0.5,'2026-01-01T00:00:00Z')"
    )
    # confirmed memory for search test (RetrievalService 仅返回 confirmed)
    conn.execute(
        "INSERT INTO memory_items (memory_id,kind,subject,predicate,value,principal_id,status,confidence,importance,created_at) VALUES ('mem2','fact','language','prefers','Chinese','owner','confirmed',0.9,0.8,'2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO connectors (connector_id,connector_type,name,url,status,created_at) VALUES ('con1','rss','Hacker News','https://hnrss.org/frontpage','active',1700000000000)"
    )
    conn.execute(
        "INSERT INTO model_calls (model_call_id,attempt_id,status,input_tokens,output_tokens,started_at,completed_at,trace_id) VALUES ('mc1','a1','success',100,200,1700000000000,1700000010000,'tr1')"
    )
    conn.execute(
        "INSERT INTO run_attempts (attempt_id,turn_id,attempt_no,status,started_at,finished_at) VALUES ('a1','t1','1','succeeded',1700000000000,1700000005000)"
    )
    conn.execute(
        "INSERT INTO approvals (approval_id,request,status,expires_at,created_at) VALUES ('ap1','{}','pending','2027-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO deliveries (delivery_id,status,idempotency_key,created_at) VALUES ('d1','failed','dk1',1700000000000)"
    )
    conn.commit()
    plugin_runtime = SqlitePluginRuntime(conn)
    plugin_runtime.install(PluginManifest(plugin_id="some-mcp"))

    cfg = Config()
    cfg.storage.db_path = db_path
    cfg.workspace_path = db_dir
    cfg.capability = cfg.capability._from_raw(
        {
            "mcp": {
                "servers": {
                    "docs": {
                        "transport": "stdio",
                        "command": "docs",
                        "isolation": "host_trusted",
                    },
                },
            },
        }
    )

    async def now_handler(_args, _context):
        return "now"

    registry = CapabilityRegistry()
    registry.register(
        ToolDef(
            "now",
            "time",
            {"type": "object"},
            now_handler,
            output_schema={"type": "string"},
        )
    )

    class _MCPManager:
        @staticmethod
        def health_states():
            return {
                "docs": {
                    "status": "healthy",
                    "reconnect_attempts": 1,
                    "schema_changes": 2,
                    "last_error": "",
                },
            }

    runtime = SimpleNamespace(
        plugin_runtime=plugin_runtime,
        local_gateway_client=None,
        config=cfg,
        runner=SimpleNamespace(_registry=registry),
        mcp_manager=_MCPManager(),
    )
    app = create_app(cfg, recovery_counts={"tasks": 1}, runtime=runtime)
    from fastapi.testclient import TestClient

    c = TestClient(app)
    yield c
    plugin_runtime.close()
    conn.close()
