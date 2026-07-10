"""Web Channel 测试 —— 验证 Web 作为真正 Channel 接入 Core 主链路。

覆盖 plan/04 的验收点：
- WebChannelAdapter 单元：subscribe / send_request_sync / 信箱回灌 / unsubscribe
- 入站：InboundService.accept(web envelope) 创建 message + turn，reply_route 携带 adapter_id=web
- 出站：Agent 回复经 ChannelGateway.send_request 路由回 WebChannelAdapter 队列（与 QQ/Terminal 对称）
- chat._build_web_envelope 构造正确的 web 信封
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.channel.base import ChannelSendRequest
from cogito.channel.drivers.web import WebChannelAdapter
from cogito.channel.manager import ChannelManager
from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
from cogito.service.channel_gateway import ChannelGateway
from cogito.service.inbound_service import InboundService
from cogito.store.migration import migrate


CONV = "web:test-conv-1"


def _make_web_envelope(text: str, conversation_id: str = CONV) -> ChannelEnvelope:
    return ChannelEnvelope(
        channel_type="web",
        channel_instance_id="web",
        platform_sender_id="web-user",
        platform_conversation_id=conversation_id,
        content_parts=[{"content_type": "text", "inline_data": text}],
        reply_route=ReplyRoute(
            channel_instance_id="web",
            platform_conversation_id=conversation_id,
        ),
        received_at=datetime.now(UTC).isoformat(),
        trust_label="authenticated",
    )


@pytest.fixture
def in_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


# =============================================================================
# WebChannelAdapter 单元测试
# =============================================================================


class TestWebChannelAdapter:
    def test_adapter_identity(self):
        a = WebChannelAdapter()
        assert a.adapter_id == "web"
        assert a.channel_type == "web"
        assert a.status.value == "created"

    @pytest.mark.asyncio
    async def test_subscribe_receives_reply(self):
        """主循环内订阅后，出站回复经队列实时送达。"""
        a = WebChannelAdapter()
        await a.start()  # 绑定当前运行 loop
        q = a.subscribe(CONV)

        req = ChannelSendRequest(
            delivery_id="d1",
            attempt_id="a1",
            idempotency_key="delivery_d1",
            channel_instance_id="web",
            target_endpoint_ref="",
            platform_conversation_id=CONV,
            reply_to_platform_message_id=None,
            text="Hello from Agent",
        )
        result = a.send_request_sync(req)
        assert result.status == "sent"

        # 让主循环执行 _pump（把跨线程缓冲搬到 asyncio 队列）
        await asyncio.sleep(0)
        item = await asyncio.wait_for(q.get(), timeout=2)
        assert item["text"] == "Hello from Agent"
        assert item["conversation_id"] == CONV
        assert item["delivery_id"] == "d1"

    @pytest.mark.asyncio
    async def test_mailbox_buffers_when_offline_then_replays(self):
        """未订阅（离线）时的回复进信箱，订阅后回灌。"""
        a = WebChannelAdapter()
        # 未 start：loop 为 None，send_request_sync → 信箱
        req = ChannelSendRequest(
            delivery_id="d2",
            attempt_id="a2",
            idempotency_key="delivery_d2",
            channel_instance_id="web",
            target_endpoint_ref="",
            platform_conversation_id=CONV,
            reply_to_platform_message_id=None,
            text="Offline reply",
        )
        res = a.send_request_sync(req)
        assert res.status == "sent"

        # 现在启动并订阅 → 应回灌离线期间的信箱消息
        await a.start()
        q = a.subscribe(CONV)
        # mailbox 回灌是同步 put_nowait，无需等 loop
        item = await asyncio.wait_for(q.get(), timeout=2)
        assert item["text"] == "Offline reply"

    @pytest.mark.asyncio
    async def test_unsubscribe_drops_subscription(self):
        """取消订阅后，出站回复落入信箱而非已失效队列。"""
        a = WebChannelAdapter()
        await a.start()
        q = a.subscribe(CONV)
        a.unsubscribe(CONV)

        req = ChannelSendRequest(
            delivery_id="d3",
            attempt_id="a3",
            idempotency_key="delivery_d3",
            channel_instance_id="web",
            target_endpoint_ref="",
            platform_conversation_id=CONV,
            reply_to_platform_message_id=None,
            text="After unsub",
        )
        a.send_request_sync(req)
        await asyncio.sleep(0)

        # 原队列应已空（消息去了信箱）
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.3)

        # 重新订阅回灌信箱
        q2 = a.subscribe(CONV)
        item = await asyncio.wait_for(q2.get(), timeout=2)
        assert item["text"] == "After unsub"


# =============================================================================
# 入站闭环：Web envelope → InboundService
# =============================================================================


class TestWebInbound:
    def test_accept_creates_message_and_turn(self, in_memory_db):
        svc = InboundService(in_memory_db)
        result = svc.accept(_make_web_envelope("hi agent"))

        assert result.message_id
        assert result.turn_id
        assert result.is_new is True

        # reply_route 已持久化，且携带 adapter_id=web
        row = in_memory_db.execute(
            "SELECT reply_route_json FROM messages WHERE message_id=?",
            (result.message_id,),
        ).fetchone()
        assert row is not None
        rr = json.loads(row["reply_route_json"])
        assert rr.get("channel_instance_id") == "web"

    def test_build_web_envelope_helper(self):
        """chat._build_web_envelope 产出与适配器对称的信封。"""
        from cogito.interaction_web.chat import _build_web_envelope

        env = _build_web_envelope("ping", CONV, "web-user")
        assert env.channel_type == "web"
        assert env.channel_instance_id == "web"
        assert env.reply_route is not None
        assert env.reply_route.channel_instance_id == "web"
        assert env.reply_route.platform_conversation_id == CONV
        assert env.content_parts[0]["inline_data"] == "ping"


# =============================================================================
# 出站闭环：ChannelGateway → WebChannelAdapter 队列
# =============================================================================


class TestWebDeliveryRouting:
    def test_gateway_routes_reply_to_web_adapter(self, in_memory_db):
        """Agent 回复经 ChannelGateway 路由回 WebChannelAdapter（与 QQ 对称）。"""
        adapter = WebChannelAdapter()
        manager = ChannelManager(None)
        manager._adapters["web"] = adapter  # 直接注册，避免启动真实 dispatcher
        gateway = ChannelGateway(in_memory_db, manager)

        # 聚焦路由：stub 掉从 DB 读内容（避免依赖 messages/content_parts 外键行）
        class _StubContent:
            def __init__(self, text=""):
                self.text = text
                self.attachments = ()
        gateway._read_message_content = lambda ref: _StubContent("Hi, I am Cogito.")
        msg_id = "assistant-msg-1"

        target_snapshot = json.dumps({
            "reply_route": {
                "channel_instance_id": "web",
                "platform_conversation_id": CONV,
            },
            "delivery_id": "d-deliver-1",
            "adapter_id": "web",
            "idempotency_key": "delivery_x",
        })

        result = gateway.send_request(target_snapshot, msg_id)
        assert result.status == "sent"

        # 出站回复进入信箱（adapter 未 start，无主循环）
        assert adapter._mailbox.get(CONV), "reply should be buffered in mailbox"
        assert adapter._mailbox[CONV][0]["text"] == "Hi, I am Cogito."

    def test_gateway_missing_adapter_is_temporary(self, in_memory_db):
        """adapter 未注册时返回 temporary（可重试）。"""
        manager = ChannelManager(None)
        gateway = ChannelGateway(in_memory_db, manager)
        target_snapshot = json.dumps({
            "reply_route": {"channel_instance_id": "web", "platform_conversation_id": CONV},
            "adapter_id": "web",
        })
        result = gateway.send_request(target_snapshot, "any-msg")
        assert result.status == "temporary"
        assert result.error_code == "adapter_not_running"


# =============================================================================
# interaction-web 聊天层（HTTP + WS）集成测试
# =============================================================================


def _build_chat_test_app(adapter: WebChannelAdapter, inbound_accept):
    """构造一个最小 FastAPI app：挂载 chat.router，注入 fake runtime。

    inbound_accept(envelope) 接受信封并模拟 Agent 完成（把回复塞进 web adapter 队列）。
    """
    from types import SimpleNamespace

    from fastapi import FastAPI

    from cogito.interaction_web.chat import router

    class _Inbound:
        def __init__(self):
            self.calls = []

        def accept(self, envelope):
            self.calls.append(envelope)
            # 仅记录进入主链路；Agent 完成后的回复由测试线程经 adapter.send_request_sync 推回
            return SimpleNamespace(message_id="m1", turn_id="t1", is_new=True)

    class _Runtime:
        inbound = _Inbound()
        web_channel_adapter = adapter

    app = FastAPI()
    app.state.runtime = _Runtime()
    app.include_router(router)
    return app


def test_http_chat_send_enters_main_link():
    """POST /api/chat/send 构造 web 信封并提交给 InboundService（主链路入口）。"""
    from fastapi.testclient import TestClient

    adapter = WebChannelAdapter()
    app = _build_chat_test_app(adapter, None)
    with TestClient(app) as client:
        resp = client.post("/api/chat/send", json={"text": "hello cogito"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["conversation_id"].startswith("web:")
        assert body["message_id"] == "m1"
        assert body["turn_id"] == "t1"
    # 信封确实以 web channel 进入主链路
    assert app.state.runtime.inbound.calls[0].channel_type == "web"
    assert app.state.runtime.inbound.calls[0].channel_instance_id == "web"


def test_ws_chat_roundtrip_pushes_reply():
    """WS /api/chat/ws：浏览器发消息 → 进主链路；离线回复经信箱回灌并由 WS 推回。"""
    from fastapi.testclient import TestClient

    adapter = WebChannelAdapter()
    app = _build_chat_test_app(adapter, None)
    CID = "web:ws-roundtrip-1"

    # 离线期间（未订阅）的回复进信箱
    adapter.send_request_sync(
        ChannelSendRequest(
            delivery_id="d-sim", attempt_id="a-sim", idempotency_key="sim",
            channel_instance_id="web", target_endpoint_ref="",
            platform_conversation_id=CID, text="echo: offline",
            reply_to_platform_message_id=None,
        )
    )

    with TestClient(app) as client:
        with client.websocket_connect("/api/chat/ws") as ws:
            # 协议：浏览器先发 init 帧，handler 收到后回 ready
            ws.send_json({"conversation_id": CID})
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["conversation_id"] == CID

            # 订阅时回灌信箱 → assistant 回复经 WS 推回
            reply = ws.receive_json()
            assert reply["type"] == "assistant"
            assert reply["text"] == "echo: offline"
            assert reply["conversation_id"] == CID

            # 浏览器发消息 → 进主链路
            ws.send_json({"text": "ping"})

    # WS 收到的消息确实进了主链路（web 信封）
    assert len(app.state.runtime.inbound.calls) == 1
    assert app.state.runtime.inbound.calls[0].content_parts[0]["inline_data"] == "ping"


def test_ws_chat_unknown_conversation_receives_reply_via_mailbox():
    """断线后代理把回复缓冲进信箱，下次订阅同一 conversation 时回灌。"""
    adapter = WebChannelAdapter()
    # 无订阅者时回复进信箱
    adapter.send_request_sync(
        ChannelSendRequest(
            delivery_id="d-mb", attempt_id="a-mb", idempotency_key="mb",
            channel_instance_id="web", target_endpoint_ref="",
            platform_conversation_id=CONV, text="offline reply",
            reply_to_platform_message_id=None,
        )
    )
    assert adapter._mailbox[CONV][0]["text"] == "offline reply"

    async def _subscribe_and_drain():
        q = adapter.subscribe(CONV)
        return q.get_nowait()

    item = asyncio.run(_subscribe_and_drain())
    assert item["text"] == "offline reply"
    adapter.unsubscribe(CONV)
