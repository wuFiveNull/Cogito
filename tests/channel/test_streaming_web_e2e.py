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
from types import SimpleNamespace

from cogito.channel.drivers.web import WebChannelAdapter
from cogito.channel.manager import ChannelManager
from cogito.config import Config
from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
from cogito.inbound.dispatcher import InboundDispatcher
from cogito.model.stub_provider import StubModelProvider
from cogito.service.agent_runner import RunOutcome, build_agent_runner
from cogito.service.channel_gateway import ChannelGateway
from cogito.service.inbound_service import InboundService
from cogito.service.streaming_delivery import (
    StreamInputMeta,
    StreamingDeliveryController,
)
from cogito.infrastructure.payload_store import PayloadStore
from cogito.store.event_replay import replay_delivery, replay_message, replay_turn
from cogito.store.event_store import EventStore
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
    inbound = InboundService(
        conn,
        payload_store=PayloadStore(config.resolve_payload_dir(), conn),
    )

    adapter = WebChannelAdapter(adapter_id="web", channel_type="web")
    manager = ChannelManager(InboundDispatcher(inbound))
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

    # Event replay：Turn 完成 + 存在 assistant Message Event。
    turn_events = EventStore(conn).read_stream_type("turn")
    completed_turns = [
        state
        for turn_id in {event.stream_id for event in turn_events}
        if (state := replay_turn(turn_events, turn_id)) is not None and state.status == "completed"
    ]
    assert completed_turns
    message_events = EventStore(conn).read_stream_type("message")
    assistants = [
        state
        for message_id in {event.stream_id for event in message_events}
        if (state := replay_message(message_events, message_id)) is not None and state.role == "assistant"
    ]
    assert assistants, "assistant message should be persisted as an Event"

    streams: dict[str, list] = {}
    for event in EventStore(conn).read_stream_type("delivery"):
        streams.setdefault(event.stream_id, []).append(event)
    stream = next(
        stream
        for stream in streams.values()
        if stream[0].attributes.get("delivery_mode") == "streaming"
    )
    assert [event.event_type for event in stream] == [
        "delivery.requested",
        "delivery.started",
        "delivery.completed",
    ]
    state = replay_delivery(stream, stream[0].stream_id)
    assert state is not None and state.status == "sent"
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


def test_streaming_delivery_commits_before_model_call() -> None:
    """创建 provisional Delivery 后，模型调用前主连接不能保留写事务。"""
    conn = _make_conn()
    seen_transactions: list[bool] = []

    class _CheckingLoop:
        async def run_stream(self, *_args, **_kwargs):
            seen_transactions.append(conn.in_transaction)
            raise RuntimeError("stop after transaction check")
            yield "", True  # pragma: no cover - make this an async generator

    class _Gateway:
        def send_text(self, *_args, **_kwargs):
            return SimpleNamespace(status="sent")

    controller = StreamingDeliveryController(
        conn=conn,
        gateway=_Gateway(),
        loop=_CheckingLoop(),
        capabilities=SimpleNamespace(supports_edit=True),
    )
    input_meta = StreamInputMeta(
        conversation_id="web:transaction-check",
        session_id="",
        endpoint_id="",
        principal_id="",
        reply_route={},
        capability_snapshot={},
        input_message_id="",
    )

    result = asyncio.run(
        controller.run_streaming_turn(
            turn=SimpleNamespace(turn_id=""),
            attempt=SimpleNamespace(),
            context=SimpleNamespace(),
            input_meta=input_meta,
        )
    )

    assert result is None
    assert seen_transactions == [False]
