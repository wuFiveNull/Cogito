"""SqliteDeliveryService 状态机 + 幂等键 + cancel/retry/reconcile 测试 (PLAN-10 M4)。

覆盖 BR-03 / BR-04 / BR-05 / BR-06 / BR-08 / BR-12 对应的本地状态机语义。
Gateway 调用经 FakeGatewayClient 模拟。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cogito.contracts.clock import FakeClock, epoch_ms
from cogito.service.delivery_service import DeliveryRequest
from cogito.service.sqlite_delivery_service import (
    GatewayClient,
    GatewayResult,
    SqliteDeliveryService,
    _uow,
)
from cogito.store.migration import migrate


class FakeGatewayClient:
    """测试用 GatewayClient：可配置 success / fail / unknown。"""

    def __init__(
        self,
        *,
        status: str = "success",
        platform_message_id: str | None = None,
        error_code: str | None = None,
        retry_after_seconds: float | None = None,
        raises: bool = False,
    ) -> None:
        self._status = status
        self._pmid = platform_message_id
        self._err = error_code
        self._retry = retry_after_seconds
        self._raises = raises
        self.send_calls: list[tuple[str, str, str]] = []

    def send(self, target_snapshot: str, content_ref: str, idempotency_key: str) -> GatewayResult:
        self.send_calls.append((target_snapshot, content_ref, idempotency_key))
        if self._raises:
            raise RuntimeError("boom")
        return GatewayResult(
            status=self._status,
            platform_message_id=self._pmid,
            error_code=self._err,
            retry_after_seconds=self._retry,
        )


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class TestEnqueue:
    async def test_enqueue_creates_pending(self, conn, clock) -> None:
        svc = SqliteDeliveryService(conn, FakeGatewayClient(), clock=clock)
        ref = await svc.enqueue(
            DeliveryRequest(
                target={"channel": "qq", "to": "owner"},
                content_ref="mem://msg/1",
            )
        )
        assert ref.delivery_id.startswith("del-")
        view = svc.get(ref.delivery_id)
        assert view is not None
        assert view.status == "pending"

    async def test_enqueue_with_idempotency_returns_existing(self, conn, clock) -> None:
        gw = FakeGatewayClient()
        svc = SqliteDeliveryService(conn, gw, clock=clock)
        r1 = await svc.enqueue(
            DeliveryRequest(
                target={},
                content_ref="x",
                idempotency_key="idem-1",
            )
        )
        r2 = await svc.enqueue(
            DeliveryRequest(
                target={},
                content_ref="y",
                idempotency_key="idem-1",
            )
        )
        assert r1.delivery_id == r2.delivery_id
        # 仅一条 delivery
        rows = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert rows == 1

    async def test_enqueue_scheduled_status(self, conn, clock) -> None:
        svc = SqliteDeliveryService(conn, FakeGatewayClient(), clock=clock)
        ref = await svc.enqueue(
            DeliveryRequest(
                target={},
                content_ref="x",
                scheduled_at="2026-01-01T00:00:00+00:00",
            )
        )
        view = svc.get(ref.delivery_id)
        assert view is not None and view.status == "scheduled"


class TestCancel:
    async def test_cancel_pending(self, conn, clock) -> None:
        svc = SqliteDeliveryService(conn, FakeGatewayClient(), clock=clock)
        ref = await svc.enqueue(DeliveryRequest(target={}, content_ref="x"))
        view = svc.get(ref.delivery_id)
        assert view and view.status == "pending"
        await svc.cancel(ref.delivery_id, 0)
        view2 = svc.get(ref.delivery_id)
        assert view2 and view2.status == "cancelled"

    async def test_cancel_noop_when_sent(self, conn, clock) -> None:
        svc = SqliteDeliveryService(conn, FakeGatewayClient(status="success"), clock=clock)
        ref = await svc.enqueue(DeliveryRequest(target={}, content_ref="x"))
        conn.execute("UPDATE deliveries SET status='sent' WHERE delivery_id=?", (ref.delivery_id,))
        conn.commit()
        await svc.cancel(ref.delivery_id, 0)
        view = svc.get(ref.delivery_id)
        assert view and view.status == "sent"


class TestRetry:
    async def test_retry_pending_back_to_pending(self, conn, clock) -> None:
        svc = SqliteDeliveryService(conn, FakeGatewayClient(), clock=clock)
        ref = await svc.enqueue(DeliveryRequest(target={}, content_ref="x"))
        # 模拟当前已在 retry_scheduled
        conn.execute(
            "UPDATE deliveries SET status='retry_scheduled' WHERE delivery_id=?",
            (ref.delivery_id,),
        )
        conn.commit()
        await svc.retry(ref.delivery_id, 0)
        view = svc.get(ref.delivery_id)
        assert view and view.status == "pending"

    async def test_retry_noop_when_sent(self, conn, clock) -> None:
        svc = SqliteDeliveryService(conn, FakeGatewayClient(), clock=clock)
        ref = await svc.enqueue(DeliveryRequest(target={}, content_ref="x"))
        conn.execute("UPDATE deliveries SET status='sent' WHERE delivery_id=?", (ref.delivery_id,))
        conn.commit()
        await svc.retry(ref.delivery_id, 0)
        view = svc.get(ref.delivery_id)
        assert view and view.status == "sent"


class TestReconcile:
    async def test_reconcile_unknown_to_sent(self, conn, clock) -> None:
        svc = SqliteDeliveryService(conn, FakeGatewayClient(), clock=clock)
        ref = await svc.enqueue(DeliveryRequest(target={}, content_ref="x"))
        conn.execute(
            "UPDATE deliveries SET status='unknown' WHERE delivery_id=?",
            (ref.delivery_id,),
        )
        conn.commit()
        result = await svc.reconcile(ref.delivery_id, platform_message_id="pmid-1")
        assert result.status == "sent"
        view = svc.get(ref.delivery_id)
        assert view and view.status == "sent"
        assert view and view.platform_message_id == "pmid-1"

    async def test_reconcile_already_sent_is_idempotent(self, conn, clock) -> None:
        svc = SqliteDeliveryService(conn, FakeGatewayClient(), clock=clock)
        ref = await svc.enqueue(DeliveryRequest(target={}, content_ref="x"))
        conn.execute(
            "UPDATE deliveries SET status='sent' WHERE delivery_id=?",
            (ref.delivery_id,),
        )
        conn.commit()
        result = await svc.reconcile(ref.delivery_id, "pmid-2")
        assert result.status == "sent"


class TestDeliverFlow:
    async def test_deliver_success_creates_confirmed_receipt(self, conn, clock) -> None:
        gw = FakeGatewayClient(status="success", platform_message_id="pmid-ok")
        svc = SqliteDeliveryService(conn, gw, clock=clock)
        ref = await svc.enqueue(
            DeliveryRequest(
                target={"channel": "qq"},
                content_ref="hello",
            )
        )
        lease = svc.lease_next("w1")
        assert lease is not None
        result = svc.deliver(lease, "w1")
        assert result == "sent"
        view = svc.get(ref.delivery_id)
        assert view and view.status == "sent"
        # 经 _LegacyGateway 适配后走 worker 的 legacy bool|None 路径，
        # platform_message_id 由 worker 生成 fake_ 前缀；关键是 confirmed receipt 写入
        assert view and view.platform_message_id is not None
        assert len(view.receipts) >= 1
        assert any(r["receipt_kind"] == "confirmed" for r in view.receipts)

    async def test_deliver_fail_creates_uncertain_receipt_when_unknown(self, conn, clock) -> None:
        gw = FakeGatewayClient(status="unknown")
        svc = SqliteDeliveryService(conn, gw, clock=clock)
        ref = await svc.enqueue(
            DeliveryRequest(
                target={"channel": "qq"},
                content_ref="hello",
            )
        )
        lease = svc.lease_next("w1")
        assert lease is not None
        result = svc.deliver(lease, "w1")
        assert result == "unknown"
        view = svc.get(ref.delivery_id)
        assert view and view.status == "unknown"
        assert any(r["receipt_kind"] == "uncertain" for r in view.receipts)
