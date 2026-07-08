"""Plan 05 M5 集成测试：崩溃恢复 + Web 订阅占位清理。

验证：
- WebChannelAdapter.subscribe 时清理本会话遗留的 interrupted 流式占位气泡，
  回灌 assistant.delete 事件（断线重连后浏览器删除 "…" 气泡）。
- 模拟流式过程崩溃（Turn 仍 running + 孤儿 streaming delivery），经 RecoveryService
  撤回并复位 Turn，再次 run_once 能重新流式并定稿（重放路径）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from cogito.channel.drivers.web import WebChannelAdapter
from cogito.channel.manager import ChannelManager
from cogito.config import Config
from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
from cogito.inbound.dispatcher import InboundDispatcher
from cogito.model.stub_provider import StubModelProvider
from cogito.service.agent_runner import RunOutcome, build_agent_runner
from cogito.service.channel_gateway import ChannelGateway
from cogito.service.inbound_service import InboundService
from cogito.service.recovery_service import RecoveryService
from cogito.store.migration import migrate
from cogito.store.time_utils import now_ms


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def test_web_adapter_reconciles_interrupted_on_subscribe() -> None:
    """订阅时清理本会话遗留的 interrupted 流式占位气泡。"""
    conn = _make_conn()
    conv_id = "web:reconcile"
    # 模拟崩溃后变成 interrupted 的孤儿流式占位
    target = {"delivery_id": "d-orphan", "conversation_id": conv_id}
    now = now_ms()
    conn.execute(
        "INSERT INTO deliveries (delivery_id, target_snapshot, status, "
        "idempotency_key, created_at, content_mode, turn_id, platform_message_id) "
        "VALUES (?, ?, 'interrupted', ?, ?, 'provisional', 't-orphan', ?)",
        ("d-orphan", json.dumps(target), "idk-orphan", now, "pm-orphan"),
    )
    conn.commit()

    adapter = WebChannelAdapter(adapter_id="web", channel_type="web", conn=conn)
    q = adapter.subscribe(conv_id)

    # 队列应回灌一条 delete 事件，指向遗留占位
    assert not q.empty(), "expected a reconciled delete event in the queue"
    item = q.get_nowait()
    assert item.get("kind") == "delete"
    assert item.get("platform_message_id") == "pm-orphan"
    assert item.get("conversation_id") == conv_id

    # 其他会话不受影响
    other = adapter.subscribe("web:other")
    assert other.empty()


async def _run_replay_scenario() -> tuple[sqlite3.Connection, bool, str]:
    """模拟崩溃 → 恢复 → 重放，返回 (conn, 重放是否完成, conv_id)。"""
    conn = _make_conn()
    config = Config()
    provider = StubModelProvider()
    runner = build_agent_runner(config=config, connection=conn, provider=provider)

    adapter = WebChannelAdapter(adapter_id="web", channel_type="web")
    manager = ChannelManager(InboundDispatcher(InboundService(conn)))
    manager._adapters["web"] = adapter
    await adapter.start()
    gateway = ChannelGateway(conn, manager)
    runner.channel_gateway = gateway
    runner.channel_manager = manager

    conv_id = "web:replay"
    envelope = ChannelEnvelope(
        channel_type="web",
        channel_instance_id="web",
        platform_sender_id="tester",
        platform_conversation_id=conv_id,
        content_parts=[{"content_type": "text", "inline_data": "讲个笑话"}],
        reply_route=ReplyRoute(channel_instance_id="web", platform_conversation_id=conv_id),
        trust_label="authenticated",
    )
    inbound = InboundService(conn)
    result = inbound.accept(envelope)
    turn_id = result.turn_id
    assert turn_id, "inbound should produce a turn"

    # 模拟"首次流式尝试崩溃"：把 Turn 置为 running（带 running attempt），
    # 并创建一个孤儿 streaming delivery（平台已发出占位 "…"，但未定稿）。
    now = now_ms()
    attempt_id = f"{turn_id}_crashed"
    conn.execute(
        "UPDATE turns SET status='running', active_attempt_id=? WHERE turn_id=?",
        (attempt_id, turn_id),
    )
    conn.execute(
        "INSERT INTO run_attempts (attempt_id, turn_id, attempt_no, status, "
        "lease_version, lease_expires_at, started_at) "
        "VALUES (?, ?, 1, 'running', 1, ?, ?)",
        (attempt_id, turn_id, now - 60_000, now),  # 租约已过期
    )
    target = {"delivery_id": "d-crashed", "conversation_id": conv_id}
    conn.execute(
        "INSERT INTO deliveries (delivery_id, target_snapshot, status, "
        "idempotency_key, created_at, content_mode, turn_id, platform_message_id) "
        "VALUES (?, ?, 'streaming', ?, ?, 'provisional', ?, 'pm-crashed')",
        ("d-crashed", json.dumps(target), "idk-crashed", now, turn_id),
    )
    conn.commit()

    # 启动恢复：撤回孤儿 delivery 并复位 Turn 为 queued
    recovery = RecoveryService(conn)
    recovery.recover_all()
    d = conn.execute("SELECT status FROM deliveries WHERE delivery_id='d-crashed'").fetchone()
    assert d["status"] == "interrupted", d
    t = conn.execute("SELECT status FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
    assert t["status"] == "queued", t

    # 重放：再次 run_once 应成功走流式并定稿
    queue = adapter.subscribe(conv_id)
    outcome = await runner.run_once("replay-worker")
    completed = outcome == RunOutcome.completed

    # 收尾：确保队列被消费（避免挂起的 task 警告）
    for _ in range(60):
        try:
            await asyncio.wait_for(queue.get(), timeout=2.0)
        except (TimeoutError, asyncio.QueueEmpty):
            break

    return conn, completed, turn_id


def test_crash_then_replay_finalizes() -> None:
    """崩溃恢复后重放能生成新的已定稿流式 Delivery 与 Assistant 消息。"""
    conn, completed, turn_id = asyncio.run(_run_replay_scenario())
    assert completed, "replay run_once should complete"

    # 重放后：1 条 sent 流式 delivery（新）+ 1 条 interrupted（崩溃遗留）
    sent = conn.execute(
        "SELECT COUNT(*) AS c FROM deliveries WHERE status='sent'"
    ).fetchone()["c"]
    interrupted = conn.execute(
        "SELECT COUNT(*) AS c FROM deliveries WHERE status='interrupted'"
    ).fetchone()["c"]
    assert sent == 1, f"expected exactly one finalized delivery, got {sent}"
    assert interrupted == 1, f"crashed delivery should remain interrupted, got {interrupted}"

    # Turn 最终完成，且写入了 1 条 assistant 消息（崩溃那次未写入）
    t = conn.execute("SELECT status FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
    assert t["status"] == "completed", t
    msgs = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE role='assistant'"
    ).fetchone()["c"]
    assert msgs == 1, f"replay should persist exactly one assistant message, got {msgs}"
