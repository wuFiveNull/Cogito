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
from typing import Any

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
from cogito.service.gateway_client import GatewayClient, GatewayResult


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
        gateway: Any | None = None,
        *,
        clock: Clock | None = None,
        lease_ttl_s: int = 120,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._gateway = gateway or _UnavailableGateway()
        # DeliveryWorker consumes ChannelSendResult.  This adapter resolves the
        # Core content_ref before crossing the Gateway boundary.
        self._worker = DeliveryWorker(
            conn=conn,
            gateway=_DeliveryGatewayAdapter(service=self),
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
            if row is None or row["status"] != "retry_scheduled":
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
        initial = self._conn.execute(
            "SELECT status, target_snapshot, idempotency_key FROM deliveries "
            "WHERE delivery_id=?", (delivery_id,),
        ).fetchone()
        if initial is None:
            return ReconcileResult(delivery_id=delivery_id, status="still_unknown")
        if initial["status"] == "sent":
            return ReconcileResult(
                delivery_id=delivery_id, status="sent",
                platform_message_id=platform_message_id,
            )
        if initial["status"] != "unknown":
            return ReconcileResult(delivery_id=delivery_id, status="still_unknown")

        # External lookup is deliberately outside the database transaction.
        gateway_result = None
        if hasattr(self._gateway, "reconcile"):
            try:
                gateway_result = self._gateway.reconcile(
                    initial["target_snapshot"], platform_message_id,
                    initial["idempotency_key"] or f"reconcile:{delivery_id}",
                )
            except Exception:
                gateway_result = GatewayResult(status="unknown", error_code="gateway_exception")
        if gateway_result is not None:
            if gateway_result.status not in ("success", "sent"):
                return ReconcileResult(
                    delivery_id=delivery_id,
                    status="failed" if gateway_result.status == "permanent" else "still_unknown",
                    platform_message_id=gateway_result.platform_message_id,
                )
            platform_message_id = gateway_result.platform_message_id or platform_message_id
        elif not platform_message_id:
            return ReconcileResult(delivery_id=delivery_id, status="still_unknown")

        with _uow(self._conn) as uow:
            row = self._conn.execute(
                "SELECT status, lease_version FROM deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return ReconcileResult(
                    delivery_id=delivery_id, status="still_unknown",
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


class _DeliveryGatewayAdapter:
    """Adapt GatewayClient results to DeliveryWorker's structured contract."""

    def __init__(self, service: SqliteDeliveryService) -> None:
        self._svc = service

    def send_request(self, target: str, content_ref: str) -> Any:
        from cogito.channel.base import ChannelSendResult
        from cogito.service.gateway_client import gateway_status_to_channel

        content = self._resolve_content(content_ref)
        idem = f"delivery:{target}:{content_ref}"
        try:
            result = self._svc._gateway.send(target, content, idem)
        except Exception as exc:
            return ChannelSendResult(status="unknown", error_code=type(exc).__name__)
        return ChannelSendResult(
            status=gateway_status_to_channel(result.status),
            platform_message_id=result.platform_message_id,
            error_code=result.error_code,
            retry_after_seconds=result.retry_after_seconds,
        )

    def _resolve_content(self, content_ref: str) -> str:
        if not content_ref:
            return ""
        row = self._svc._conn.execute(
            "SELECT inline_data FROM content_parts "
            "WHERE message_id=? AND content_type='text' ORDER BY rowid LIMIT 1",
            (content_ref,),
        ).fetchone()
        # Scheduled/proactive callers may already provide literal or payload
        # references. Preserve the value if no Message row exists.
        return str(row[0]) if row is not None else content_ref


class _UnavailableGateway:
    """Safe default used by enqueue-only maintenance and migration paths."""

    def send(self, target_snapshot: str, content: str, idempotency_key: str) -> GatewayResult:
        return GatewayResult(status="unknown", error_code="gateway_not_configured")


# Compatibility re-exports for callers that imported the Port from this module.
__all__ = [
    "GatewayClient", "GatewayResult", "SqliteDeliveryService", "_uow",
]
