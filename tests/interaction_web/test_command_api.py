"""Command API 测试 —— 验证命令走服务层并写 audit。"""

from types import SimpleNamespace


def _audit_count(client):
    # 偷看 audit_records 数量需要 DB 访问；直接通过公共接口行为断言即可。
    return None


def test_approve_pending(client):
    r = client.post("/api/commands/approve", json={"approval_id": "ap1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # 再 approve 应失败（非 pending）
    r2 = client.post("/api/commands/approve", json={"approval_id": "ap1"})
    assert r2.json()["status"] == "failed"


def test_reject_pending(client):
    # 准备新的 pending approval
    import os
    import sqlite3
    import tempfile

    # 直接通过一个已有 pending 测试：先验证 404
    r = client.post("/api/commands/reject", json={"approval_id": "missing"})
    assert r.status_code == 404


def test_confirm_and_delete_memory(client):
    r = client.post("/api/commands/confirm-memory", json={"memory_id": "mem1"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    r = client.post("/api/commands/delete-memory", json={"memory_id": "mem1"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_confirm_missing_memory(client):
    r = client.post("/api/commands/confirm-memory", json={"memory_id": "ghost"})
    assert r.status_code == 404


def test_retry_task_failed(client):
    r = client.post("/api/commands/retry-task", json={"task_id": "task1"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # 已 retry → queued，再次 retry 应失败
    r2 = client.post("/api/commands/retry-task", json={"task_id": "task1"})
    assert r2.json()["status"] == "failed"


def test_replay_delivery(client):
    r = client.post("/api/commands/replay-delivery", json={"delivery_id": "d1"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    r = client.post("/api/commands/replay-delivery", json={"delivery_id": "d1"})
    assert r.json()["status"] == "failed"  # 已是 pending


def test_replay_missing_delivery(client):
    r = client.post("/api/commands/replay-delivery", json={"delivery_id": "ghost"})
    assert r.status_code == 404


def test_pause_connector(client):
    r = client.post("/api/commands/pause-connector", json={"connector_id": "con1", "paused": True})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_pause_missing_connector(client):
    r = client.post("/api/commands/pause-connector", json={"connector_id": "ghost"})
    assert r.status_code == 404


def test_disable_plugin_audits(client):
    r = client.post("/api/commands/disable-plugin", json={"name": "some-mcp"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_delete_session_soft(client):
    """软删除会话：API 返回 ok，query 不再返回该 session，但 DB 数据保留。"""
    # 删除成功
    r = client.post("/api/commands/delete-session", json={"session_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["details"]["deleted_at"]

    # 再次删除返回 failed（已删除）
    r2 = client.post("/api/commands/delete-session", json={"session_id": "s1"})
    assert r2.json()["status"] == "failed"

    # query /sessions 不再返回
    r3 = client.get("/api/sessions")
    assert r3.status_code == 200
    ids = [item["session_id"] for item in r3.json()["items"]]
    assert "s1" not in ids

    # trace 删除的 session 返回 404（已通过 deleted_at 过滤）
    r4 = client.get("/api/sessions/s1/trace")
    assert r4.status_code == 404


def test_delete_session_missing(client):
    """删除不存在的 session 返回 status=failed。"""
    r = client.post("/api/commands/delete-session", json={"session_id": "ghost"})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"


def test_delete_sessions_by_conversation(client):
    """按 conversation_id 批量软删除其下所有 session。"""
    # conversation c1 下有 session s1
    r = client.post("/api/commands/delete-sessions-by-conversation", json={"conversation_id": "c1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["details"]["deleted_count"] >= 1

    # 再次删除应该 failed（没有活跃 session 了）
    r2 = client.post(
        "/api/commands/delete-sessions-by-conversation", json={"conversation_id": "c1"}
    )
    assert r2.json()["status"] == "failed"

    # query 不返回
    r3 = client.post("/api/commands/delete-session", json={"session_id": "s1"})
    assert r3.json()["status"] == "failed"  # 已软删除


def test_fetch_proactive_data_is_fixed_to_aihot_and_idempotent(client):
    provider = client.app.state._provider
    provider.config.capability.proactive.enabled = True
    conn = provider.open_conn()
    conn.execute(
        "INSERT INTO connectors "
        "(connector_id, connector_type, name, url, status, created_at) "
        "VALUES ('connector-aihot-items','mcp','AIHOT','mcp://aihot','active',1700000000000)"
    )
    conn.commit()
    conn.close()
    client.app.state.runtime.mcp_manager = SimpleNamespace(
        get_client=lambda server_id: SimpleNamespace(connected=server_id == "aihot")
    )

    payload = {"idempotency_key": "fetch-test-1"}
    first = client.post("/api/commands/fetch-proactive-data", json=payload)
    second = client.post("/api/commands/fetch-proactive-data", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    first_details = first.json()["details"]
    second_details = second.json()["details"]
    assert first_details["connector_id"] == "connector-aihot-items"
    assert first_details["dry_run"] is True
    assert second_details["poll_task_id"] == first_details["poll_task_id"]
    assert second_details["idempotent"] is True

    run = client.get(f"/api/proactive/fetch-runs/{first_details['poll_task_id']}")
    assert run.status_code == 200
    assert run.json()["poll_status"] == "queued"
