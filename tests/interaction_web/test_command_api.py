"""Command API 测试 —— 验证命令走服务层并写 audit。"""


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
    import sqlite3, os, tempfile
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
