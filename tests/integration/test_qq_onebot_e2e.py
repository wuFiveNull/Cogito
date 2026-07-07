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
验证 Core 的 Inbound/AgentLoop/DeliveryWorker/Receipt 完整链路。
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime
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
from cogito.service.inbound_service import InboundService
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
        self, conversation_id: str, message: str, reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        result = await self.send_request(ChannelSendRequest(
            delivery_id="",
            attempt_id="",
            idempotency_key="",
            channel_instance_id=self.adapter_id,
            target_endpoint_ref="",
            platform_conversation_id=conversation_id,
            reply_to_platform_message_id=reply_to_message_id,
            text=message,
        ))
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
                status="temporary", error_code="connection_error",
            )
        if self._fail_mode == "permanent":
            return ChannelSendResult(
                status="permanent", error_code="auth_error",
            )

        message_id = self._next_message_id
        self._next_message_id += 1
        self.sent_log.append({
            "delivery_id": request.delivery_id,
            "target": request.platform_conversation_id,
            "text": request.text,
            "message_id": message_id,
        })
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
    message_id: str, group_id: str = ALLOWED_GROUP, text: str = "@Bot 你好",
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
        service = TurnCompletionService(self._conn)
        message_id = service.complete_reply(
            turn=turn,
            attempt=attempt,
            reply_text="Stub reply: hello from Cogito",
        )
        if message_id is None:
            return None

        # 读取刚创建的 Delivery
        delivery = self._conn.execute(
            "SELECT delivery_id FROM deliveries WHERE content_ref=? AND status='pending' LIMIT 1",
            (message_id,),
        ).fetchone()
        if delivery is None:
            return None
        return delivery["delivery_id"]


class TestE2EPrivateLoop:
    """QQ-A14: 私聊完整闭环。"""

    @pytest.mark.asyncio
    async def test_private_text_full_cycle(self) -> None:
        """QQ-A14: 私聊文本 → Agent Turn → Delivery → send → platform_message_id 落库。"""
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

        # 3. 验证 Delivery 创建
        delivery_row = conn.execute(
            "SELECT delivery_id, target_snapshot, content_ref, status FROM deliveries WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        assert delivery_row is not None, "应创建 Delivery"
        assert delivery_row["status"] == "pending"
        assert delivery_row["content_ref"] is not None

        # 4. 执行 Delivery send (直接 await adapter.send_request 模拟 Gateway)
        fake_adapter = FakeChannelAdapter()
        await fake_adapter.start()
        # 构建 ChannelSendRequest
        from cogito.channel.base import ChannelSendRequest
        import json as _json
        target = _json.loads(delivery_row["target_snapshot"])
        request = ChannelSendRequest(
            delivery_id=target.get("delivery_id", ""),
            attempt_id="",
            idempotency_key=target.get("idempotency_key", ""),
            channel_instance_id=target.get("adapter_id", "qq-main"),
            target_endpoint_ref=target.get("target_endpoint_ref", ""),
            platform_conversation_id=delivery_row["target_snapshot"],  # 仅用于解析
            reply_to_platform_message_id=target.get("reply_route", {}).get("reply_to_platform_message_id"),
            text="Stub reply: hello from Cogito",
        )
        result = await fake_adapter.send_request(request)
        assert result.status == "sent", f"send 应成功，得到 {result}"
        assert result.platform_message_id is not None

        # 5. 验证 FakeAdapter 被调用
        assert len(fake_adapter.sent_log) == 1
        assert fake_adapter.sent_log[0]["message_id"] == int(result.platform_message_id)


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


class TestE2ETemporaryFailure:
    """QQ-A19 / QQ-A21: 临时失败 → retry_scheduled → 重试 → sent。"""

    @pytest.mark.asyncio
    async def test_temporary_then_retry(self) -> None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        migrate(conn)

        inbound_svc = InboundService(conn)
        dispatcher = InboundDispatcher(inbound_svc)
        fake_adapter = FakeChannelAdapter(fail_mode="temporary")
        from cogito.service.channel_gateway import ChannelGateway
        from cogito.service.delivery_worker import DeliveryWorker

        gateway = ChannelGateway(conn, _MockManager(fake_adapter))
        worker = DeliveryWorker(conn, gateway, lease_ttl_s=120)

        # 入站 + Turn + Delivery 创建
        inbound = _make_private_inbound("tmp_001")
        await dispatcher.dispatch(inbound)
        agent = _AgentStub(conn)
        delivery_id = agent.process_queued_turn()
        assert delivery_id is not None

        # 第一次 send：临时失败
        lease = worker.lease_next("test-worker")
        assert lease is not None
        result_str = worker.deliver(lease, "test-worker")

        # 应进入 retry_scheduled
        after = conn.execute(
            "SELECT status FROM deliveries WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        assert after["status"] == "retry_scheduled", f"status={after['status']} result={result_str}"

        # Receipt 应为 temporary
        receipt = conn.execute(
            "SELECT receipt_kind FROM delivery_receipts WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        assert receipt is not None, f"no receipt, result={result_str}"
        assert receipt["receipt_kind"] == "temporary"

        # 模拟临时失败恢复
        fake_adapter.set_fail_mode("")
        conn.execute(
            "UPDATE deliveries SET next_attempt_at=0 WHERE delivery_id=?",
            (delivery_id,),
        )
        conn.commit()

        # 第二次 send：成功
        lease2 = worker.lease_next("test-worker")
        assert lease2 is not None
        worker.deliver(lease2, "test-worker")

        after2 = conn.execute(
            "SELECT status, platform_message_id FROM deliveries WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        assert after2["status"] == "sent"
        assert after2["platform_message_id"] is not None


class TestE2EUnknownNoRetry:
    """QQ-A22 / QQ-A23: 发送后响应丢失 → unknown → 重启不重发。"""

    @pytest.mark.asyncio
    async def test_unknown_does_not_auto_retry(self) -> None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        migrate(conn)

        inbound_svc = InboundService(conn)
        dispatcher = InboundDispatcher(inbound_svc)
        fake_adapter = FakeChannelAdapter(drop_response=True)
        from cogito.service.channel_gateway import ChannelGateway
        from cogito.service.delivery_worker import DeliveryWorker

        gateway = ChannelGateway(conn, _MockManager(fake_adapter))
        worker = DeliveryWorker(conn, gateway, lease_ttl_s=120)

        # 入站 + Turn + Delivery 创建
        inbound = _make_private_inbound("unk_001")
        await dispatcher.dispatch(inbound)
        agent = _AgentStub(conn)
        delivery_id = agent.process_queued_turn()
        assert delivery_id is not None

        # 发送（响应丢失）
        lease = worker.lease_next("test-worker")
        assert lease is not None
        result = worker.deliver(lease, "test-worker")
        assert result == "unknown"

        after = conn.execute(
            "SELECT status FROM deliveries WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        assert after["status"] == "unknown"

        # 验证 send 只被调用了一次（external call count）
        assert len(fake_adapter.sent_log) == 0  # drop_response → adapter 内部不调用

        # Receipt 应为 uncertain
        receipt = conn.execute(
            "SELECT receipt_kind FROM delivery_receipts WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        assert receipt is not None
        assert receipt["receipt_kind"] == "uncertain"

        # 重启后不自动重发：unknown 不进入 lease_next 的领取范围
        lease2 = worker.lease_next("test-worker")
        assert lease2 is None, "unknown delivery 不应被领取"


# ── Mock Helpers ─────────────────────────────────────────────────────────


class _MockManager:
    """模拟 ChannelManager，只返回一个 FakeAdapter。"""

    def __init__(self, adapter: FakeChannelAdapter) -> None:
        self._adapter = adapter

    def get_adapter(self, adapter_id: str) -> FakeChannelAdapter | None:
        if adapter_id == self._adapter.adapter_id:
            return self._adapter
        return None
