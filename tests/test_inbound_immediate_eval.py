"""PLAN-17 R6 PA-P1-02: InboundMessageAccepted Outbox event 激活 proactive.evaluate。

Scheduler 注释提到 schedule_immediate_evaluate 但源码不存在 — 由
InboundImmediateEvalConsumer 替代实现。
"""
from __future__ import annotations

import sqlite3
import time

from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
from cogito.service.event_consumers import (
    EventConsumerRegistry, InboundImmediateEvalConsumer)
from cogito.service.inbound_service import InboundService
from cogito.service.outbox_worker import OutboxWorker
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
        channel_instance_id="ci-1", channel_type="qq",
        platform_sender_id="u-1", platform_message_id=msg_id,
        message_id=msg_id, content_parts=[{"content_type": "text", "inline_data": "hi"}],
        metadata={}, reply_route=ReplyRoute(),
        received_at="2026-07-12T00:00:00Z", capability_snapshot=None)


def test_inbound_creates_immediate_eval_task():
    """PLAN-17 R6 PA-P1-02: InboundMessageAccepted Outbox event 触发 evaluate Task 创建。"""
    conn = _fresh_db()
    svc = InboundService(conn)
    res = svc.accept(_env())
    assert res.is_new

    # Outbox 应含 InboundMessageAccepted event
    rows = conn.execute(
        "SELECT event_id, event_type, aggregate_id FROM outbox_events WHERE event_type='InboundMessageAccepted'").fetchall()
    assert len(rows) >= 1, [dict(r) for r in rows]
    event_id = rows[0]["event_id"]

    # 消费后应创建 proactive.evaluate Task
    consumer = InboundImmediateEvalConsumer(default_principal_id="owner")
    # 构造 Lease
    lease = __import__("cogito.service.outbox_worker", fromlist=["OutboxLease"]).OutboxLease(
        event_id=event_id, event_type="InboundMessageAccepted",
        aggregate_type="message", aggregate_id=rows[0]["aggregate_id"],
        aggregate_version=1, payload_ref=None, content_hash="",
        schema_version="1.0", correlation_id="", causation_id="",
        origin="qq", trust_label="unverified",
        created_at=str(int(time.time()*1000)), lease_version=1, attempt_count=0)
    ok = consumer.handle(conn, lease)
    assert ok is True

    # 验证 task 已创建
    t = conn.execute(
        "SELECT task_id, task_type, status, origin FROM tasks "
        "WHERE origin='inbound-immediate-eval'").fetchone()
    assert t is not None, "immediate evaluate task 必须被创建"
    assert t["task_type"] == "proactive.evaluate"
    assert t["status"] == "queued"


def test_idempotent_per_turn_per_day():
    """同一 turn 当日第二次消费不重复创建。"""
    conn = _fresh_db()
    InboundService(conn).accept(_env("pm-1"))
    rows = conn.execute(
        "SELECT event_id FROM outbox_events WHERE event_type='InboundMessageAccepted'").fetchone()
    event_id = rows[0]
    consumer = InboundImmediateEvalConsumer(default_principal_id="owner")
    lease = __import__("cogito.service.outbox_worker", fromlist=["OutboxLease"]).OutboxLease(
        event_id=event_id, event_type="InboundMessageAccepted",
        aggregate_type="message", aggregate_id="m-1",
        aggregate_version=1, payload_ref=None, content_hash="",
        schema_version="1.0", correlation_id="", causation_id="",
        origin="qq", trust_label="unverified",
        created_at=str(int(time.time()*1000)), lease_version=1, attempt_count=0)
    assert consumer.handle(conn, lease) is True
    # 第二次 consume — 已消费过 event_id, 仍返回 True 且不再新建 task
    assert consumer.handle(conn, lease) is True
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE origin='inbound-immediate-eval'").fetchone()[0]
    assert n == 1, f"Idempotent: only 1 task expected, got {n}"


def test_registry_registers_consumer():
    """PLAN-17 R6: registry 应该能找到 InboundImmediateEvalConsumer。"""
    registry = EventConsumerRegistry()
    registry.register(InboundImmediateEvalConsumer(default_principal_id="owner"))
    lease = __import__("cogito.service.outbox_worker", fromlist=["OutboxLease"]).OutboxLease(
        event_id="x", event_type="InboundMessageAccepted",
        aggregate_type="message", aggregate_id="m", aggregate_version=1,
        payload_ref=None, content_hash="", schema_version="1.0",
        correlation_id="", causation_id="", origin="qq",
        trust_label="unverified", created_at="1", lease_version=1, attempt_count=0)
    consumer = registry.find(lease)
    assert consumer is not None
    assert consumer.name == "inbound-immediate-eval"
