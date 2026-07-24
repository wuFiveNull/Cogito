"""Plan 05 M5 集成测试：崩溃恢复 + Web 订阅占位清理。

验证：
- WebChannelAdapter.subscribe 时清理本会话遗留的 interrupted 流式占位气泡，
  回灌 assistant.delete 事件（断线重连后浏览器删除 "…" 气泡）。
- 模拟流式过程崩溃（Turn 仍 running + 孤儿 streaming delivery），经 RecoveryService
  撤回并复位 Turn，再次 run_once 能重新流式并定稿（重放路径）。
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
from cogito.service.recovery_service import RecoveryService
from cogito.service.streaming_delivery_event_store import StreamingDeliveryEventStore
from cogito.domain.event import Event, EventClass, EventContext
from cogito.infrastructure.payload_store import PayloadStore
from cogito.store.event_replay import replay_delivery, replay_message, replay_turn
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate
from cogito.store.time_utils import now_ms


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def test_web_adapter_reconciles_cancelled_event_on_subscribe() -> None:
    """订阅时从已取消的流式 Event 清理遗留占位气泡。"""
    conn = _make_conn()
    conv_id = "web:reconcile"
    lifecycle = StreamingDeliveryEventStore(conn)
    lifecycle.create_streaming_delivery(
        delivery_id="d-orphan",
        attempt_id="a-orphan",
        target={"conversation_id": conv_id, "adapter_id": "web"},
        content_ref="",
        degradation_mode="edit_placeholder",
        idempotency_key="orphan",
        policy={},
        turn_id="t-orphan",
    )
    lifecycle.mark_placeholder("d-orphan", "a-orphan", "pm-orphan")
    lifecycle.withdraw("d-orphan", "a-orphan", "cancelled")
    conn.commit()

    adapter = WebChannelAdapter(adapter_id="web", channel_type="web", conn=conn)
    q = adapter.subscribe(conv_id)

    # 队列应回灌一条 delete 事件，指向遗留占位。
    assert not q.empty(), "expected a reconciled delete event in the queue"
    item = q.get_nowait()
    assert item.get("kind") == "delete"
    assert item.get("platform_message_id") == "pm-orphan"
    assert item.get("conversation_id") == conv_id

    # 其他会话不受影响
    other = adapter.subscribe("web:other")
    assert other.empty()


def test_event_streaming_recovery_cancels_and_reconciles_placeholder() -> None:
    """Event-only streaming state survives restart without Delivery projections."""
    conn = _make_conn()
    lifecycle = StreamingDeliveryEventStore(conn)
    lifecycle.create_streaming_delivery(
        delivery_id="d-event-orphan",
        attempt_id="a-event-orphan",
        target={"conversation_id": "web:event-reconcile", "adapter_id": "web"},
        content_ref="",
        degradation_mode="edit_placeholder",
        idempotency_key="event-orphan",
        policy={},
        turn_id="missing-turn",
        conversation_id="internal-conversation",
    )
    lifecycle.mark_placeholder("d-event-orphan", "a-event-orphan", "pm-event-orphan")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0

    assert RecoveryService(conn).recover_streaming_deliveries() == 1
    stream = EventStore(conn).read_stream("delivery", "d-event-orphan")
    state = replay_delivery(stream, "d-event-orphan")
    assert state is not None and state.status == "cancelled"

    queue = WebChannelAdapter(conn=conn).subscribe("web:event-reconcile")
    item = queue.get_nowait()
    assert item["kind"] == "delete"
    assert item["platform_message_id"] == "pm-event-orphan"


async def _run_replay_scenario() -> tuple[sqlite3.Connection, bool, str]:
    """模拟崩溃 → 恢复 → 重放，返回 (conn, 重放是否完成, conv_id)。"""
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
    result = inbound.accept(envelope)
    turn_id = result.turn_id
    assert turn_id, "inbound should produce a turn"

    # 模拟“首次流式尝试崩溃”：仅追加 running Turn/Attempt Event，
    # 并创建一个孤儿 streaming delivery（平台已发出占位“…”但未定稿）。
    now = now_ms()
    attempt_id = f"{turn_id}_crashed"
    events = EventStore(conn)
    turn_stream = events.read_stream("turn", turn_id)
    source = turn_stream[-1].context
    started = events.append(
        Event(
            event_type="runtime.turn.started",
            stream_type="turn",
            stream_id=turn_id,
            producer="test-crash-simulation",
            event_class=EventClass.OPERATION,
            context=EventContext(
                trace_id=source.trace_id,
                correlation_id=source.correlation_id,
                causation_id=turn_stream[-1].event_id,
                principal_id=source.principal_id,
                conversation_id=source.conversation_id,
                session_id=source.session_id,
                turn_id=turn_id,
                attempt_id=attempt_id,
            ),
            attributes={"active_attempt_id": attempt_id, "worker_id": "crashed-worker", "attempt_no": 1},
            outcome="running",
            occurred_at=now,
        ),
        expected_version=len(turn_stream),
    )
    events.append(
        Event(
            event_type="runtime.attempt.started",
            stream_type="run_attempt",
            stream_id=attempt_id,
            producer="test-crash-simulation",
            event_class=EventClass.OPERATION,
            context=EventContext(
                trace_id=source.trace_id,
                correlation_id=source.correlation_id,
                causation_id=started.event_id,
                principal_id=source.principal_id,
                conversation_id=source.conversation_id,
                session_id=source.session_id,
                turn_id=turn_id,
                attempt_id=attempt_id,
            ),
            attributes={
                "attempt_no": 1,
                "worker_id": "crashed-worker",
                "lease_version": 1,
                "lease_expires_at": now - 60_000,
            },
            outcome="running",
            occurred_at=now,
        ),
        expected_version=0,
    )
    lifecycle = StreamingDeliveryEventStore(conn)
    lifecycle.create_streaming_delivery(
        delivery_id="d-crashed",
        attempt_id=attempt_id,
        target={"delivery_id": "d-crashed", "conversation_id": conv_id, "adapter_id": "web"},
        content_ref="",
        degradation_mode="edit_placeholder",
        idempotency_key="crashed-stream",
        policy={},
        turn_id=turn_id,
    )
    lifecycle.mark_placeholder("d-crashed", attempt_id, "pm-crashed")
    conn.commit()

    # 启动恢复：撤回孤儿 delivery 并复位 Turn 为 queued
    recovery = RecoveryService(conn)
    recovery.recover_all()
    d = replay_delivery(EventStore(conn).read_stream("delivery", "d-crashed"), "d-crashed")
    assert d is not None and d.status == "cancelled"
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    recovered_turn = replay_turn(EventStore(conn).read_stream("turn", turn_id), turn_id)
    assert recovered_turn is not None and recovered_turn.status == "queued", recovered_turn

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

    # 重放后：新流式 Delivery 为 sent；崩溃前的 Event 已被 cancelled。
    streams: dict[str, list] = {}
    for event in EventStore(conn).read_stream_type("delivery"):
        streams.setdefault(event.stream_id, []).append(event)
    sent = sum(
        replay_delivery(stream, delivery_id).status == "sent"
        for delivery_id, stream in streams.items()
        if replay_delivery(stream, delivery_id) is not None
        and stream[0].attributes.get("delivery_mode") == "streaming"
    )
    cancelled = sum(
        replay_delivery(stream, delivery_id).status == "cancelled"
        for delivery_id, stream in streams.items()
        if replay_delivery(stream, delivery_id) is not None
        and stream[0].attributes.get("delivery_mode") == "streaming"
    )
    assert sent == 1, f"expected exactly one finalized delivery event, got {sent}"
    assert cancelled == 1, f"crashed delivery should be cancelled, got {cancelled}"

    # Turn 最终完成，且写入了 1 条 assistant Message Event（崩溃那次未写入）。
    turn = replay_turn(EventStore(conn).read_stream("turn", turn_id), turn_id)
    assert turn is not None and turn.status == "completed", turn
    message_events = EventStore(conn).read_stream_type("message")
    assistants = [
        state
        for message_id in {event.stream_id for event in message_events}
        if (state := replay_message(message_events, message_id)) is not None and state.role == "assistant"
    ]
    assert len(assistants) == 1, f"replay should persist exactly one assistant message, got {len(assistants)}"
