"""Plan 05 M4 集成测试：AgentRunner 按渠道能力走流式投递分支，并经 WebChannelAdapter 实时推流。

验证：
- web 渠道（capabilities.supports_edit + supports_streaming）下，run_once 走
  StreamingDeliveryController，发出 placeholder(send) → 多个 edit → 最终 is_final edit。
- Turn 在流式定稿后进入 completed，且 Assistant Message 已写入 DB。
- 队列事件携带一致的 platform_message_id，便于前端按 message_id 增量渲染。
"""

from __future__ import annotations

import asyncio
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
from cogito.store.migration import migrate


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


async def _run_streaming_scenario() -> tuple[sqlite3.Connection, list[dict], bool, str]:
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

    conv_id = "web:stream-test"
    envelope = ChannelEnvelope(
        channel_type="web",
        channel_instance_id="web",
        platform_sender_id="tester",
        platform_conversation_id=conv_id,
        content_parts=[{"content_type": "text", "inline_data": "你好，介绍一下你自己"}],
        reply_route=ReplyRoute(channel_instance_id="web", platform_conversation_id=conv_id),
        trust_label="authenticated",
    )
    inbound = InboundService(conn)
    result = inbound.accept(envelope)
    assert result.turn_id, "inbound should produce a turn"

    queue = adapter.subscribe(conv_id)
    outcome = await runner.run_once("test-worker")
    assert outcome == RunOutcome.completed, f"unexpected outcome: {outcome}"

    # 收集队列事件，直到出现最终定稿 edit
    items: list[dict] = []
    saw_final = False
    msg_id: str | None = None
    for _ in range(60):
        try:
            item = await asyncio.wait_for(queue.get(), timeout=3.0)
        except TimeoutError:
            break
        items.append(item)
        if item.get("kind") == "send":
            msg_id = item.get("platform_message_id")
        if item.get("kind") == "edit" and item.get("is_final"):
            saw_final = True
            break

    return conn, items, saw_final, conv_id, msg_id


def test_agent_runner_streams_to_web_adapter() -> None:
    conn, items, saw_final, conv_id, msg_id = asyncio.run(_run_streaming_scenario())

    kinds = [it.get("kind") for it in items]
    assert "send" in kinds, f"expected placeholder 'send', got {kinds}"
    assert saw_final, f"expected final 'edit' with is_final, got {kinds}"

    # 所有 edit 与 send 共享同一 platform_message_id（前端按 id 增量渲染）
    assert msg_id, "placeholder should carry a platform_message_id"
    for it in items:
        if it.get("kind") in ("send", "edit"):
            assert it.get("platform_message_id") == msg_id, f"platform_message_id mismatch: {it}"

    # DB：Turn 完成 + 存在 assistant 消息（内部 conversation_id 为 UUID，按 role 统计）
    turn = conn.execute("SELECT status FROM turns").fetchone()
    assert turn["status"] == "completed"
    msg = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE role='assistant'").fetchone()
    assert msg["c"] >= 1, "assistant message should be persisted"

    delivery = conn.execute("SELECT content_mode, stream_status, status FROM deliveries").fetchone()
    assert delivery is not None, "streaming delivery should be persisted"
    assert delivery["content_mode"] == "final", delivery
    assert delivery["stream_status"] == "done", delivery
    assert delivery["status"] == "sent", delivery
