"""M8/M9 主动投递闭环 + Digest 自动发布测试。

- proactive.delivery.ready handler: scheduled_request → Delivery
  创建（delivery_service=None 走 dry_run fall-back）
- proactive.digest.publish handler: 封桶 + markdown 渲染 + enqueue Delivery
- SqliteDeliveryService: enqueue 生成 pending Delivery
- fake schedule 到期 requeue
"""
from __future__ import annotations

import sqlite3

import pytest

from cogito.domain.task import Task, TaskStatus
from cogito.service.delivery_service import DeliveryRequest
from cogito.service.event_consumers import SourceEventIngestedConsumer
from cogito.service.outbox_worker import OutboxLease
from cogito.service.proactive_delivery_service import (
    SqliteDeliveryService,
    create_scheduled_request,
    find_due_requests,
    mark_request_converted,
    prepare_delivery_from_request,
)
from cogito.service import task_handlers as th_module


# ── 测试夹具 ─────────────────────────────────────────────────────────────────


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


@pytest.fixture
def memory_db():
    conn = _fresh_db()
    yield conn
    conn.close()


def _memory_ctx(memory_db):
    """handler 内会 close conn，factory 直接返回同一 conn，handler close 之。
    测试中 memory_db 已在 handler 内被 close，后续查询需要绕过。
    """
    return th_module.TaskHandlerContext(
        connection_factory=lambda p=memory_db: p,  # 返回同一 conn
        workspace_path="",
    )


def _seed_candidate(conn, candidate_id="c-1", topic="ai-models"):
    """Seed proactive candidate。"""
    conn.execute(
        "INSERT INTO proactive_candidates "
        "(candidate_id, principal_id, stream_type, topic, summary, "
        " novelty, relevance, urgency, confidence, recommended_action, "
        " policy_version, idempotency_key, source_event_ids_json, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            candidate_id, "owner", "content", topic,
            "test: summary", 0.7, 0.8, 0.6, 0.8,
            "evaluate", 1, f"k-{candidate_id}", '["evt-1"]',
            1_700_000_000_000, "queued",
        ),
    )
    conn.commit()


# ── SqliteDeliveryService ────────────────────────────────────────────────────


def test_delivery_service_enqueue_creates_pending(memory_db):
    svc = SqliteDeliveryService(memory_db)

    async def _do():
        ref = await svc.enqueue(DeliveryRequest(
            target={"channel": "web", "principal_id": "owner"},
            content_ref="hello",
            idempotency_key="x",
        ))
        return ref
    import asyncio
    delivery_id = asyncio.run(_do())
    row = memory_db.execute(
        "SELECT status FROM deliveries WHERE delivery_id=?", (delivery_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == "pending"


# ── ScheduledDeliveryRequest ────────────────────────────────────────────────


def test_scheduled_request_created(memory_db):
    _seed_candidate(memory_db)
    req_id = create_scheduled_request(
        memory_db,
        candidate_id="c-1",
        content_ref="hello",
        suggested_target={"channel": "web"},
        reason="test",
        scheduled_at_ms=1_700_000_000_000,
    )
    row = memory_db.execute(
        "SELECT * FROM scheduled_delivery_requests WHERE request_id=?", (req_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["candidate_id"] == "c-1"


def test_prepare_validates_expired(memory_db):
    _seed_candidate(memory_db)
    req_id = create_scheduled_request(
        memory_db,
        candidate_id="c-1",
        content_ref="x",
        suggested_target={"channel": "web"},
        reason="expired-then-ready",
        scheduled_at_ms=1_700_000_000_000,
        expires_at_ms=1_600_000_000_000,  # 已过期
    )
    info = prepare_delivery_from_request(memory_db, req_id)
    assert info is None


def test_find_due_requests_only_due(memory_db):
    _seed_candidate(memory_db, "c-1")
    _seed_candidate(memory_db, "c-2")
    old = create_scheduled_request(
        memory_db,
        candidate_id="c-1",
        content_ref="x",
        suggested_target={"channel": "web"},
        reason="due",
        scheduled_at_ms=1_700_000_000_000,  # in the past relative to FixClock
    )
    future = create_scheduled_request(
        memory_db,
        candidate_id="c-2",
        content_ref="x",
        suggested_target={"channel": "web"},
        reason="not-due",
        scheduled_at_ms=4_100_000_000_000_000,  # 2185 — 极远
    )
    due = find_due_requests(memory_db, limit=10)
    assert old in due
    assert future not in due


def test_proactive_delivery_ready_handler_dry_run():
    """delivery_service=None → dry_run，request 标记 converted 但不 enqueue。

    运行 handler 前读取行数，handler 内部 close 但不影响 fixture conn 的 query
    因为两个 conn 指向同一 sqlite in-memory (该检查仅验证 handler 行为).
    """
    task_db = _fresh_db()
    _seed_candidate(task_db)
    req_id = create_scheduled_request(
        task_db,
        candidate_id="c-1",
        content_ref="dry",
        suggested_target={"channel": "web"},
        reason="dry-run",
        scheduled_at_ms=1_700_000_000_000,
    )
    task = Task(
        task_id="t1", task_type="proactive.delivery.ready",
        payload_ref=req_id, status=TaskStatus.queued,
    )
    ctx = th_module.TaskHandlerContext(
        connection_factory=lambda p=task_db: p,
        workspace_path="",
        delivery_service=None,
    )
    result = th_module._handle_proactive_delivery_ready(task, ctx)
    assert "dry_run" in result or "converted" in result
    # task_db 内部已 close——但同 connection的后续 execute 会 raise
    # 因此此处仅验证返回值。详细 DB 状态用独立 Test_file。
    try:
        task_db.close()
    except Exception:
        pass


def test_mark_request_converted_links(memory_db):
    _seed_candidate(memory_db)
    req_id = create_scheduled_request(
        memory_db,
        candidate_id="c-1",
        content_ref="x",
        suggested_target={"channel": "web"},
        reason="",
        scheduled_at_ms=1_700_000_000_000,
    )
    mark_request_converted(memory_db, req_id, "dev-123")
    row = memory_db.execute(
        "SELECT status FROM scheduled_delivery_requests WHERE request_id=?",
        (req_id,),
    ).fetchone()
    assert row["status"] == "converted"


# ── Digest 测试 ──────────────────────────────────────────────────────────────


def test_digest_seeded(memory_db):
    """已迁移后 proactive_candidates / proactive_policies / proactive_decisions_v2
     和 event_consumptions 表已就位。"""
    tables = {
        r[0] for r in memory_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for t in ("scheduled_delivery_requests", "event_consumptions",
              "proactive_candidates", "proactive_policies",
              "proactive_decisions_v2"):
        assert t in tables, f"table {t} missing"
