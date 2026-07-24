"""M5 幂等候选投影测试。

验证 SourceEventIngestedConsumer:
- 单 SourceEvent 投影为 1 条 proactive_candidate（content 路）
- event_consumptions Inbox 幂等
- 重复 handle 不产生重复 candidate
- connector_items.status='digest' 才投影，silent/new 不投影
- source='mcp:…' origin 的 event 正确处理
"""

from __future__ import annotations

import json

import pytest

from cogito.domain.connector import ConnectorItem, ItemStatus
from cogito.service.event_consumers import SourceEventIngestedConsumer
from cogito.service.event_subscription import CanonicalConsumerEvent
from cogito.store.connector_repo import ConnectorItemRepository
from cogito.store.migration import migrate


# ── 测试夹具 ─────────────────────────────────────────────────────────────────


@pytest.fixture
def memory_db():
    import sqlite3

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    yield conn
    conn.close()


def _insert_digest_item(
    conn,
    item_id="item-1",
    title="AI 模型更新",
    body="重要更新...",
    relevance=0.7,
    connector_id="conn-mcp",
):
    """预置一条 status='digest' 的 connector_item（含必要的外键行）。"""
    from cogito.domain.connector import Connector, ConnectorRawItem
    from cogito.store.connector_repo import (
        ConnectorRepository,
        ConnectorRawRepository,
    )

    # connector FK (唯一主键；已存在则跳过)
    if not ConnectorRepository(conn).get(connector_id):
        conn.execute(
            "INSERT OR IGNORE INTO connectors "
            "(connector_id, connector_type, name, created_at) VALUES (?,?,?,0)",
            (connector_id, "mcp", "test"),
        )
    # raw item FK
    raw_id = f"raw-{item_id}"
    conn.execute(
        "INSERT OR IGNORE INTO connector_raw_items "
        "(raw_item_id, connector_id, source_item_id, content_hash, fetched_at) "
        "VALUES (?,?,?,?,0)",
        (raw_id, connector_id, f"ext-{item_id}", f"hash-{item_id}"),
    )
    conn.commit()
    item = ConnectorItem(
        item_id=item_id,
        connector_id=connector_id,
        raw_item_id=raw_id,
        source_item_id=f"ext-{item_id}",
        title=title,
        link="https://example.test/x",
        summary=body,
        published_at=None,
        content_hash=f"hash-{item_id}",
        relevance=relevance,
        status=ItemStatus.digest,
    )
    ConnectorItemRepository(conn).insert(
        item,
        source_metadata=json.dumps(
            {
                "id": item_id,
                "category": "ai-models",
            }
        ),
    )
    conn.commit()


def _make_source_event_ingested(item_id):
    return CanonicalConsumerEvent(
        event_id=f"evt-{item_id}",
        event_type="SourceEventIngested",
        aggregate_type="source",
        aggregate_id=item_id,
        aggregate_version=1,
        payload_ref=item_id,
        content_hash=f"hash-{item_id}",
        schema_version="1",
        correlation_id="",
        causation_id="",
        origin="mcp:fake-data-server:list_items",
        trust_label="external_unverified",
    )


# ── 真实测试 ─────────────────────────────────────────────────────────────────


def test_single_event_projected(memory_db):
    _insert_digest_item(memory_db)
    consumer = SourceEventIngestedConsumer(default_principal_id="owner")
    lease = _make_source_event_ingested("item-1")

    assert consumer.can_handle(lease)
    assert consumer.handle(memory_db, lease) is True

    cnt = memory_db.execute(
        "SELECT COUNT(*) FROM proactive_candidates WHERE idempotency_key!=''",
    ).fetchone()[0]
    assert cnt == 1

    row = memory_db.execute(
        "SELECT principal_id, stream_type, topic, status, source_event_ids_json "
        "FROM proactive_candidates LIMIT 1",
    ).fetchone()
    assert row is not None
    assert row["principal_id"] == "owner"
    assert row["stream_type"] == "content"
    assert row["status"] == "evaluating"
    events = json.loads(row["source_event_ids_json"])
    assert events == ["evt-item-1"]


def test_inbox_idempotent(memory_db):
    """重复消费同一 event_id 不建重复 candidate。"""
    _insert_digest_item(memory_db)
    consumer = SourceEventIngestedConsumer(default_principal_id="owner")
    lease = _make_source_event_ingested("item-1")

    assert consumer.handle(memory_db, lease) is True
    # 第二次 with same event_id - 应该 succeed 但不建 candidate
    assert consumer.handle(memory_db, lease) is True

    cnt = memory_db.execute("SELECT COUNT(*) FROM proactive_candidates").fetchone()[0]
    assert cnt == 1

    # Inbox 标记 succeeded
    inbox = memory_db.execute(
        "SELECT status FROM event_consumptions WHERE consumer_name=? AND event_id=?",
        (consumer.name, lease.event_id),
    ).fetchone()
    assert inbox is not None
    assert inbox["status"] == "succeeded"


def test_status_digest_only_projected(memory_db):
    """只有 status='digest' 才投影候选。"""
    # 预置 connector + raw_item FK
    conn = memory_db
    conn.execute(
        "INSERT OR IGNORE INTO connectors "
        "(connector_id, connector_type, name, created_at) VALUES (?,?,?,0)",
        ("conn-mcp", "mcp", "test"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO connector_raw_items "
        "(raw_item_id, connector_id, content_hash, fetched_at) VALUES (?,?,?,0)",
        ("raw-shared", "conn-mcp", "h-shared"),
    )
    conn.commit()
    # 写入 new / silent 两个 item
    for st in ("new", "silent"):
        item_id = f"item-{st}"
        ConnectorItemRepository(conn).insert(
            ConnectorItem(
                item_id=item_id,
                connector_id="conn-mcp",
                raw_item_id="raw-shared",
                source_item_id=f"ext-{item_id}",
                title="t",
                summary="s",
                content_hash=f"h-{item_id}",
                status=ItemStatus(st),
            )
        )
    conn.commit()

    cnt = conn.execute("SELECT COUNT(*) FROM proactive_candidates").fetchone()[0]
    assert cnt == 0


def test_manual_mock_source_projects_an_alert_candidate(memory_db):
    _insert_digest_item(
        memory_db,
        item_id="mock-item",
        connector_id="connector-proactive-mock",
    )
    consumer = SourceEventIngestedConsumer(default_principal_id="owner")

    assert consumer.handle(memory_db, _make_source_event_ingested("mock-item")) is True

    row = memory_db.execute(
        "SELECT stream_type, urgency FROM proactive_candidates WHERE candidate_id!=''"
    ).fetchone()
    assert row["stream_type"] == "alert"
    assert row["urgency"] == 1.0


def test_can_handle_filter(memory_db):
    consumer = SourceEventIngestedConsumer(default_principal_id="owner")

    source_evt = _make_source_event_ingested("item-1")
    assert consumer.can_handle(source_evt) is True

    other_evt = CanonicalConsumerEvent(
        event_id="e2",
        event_type="InboundMessageAccepted",
        aggregate_type="message",
        aggregate_id="m1",
        aggregate_version=1,
        payload_ref=None,
        content_hash="",
        schema_version="1",
        correlation_id="",
        causation_id="",
        origin="channel",
        trust_label="internal",
    )
    assert consumer.can_handle(other_evt) is False
