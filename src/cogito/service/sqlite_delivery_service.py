"""SqliteDeliveryService — DeliveryService Protocol 的 SQLite 实现 (PLAN-10 M4)。

提供 enqueue / get / cancel / retry / reconcile 作为 Delivery 聚合的
唯一写入口 (SYSTEM-BOUNDARIES / 4)。

本实现沿用现有 DeliveryWorker 的 lease + deliver + receipt 可靠性语义；
新增状态机守护的 cancel / retry / reconcile 语义以及幂等键去重。

GatewayClient 抽象：service 层通过 GatewayClient Protocol 访问平台适配器；
LoopbackGatewayClient（合并进程）复用现有 ChannelManager + Adapter。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from cogito.contracts.clock import Clock, epoch_ms
from cogito.service.delivery_service import (
    DeliveryRef,
    DeliveryRequest,
    DeliveryService,
    DeliveryView,
    ReconcileResult,
)
from cogito.service.delivery_worker import (
    MAX_DELIVERY_TENTATIVE,
    DeliveryLease,
    DeliveryWorker,
)

# ── GatewayClient Protocol ─────────────────────────────────────────────


class GatewayResult:
    """平台发送结果。"""

    __slots__ = (
        "status", "platform_message_id", "error_code",
        "retry_after_seconds",
    )

    def __init__(
        self,
        status: str,
        *,
        platform_message_id: str | None = None,
        error_code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status = status
        self.platform_message_id = platform_message_id
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds


@runtime_checkable
class GatewayClient(Protocol):
    """投递通道抽象（service 层只经此访问平台）。

    部署形态：
      - LoopbackGatewayClient: 合并进程，复用 ChannelManager + Adapter
      - HttpGatewayClient: 分离 Gateway 进程，走 HTTP
    """

    def send(
        self, target_snapshot: str, content_ref: str, idempotency_key: str,
    ) -> GatewayResult:
        """发送一条消息。"""
        ...


def _now_ms(clock: Clock | None = None) -> int:
    c = clock
    if c is None:
        from cogito.contracts.clock import ProductionClock
        c = ProductionClock()
    return epoch_ms(c.now())


# ── SqliteDeliveryService ──────────────────────────────────────────────


class SqliteDeliveryService(DeliveryService):  # type: ignore[override]
    """DeliveryService Protocol 的唯一 SQLite 写实现。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        gateway: Any,
        *,
        clock: Clock | None = None,
        lease_ttl_s: int = 120,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._gateway = gateway
        # DeliveryWorker 只认 Gateway Protocol (send -> bool|None)；
        # 通过此 adapter 把 GatewayClient 的 bool|None 桥接回去
        self._worker = DeliveryWorker(
            conn=conn,
            gateway=_LegacyGateway(send_worker=self),
            lease_ttl_s=lease_ttl_s,
            clock=clock,
        )
        self._max_tentative = MAX_DELIVERY_TENTATIVE

    async def enqueue(self, request: DeliveryRequest) -> DeliveryRef:
        delivery_id = f"del-{uuid.uuid4().hex[:16]}"
        now = _now_ms(self._clock)
        now_iso = datetime.fromtimestamp(now / 1000, tz=UTC).isoformat()
        target_json = json.dumps(request.target)
        idem = request.idempotency_key or f"auto-{delivery_id}"

        existing = self._conn.execute(
            "SELECT delivery_id FROM deliveries WHERE idempotency_key=? AND status IN "
            "('pending','scheduled','sending','sent','unknown','retry_scheduled') "
            "ORDER BY created_at DESC LIMIT 1",
            (idem,),
        ).fetchone()
        if existing is not None:
            return DeliveryRef(delivery_id=existing["delivery_id"])

        initial_status = "scheduled" if request.scheduled_at else "pending"

        self._conn.execute(
            "INSERT INTO deliveries "
            "(delivery_id, target_snapshot, content_ref, status, idempotency_key, "
            "scheduled_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (delivery_id, target_json, request.content_ref, initial_status,
             idem, request.scheduled_at, now_iso),
        )
        self._conn.commit()
        return DeliveryRef(delivery_id=delivery_id)

    def get(self, delivery_id: str) -> DeliveryView | None:
        row = self._conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,),
        ).fetchone()
        if row is None:
            return None
        attempts = self._conn.execute(
            "SELECT * FROM delivery_attempts WHERE delivery_id=? ORDER BY attempt_no ASC",
            (delivery_id,),
        ).fetchall()
        receipts = self._conn.execute(
            "SELECT * FROM delivery_receipts WHERE delivery_id=? ORDER BY observed_at ASC",
            (delivery_id,),
        ).fetchall()
        return DeliveryView(
            delivery_id=row["delivery_id"],
            status=row["status"],
            target_snapshot=json.loads(row["target_snapshot"] or "{}"),
            content_ref=row["content_ref"],
            idempotency_key=row["idempotency_key"],
            attempt_count=row["attempt_count"],
            platform_message_id=row["platform_message_id"],
            attempts=[dict(a) for a in attempts],
            receipts=[dict(r) for r in receipts],
        )

    async def cancel(self, delivery_id: str, expected_version: int) -> None:
        with _uow(self._conn) as uow:
            row = self._conn.execute(
                "SELECT status FROM deliveries WHERE delivery_id=?", (delivery_id,),
            ).fetchone()
            if row is None or row["status"] in ("sent", "failed", "cancelled"):
                return
            self._conn.execute(
                "UPDATE deliveries SET status='cancelled', lease_version=? "
                "WHERE delivery_id=? AND status NOT IN ('sent','failed','cancelled')",
                (expected_version + 1, delivery_id),
            )
            uow.commit()

    async def retry(self, delivery_id: str, expected_version: int) -> None:
        now = _now_ms(self._clock)
        with _uow(self._conn) as uow:
            row = self._conn.execute(
                "SELECT status FROM deliveries WHERE delivery_id=?", (delivery_id,),
            ).fetchone()
            if row is None or row["status"] not in (
                "retry_scheduled", "failed", "unknown",
            ):
                return
            self._conn.execute(
                "UPDATE deliveries SET status='pending', next_attempt_at=?, "
                "lease_version=? "
                "WHERE delivery_id=? AND status=?",
                (now, expected_version + 1, delivery_id, row["status"]),
            )
            uow.commit()

    async def reconcile(
        self, delivery_id: str, platform_message_id: str | None = None,
    ) -> ReconcileResult:
        now = _now_ms(self._clock)
        with _uow(self._conn) as uow:
            row = self._conn.execute(
                "SELECT status, lease_version FROM deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return ReconcileResult(
                    delivery_id=delivery_id, status="still_unknown",
                )
            if row["status"] == "sent":
                return ReconcileResult(
                    delivery_id=delivery_id, status="sent",
                    platform_message_id=platform_message_id,
                )
            if row["status"] != "unknown":
                return ReconcileResult(
                    delivery_id=delivery_id, status="still_unknown",
                )

            updated = self._conn.execute(
                "UPDATE deliveries SET status='sent', platform_message_id=?, "
                "lease_version=? "
                "WHERE delivery_id=? AND status='unknown'",
                (platform_message_id, row["lease_version"] + 1, delivery_id),
            )
            if updated.rowcount > 0:
                attempt_row = self._conn.execute(
                    "SELECT attempt_id FROM delivery_attempts "
                    "WHERE delivery_id=? ORDER BY attempt_no DESC LIMIT 1",
                    (delivery_id,),
                ).fetchone()
                attempt_id = attempt_row["attempt_id"] if attempt_row else ""
                seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(operation_seq), 0) + 1 "
                    "FROM delivery_receipts WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                op_seq = int(seq_row[0] or 1)
                self._conn.execute(
                    "INSERT OR IGNORE INTO delivery_receipts "
                    "(receipt_id, delivery_id, delivery_attempt_id, operation_seq, "
                    "request_hash, receipt_kind, platform_message_id, safe_result, "
                    "observed_at, lease_version) "
                    "VALUES (?, ?, ?, ?, '', 'reconciled', ?, 'reconciled', ?, ?)",
                    (uuid.uuid4().hex, delivery_id, attempt_id, op_seq,
                     platform_message_id, now, row["lease_version"] + 1),
                )
            uow.commit()
            return ReconcileResult(
                delivery_id=delivery_id, status="sent",
                platform_message_id=platform_message_id,
            )

    # ── 投递执行（供 DeliveryWorker 编排调用）──────────────────────────

    def lease_next(self, worker_id: str) -> DeliveryLease | None:
        return self._worker.lease_next(worker_id)

    def deliver(self, lease: DeliveryLease, worker_id: str) -> str:
        return self._worker.deliver(lease, worker_id)

    def worker(self) -> DeliveryWorker:
        return self._worker


# ── 内部 helper ────────────────────────────────────────────────────────


def _uow(conn: sqlite3.Connection) -> Any:
    from cogito.service.unit_of_work import UnitOfWork
    return UnitOfWork(conn)


class _LegacyGateway:
    """把 bool|None () 形 GatewayClient 结果适配回 DeliveryWorker 期望的 bool|None。

    DeliveryWorker 按 send() 的 bool|None 决定 confirmed/temporary/unknown；
    此 adapter 把 GatewayClient 的 GatewayResult.status 映射回 bool|None:
      success                                            → True
      permanent / auth_error / route_expired /
      unsupported / too_large                            → False
      temporary / rate_limited / unknown                 → None
    """

    _PERMANENT_STATUSES = frozenset({
        "permanent", "auth_error", "route_expired", "unsupported", "too_large",
    })

    def __init__(self, send_worker: SqliteDeliveryService) -> None:
        self._svc = send_worker

    def send(self, target: str, content_ref: str) -> bool | None:
        try:
            client = self._svc._gateway
            idem = f"{target}:{content_ref}"
            if isinstance(client, GatewayClient) or hasattr(client, "send"):
                result = client.send(target, content_ref, idem)
                if isinstance(result, GatewayResult):
                    if result.status == "success":
                        return True
                    if result.status in self._PERMANENT_STATUSES:
                        return False
                    return None
                if isinstance(result, (tuple, list)) and result:
                    status = str(result[0])
                    if status == "success":
                        return True
                    if status in self._PERMANENT_STATUSES:
                        return False
                    return None
        except Exception:
            pass
        return None
