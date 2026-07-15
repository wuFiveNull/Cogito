"""Query API 测试 —— 验证各只读 endpoint 返回结构与真实数据。"""


def test_status_returns_counts_and_recovery(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["turns"] == 1
    assert body["counts"]["tasks"] == 1
    assert body["counts"]["connectors"] == 1
    assert body["recovery"] == {"tasks": 1}


def test_usage_returns_windowed_and_total(client):
    r = client.get("/api/usage")
    assert r.status_code == 200
    body = r.json()
    # seed started_at 为 2023年，在 24h 窗口外 → windowed 为 0；total 全量统计为 1 次
    assert body["windowed"]["calls"] == 0
    assert body["total"]["calls"] == 1
    assert body["total"]["input_tokens"] == 100
    assert body["total"]["output_tokens"] == 200


def test_list_turns_and_detail(client):
    r = client.get("/api/turns")
    assert r.status_code == 200
    assert r.json()["total"] == 1
    r = client.get("/api/turns/t1")
    assert r.status_code == 200
    assert r.json()["turn"]["turn_id"] == "t1"
    assert len(r.json()["attempts"]) == 1
    r = client.get("/api/turns/t1/attempts")
    assert r.status_code == 200
    assert len(r.json()["attempts"]) == 1


def test_turns_filter_by_status(client):
    r = client.get("/api/turns?status=completed")
    assert r.status_code == 200
    assert r.json()["total"] == 1
    r = client.get("/api/turns?status=running")
    assert r.json()["total"] == 0


def test_turn_not_found(client):
    r = client.get("/api/turns/nope")
    assert r.status_code == 404


def test_list_tasks_and_detail(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert r.json()["total"] == 1
    r = client.get("/api/tasks/task1")
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "failed"


def test_search_memory(client):
    # RetrievalService 仅返回 confirmed 记忆；mem2 为 confirmed
    r = client.get("/api/memory?q=Chinese")
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_list_connectors_channels_conversations_deliveries(client):
    assert client.get("/api/connectors").json()["items"][0]["name"] == "Hacker News"
    assert client.get("/api/channels").json()["items"][0]["channel_type"] == "web"
    assert len(client.get("/api/conversations").json()["items"]) >= 1
    d = client.get("/api/deliveries")
    assert d.status_code == 200
    assert d.json()["total"] == 1


def test_trace(client):
    r = client.get("/api/traces/tr1")
    assert r.status_code == 200
    assert len(r.json()["model_calls"]) == 1
    r = client.get("/api/traces/missing")
    assert r.status_code == 404


def test_plugins(client):
    r = client.get("/api/plugins")
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)


def test_tool_control_plane(client):
    response = client.get("/api/tools")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["contract_complete"] is True
    assert body["items"][0]["name"] == "now"

    detail = client.get("/api/tools/now")
    assert detail.status_code == 200
    assert detail.json()["output_schema"] == {"type": "string"}
    assert client.get("/api/tools/missing").status_code == 404


def test_mcp_control_plane(client):
    response = client.get("/api/mcp/status")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["items"][0]["name"] == "docs"
    assert body["items"][0]["status"] == "healthy"
    assert body["items"][0]["schema_changes"] == 2
