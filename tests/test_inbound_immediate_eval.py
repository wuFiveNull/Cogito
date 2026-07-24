"""PLAN-17 R6 PA-P1-02: 入站规范 Event 激活 proactive.evaluate。

Scheduler 注释提到 schedule_immediate_evaluate 但源码不存在 — 由
InboundImmediateEvalConsumer 替代实现。
"""

from __future__ import annotations

import sqlite3
from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
from cogito.service.event_consumers import EventConsumerRegistry, InboundImmediateEvalConsumer
from cogito.service.event_subscription import CanonicalEventConsumerWorker, CanonicalConsumerEvent
from cogito.service.inbound_service import InboundService
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _env(msg_id="pm-1"):
    return ChannelEnvelope(
        channel_instance_id="ci-1",
        channel_type="qq",
        platform_sender_id="u-1",
        platform_message_id=msg_id,
        message_id=msg_id,
        content_parts=[{"content_type": "text", "inline_data": "hi"}],
        metadata={},
        reply_route=ReplyRoute(),
        received_at="2026-07-12T00:00:00Z",
        capability_snapshot=None,
    )


def test_inbound_creates_immediate_eval_task():
    """入站 canonical Event 触发 evaluate Task 创建。"""
    conn = _fresh_db()
    svc = InboundService(conn)
    res = svc.accept(_env())
    assert res.is_new

    registry = EventConsumerRegistry()
    registry.register(InboundImmediateEvalConsumer(default_principal_id="owner"))
    worker = CanonicalEventConsumerWorker(EventStore(conn), registry)
    assert worker.run_pending(conn) == 1

    # 验证 task 已创建
    t = conn.execute(
        "SELECT task_id, task_type, status, origin FROM tasks WHERE origin='inbound-immediate-eval'"
    ).fetchone()
    assert t is not None, "immediate evaluate task 必须被创建"
    assert t["task_type"] == "proactive.evaluate"
    assert t["status"] == "queued"


def test_idempotent_per_turn_per_day():
    """同一 turn 当日第二次消费不重复创建。"""
    conn = _fresh_db()
    InboundService(conn).accept(_env("pm-1"))
    registry = EventConsumerRegistry()
    registry.register(InboundImmediateEvalConsumer(default_principal_id="owner"))
    worker = CanonicalEventConsumerWorker(EventStore(conn), registry)
    assert worker.run_pending(conn) == 1
    # 第二次扫描会遇到同一 Event，但 consumer 的消费幂等键不会再建 Task。
    assert worker.run_pending(conn) == 1
    n = conn.execute("SELECT COUNT(*) FROM tasks WHERE origin='inbound-immediate-eval'").fetchone()[
        0
    ]
    assert n == 1, f"Idempotent: only 1 task expected, got {n}"


def test_registry_registers_consumer():
    """PLAN-17 R6: registry 应该能找到 InboundImmediateEvalConsumer。"""
    registry = EventConsumerRegistry()
    registry.register(InboundImmediateEvalConsumer(default_principal_id="owner"))
    event = CanonicalConsumerEvent(
        event_id="x",
        event_type="InboundMessageAccepted",
        aggregate_type="message",
        aggregate_id="m",
        aggregate_version=1,
        payload_ref=None,
        content_hash="",
        schema_version="1.0",
        correlation_id="",
        causation_id="",
        origin="qq",
        trust_label="unverified",
    )
    consumer = registry.find(event)
    assert consumer is not None
    assert consumer.name == "inbound-immediate-eval"
