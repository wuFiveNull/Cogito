"""PLAN-17 R4 P0-05: 入站 Turn 到达后发射抢占信号，Drift 在安全点被捕获。

证据 1: request_preemption() 在 InboundService 中无生产调用 → 现在 InboundService.accept
在入站事务提交后调用 request_preemption。

验收: 通过真实 InboundService.accept (envelope) 创建 Turn 后，drift_preemption_signals 表
中应有 preempt_requested=1 记录，无需测试直接调用 helper。
"""

from __future__ import annotations

import sqlite3
import time

from cogito.config import DriftConfig
from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
from cogito.service.inbound_service import InboundService
from cogito.store.migration import migrate


def _fresh_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _env(platform_msg_id="pm-1", msg_id="m-1"):
    return ChannelEnvelope(
        channel_instance_id="ci-1",
        channel_type="qq",
        platform_sender_id="u-1",
        platform_message_id=platform_msg_id,
        message_id=msg_id,
        content_parts=[{"content_type": "text", "inline_data": "hi"}],
        metadata={},
        reply_route=ReplyRoute(),
        received_at="2026-07-12T00:00:00Z",
        capability_snapshot=None,
    )


def test_inbound_new_turn_emits_preemption_signal():
    """P0-05 Evidence 1 修复：InboundService 入站后应发射 preemption signal。"""
    conn = _fresh_db()
    dr_cfg = DriftConfig(enabled=True, default_principal_id="owner")
    svc = InboundService(conn, drift_preemption=dr_cfg)
    res = svc.accept(_env())
    assert res.is_new
    # 入站事务提交后抢占信号已写表
    row = conn.execute(
        "SELECT preempt_requested, reason FROM drift_preemption_signals WHERE principal_id='owner'"
    ).fetchone()
    assert row is not None, "入站后必须发射 preemption signal"
    assert row["preempt_requested"] == 1
    assert "inbound_turn" in (row["reason"] or "")


def test_inbound_idempotent_repeat_no_new_signal_overwrite():
    """重复入站不应重复写 signal (ON CONFLICT DO UPDATE 语义覆盖)。"""
    conn = _fresh_db()
    dr_cfg = DriftConfig(enabled=True, default_principal_id="owner")
    svc = InboundService(conn, drift_preemption=dr_cfg)
    svc.accept(_env("pm-1", "m-1"))
    # 同 message_id 再次 → 幂等，is_new=False，不重复发射
    res2 = svc.accept(_env("pm-1", "m-1"))
    assert res2.is_new is False
    n = conn.execute("SELECT COUNT(*) FROM drift_preemption_signals").fetchone()[0]
    assert n == 1


def test_inbound_with_drift_disabled_no_signal():
    """drift 未启用时 (drift_preemption=None)，入站不触碰 signal 表。"""
    conn = _fresh_db()
    svc = InboundService(conn, drift_preemption=None)
    svc.accept(_env("pm-x", "m-x"))
    n = conn.execute("SELECT COUNT(*) FROM drift_preemption_signals").fetchone()[0]
    assert n == 0
