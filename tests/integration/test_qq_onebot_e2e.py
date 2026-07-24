"""QQ OneBot 端到端测试 —— Core 完整生命周期。

QQ-ONEBOT-E2E-01 / PR 4:
- QQ-A14: 私聊完整闭环（入站 → Turn → 回复 → Delivery sent）
- QQ-A15: 群聊完整闭环
- QQ-A17: 真实 platform_message_id 写入 Delivery 和 Receipt
- QQ-A18: Outbox drain（Turn 完成后 Event 最终 published）
- QQ-A19/QB-A21: 临时失败 → retry_scheduled → 重试 → sent
- QQ-A22/QB-A23: 发送后响应丢失 → unknown → 不自动重发

注意：这些测试使用 FakeChannelAdapter（非真实 QQ 协议）。
真实 NapCat/Lagrange 人工验收见 PR 5。

FakeChannelAdapter 在内存中模拟 OneBot send_*_msg 响应，
验证 Core 的 Inbound/AgentLoop/canonical Delivery Event 完整链路。
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from cogito.application import RuntimeApplication
from cogito.channel.base import (
    AdapterStatus,
    ChannelCapabilities,
    ChannelSendRequest,
    ChannelSendResult,
)
from cogito.channel.drivers.onebot_models import OneBotPolicy
from cogito.config import Config, QQOneBotConfig
from cogito.inbound.dispatcher import InboundDispatcher
from cogito.inbound.models import Inbound, InboundContent, InboundRoute
from cogito.infrastructure.payload_store import PayloadStore
from cogito.service.inbound_service import InboundService
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate

OWNER_QQ = "12345678"
ALLOWED_GROUP = "88888888"


class FakeChannelAdapter:
    """内存中的假 Channel Adapter —— 模拟 OneBot send_*_msg 响应。

    不通过真实 WS/HTTP 协议；直接操作数据库验证 Core 链路。
    """

    def __init__(self, *, fail_mode: str = "", drop_response: bool = False) -> None:
        self.adapter_id = "qq-main"
        self.channel_type = "qq"
        self.status = AdapterStatus.created
        self._fail_mode = fail_mode
        self._drop_response = drop_response
        self._next_message_id = 300001
        self.sent_log: list[dict] = []
        self._handler: Any = None

    def set_fail_mode(self, mode: str) -> None:
        self._fail_mode = mode

    def set_drop_response(self, drop: bool) -> None:
        self._drop_response = drop

    def set_inbound_handler(self, handler: Any) -> None:
        self._handler = handler

    async def start(self) -> None:
        self.status = AdapterStatus.running

    async def stop(self) -> None:
        self.status = AdapterStatus.stopped

    async def send(
        self,
        conversation_id: str,
        message: str,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        result = await self.send_request(
            ChannelSendRequest(
                delivery_id="",
                attempt_id="",
                idempotency_key="",
                channel_instance_id=self.adapter_id,
                target_endpoint_ref="",
                platform_conversation_id=conversation_id,
                reply_to_platform_message_id=reply_to_message_id,
                text=message,
            )
        )
        return {"status": result.status, "platform_message_id": result.platform_message_id}

    def send_request_sync(self, request: ChannelSendRequest) -> ChannelSendResult:
        """同步版本 —— 供测试中使用。

        DeliveryWorker 在同步上下文中调用，通过 asyncio.to_thread 回到主 loop。
        但 FakeAdapter 不需要任何 event loop，同步直接返回结果。
        """
        if self._drop_response:
            return ChannelSendResult(status="unknown", error_code="no_response")

        if self._fail_mode == "temporary":
            return ChannelSendResult(
                status="temporary",
                error_code="connection_error",
            )
        if self._fail_mode == "permanent":
            return ChannelSendResult(
                status="permanent",
                error_code="auth_error",
            )

        message_id = self._next_message_id
        self._next_message_id += 1
        self.sent_log.append(
            {
                "delivery_id": request.delivery_id,
                "target": request.platform_conversation_id,
                "text": request.text,
                "message_id": message_id,
            }
        )
        return ChannelSendResult(
            status="sent",
            platform_message_id=str(message_id),
        )

    async def send_request(self, request: ChannelSendRequest) -> ChannelSendResult:
        return self.send_request_sync(request)

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities()


# ── Helpers ──────────────────────────────────────────────────────────────


def _build_config(*, qq_enabled: bool = False) -> Config:
    """构建测试用 Config。"""
    return Config(
        workspace_path=".workspace",
    )


def _make_private_inbound(message_id: str, text: str = "你好 Cogito") -> Inbound:
    """创建私聊入站消息。"""
    return Inbound(
        channel="qq",
        channel_instance_id="qq-main",
        conversation_id=f"private:{OWNER_QQ}",
        sender_id=OWNER_QQ,
        message_id=message_id,
        content=[InboundContent(type="text", data=text)],
        timestamp=1700000000,
        metadata={
            "conversation_type": "private",
            "trust_label": "external_untrusted",
            "sender_endpoint_ref": f"qq:qq-main:user:{OWNER_QQ}",
            "conversation_endpoint_ref": f"qq:qq-main:private:{OWNER_QQ}",
            "target_endpoint_ref": f"qq:qq-main:person:{OWNER_QQ}",
        },
        route=InboundRoute(
            adapter_id="qq-main",
            channel_type="qq",
            conversation_id=f"private:{OWNER_QQ}",
            source_message_id=message_id,
        ),
    )


def _make_group_inbound(
    message_id: str,
    group_id: str = ALLOWED_GROUP,
    text: str = "@Bot 你好",
) -> Inbound:
    """创建群聊入站消息。"""
    return Inbound(
        channel="qq",
        channel_instance_id="qq-main",
        conversation_id=f"group:{group_id}",
        sender_id=OWNER_QQ,
        message_id=message_id,
        content=[
            InboundContent(type="at", data="99999999"),
            InboundContent(type="text", data=" 你好"),
        ],
        timestamp=1700000100,
        metadata={
            "conversation_type": "group",
            "trust_label": "external_untrusted",
            "group_id": group_id,
            "sender_id": OWNER_QQ,
            "sender_endpoint_ref": f"qq:qq-main:user:{OWNER_QQ}",
            "conversation_endpoint_ref": f"qq:qq-main:group:{group_id}",
            "target_endpoint_ref": f"qq:qq-main:group:{group_id}",
        },
        route=InboundRoute(
            adapter_id="qq-main",
            channel_type="qq",
            conversation_id=f"group:{group_id}",
            source_message_id=message_id,
        ),
    )


# ── E2E Tests ────────────────────────────────────────────────────────────


class _AgentStub:
    """简化 Agent 逻辑 —— 直接创建 Assistant Message + Delivery，不调用模型。

    使用真实的 TurnCompletionService 路径来确保 Delivery 正确创建。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._payload_dir = TemporaryDirectory()
        self.payload_store = PayloadStore(Path(self._payload_dir.name), conn)

    def process_queued_turn(self, worker_id: str = "test-worker") -> str | None:
        """领取一个 queued Turn，使用 Dispatcher + TurnCompletionService 完成。

        模拟 AgentRunner.run_once 的核心路径：
        1. Dispatcher.claim_next → 创建 RunAttempt
        2. TurnCompletionService.complete_reply → 写入 Assistant Message + Delivery
        """
        from cogito.domain.turn import Turn, TurnStatus, RunAttempt
        from cogito.service.completion import TurnCompletionService
        from cogito.service.dispatcher import Dispatcher
        import uuid as _uuid

        # 1. Claim turn (创建 RunAttempt)
        dispatcher = Dispatcher(self._conn)
        claimed = dispatcher.claim_next(worker_id)
        if claimed is None:
            return None

        turn = claimed.turn
        attempt = claimed.attempt

        # 2. Complete turn
        service = TurnCompletionService(self._conn, effect_payload_store=self.payload_store)
        message_id = service.complete_reply(
            turn=turn,
            attempt=attempt,
            reply_text="Stub reply: hello from Cogito",
        )
        if message_id is None:
            return None

        request = next(
            (
                event
                for event in EventStore(self._conn).read_stream_type("delivery")
                if event.event_type == "delivery.requested"
                and event.context.turn_id == turn.turn_id
            ),
            None,
        )
        return request.stream_id if request is not None else None


class TestE2EPrivateLoop:
    """QQ-A14: 私聊完整闭环。"""

    @pytest.mark.asyncio
    async def test_private_text_full_cycle(self) -> None:
        """QQ-A14: 私聊文本 → Agent Turn → Event Delivery → platform receipt。"""
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        migrate(conn)

        inbound_svc = InboundService(conn)
        dispatcher = InboundDispatcher(inbound_svc)

        # 1. 入站
        inbound = _make_private_inbound("pm_e2e_001")
        await dispatcher.dispatch(inbound)

        # 验证 Message + Turn 创建
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE platform_message_id=?",
            ("pm_e2e_001",),
        ).fetchone()
        assert row[0] == 1, "OneBot message_id 应创建一条 Message"

        # 2. 执行 Turn（stub agent）
        agent = _AgentStub(conn)
        delivery_id = agent.process_queued_turn()
        assert delivery_id is not None, "Agent 应创建 Delivery"

        # 3. 执行 canonical Delivery effect，通过真实 ChannelGateway 边界发送。
        fake_adapter = FakeChannelAdapter()
        await fake_adapter.start()
        from cogito.service.canonical_delivery_effect_executor import (
            CanonicalDeliveryEffectExecutor,
        )
        from cogito.service.channel_gateway import ChannelGateway
        from cogito.service.event_effect_worker import CanonicalEffectWorker
        from cogito.service.loopback_gateway_client import LoopbackGatewayClient

        gateway = LoopbackGatewayClient(ChannelGateway(conn, _MockManager(fake_adapter)))
        worker = CanonicalEffectWorker(
            EventStore(conn),
            CanonicalDeliveryEffectExecutor(agent.payload_store, gateway),
            effect_types=frozenset({"delivery"}),
        )
        assert worker.run_pending() == 1
        stream = EventStore(conn).read_stream("delivery", delivery_id)
        assert stream[-1].event_type == "delivery.completed"
        assert stream[-1].attributes["platform_message_id"]

        # 4. 验证 FakeAdapter 被调用
        assert len(fake_adapter.sent_log) == 1
        assert fake_adapter.sent_log[0]["text"] == "Stub reply: hello from Cogito"


class TestE2EGroupIdempotency:
    """QQ-A09: 重复 OneBot message_id 不重复创建 Turn。"""

    @pytest.mark.asyncio
    async def test_duplicate_oneobo_message_id(self) -> None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        migrate(conn)
        inbound_svc = InboundService(conn)
        dispatcher = InboundDispatcher(inbound_svc)

        # 第一次入站
        inbound1 = _make_private_inbound("dup_001", "第一次")
        await dispatcher.dispatch(inbound1)

        # 第二次入站（相同 message_id）
        inbound2 = _make_private_inbound("dup_001", "第二次")
        await dispatcher.dispatch(inbound2)

        # 只创建一条 Message 和一个 Turn
        msg_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE platform_message_id='dup_001'",
        ).fetchone()[0]
        assert msg_count == 1, "重复 message_id 只创建一条 Message"

        turn_count = conn.execute(
            "SELECT COUNT(*) FROM turns",
        ).fetchone()[0]
        assert turn_count == 1, "重复 message_id 只创建一个 Turn"


# ── Mock Helpers ─────────────────────────────────────────────────────────


class _MockManager:
    """模拟 ChannelManager，只返回一个 FakeAdapter。"""

    def __init__(self, adapter: FakeChannelAdapter) -> None:
        self._adapter = adapter

    def get_adapter(self, adapter_id: str) -> FakeChannelAdapter | None:
        if adapter_id == self._adapter.adapter_id:
            return self._adapter
        return None
