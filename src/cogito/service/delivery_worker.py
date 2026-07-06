"""Delivery Worker — 领取 pending/retry_scheduled Delivery、通过 Gateway 发送、重试和 Reconcile。

Fake Gateway 提供测试用的假发送通道。

ACCESS-DELIVERY / 4.3 Delivery 状态
pending → sending → sent
              ├→ retry_scheduled
              ├→ failed
              └→ unknown → sent (reconcile)

可靠性语义：
- lease_next 仅领取 pending 或已到期的 retry_scheduled
- 条件更新验证 lease_owner + lease_version
- 外部结果明确失败时按策略重试
- 外部结果 unknown 时只能 reconcile，不能自动重试
- 达到 MAX_TENTATIVE 后精确进入 failed
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import NamedTuple, Protocol

from cogito.service.unit_of_work import UnitOfWork

# ── Gateway Protocol ──


class Gateway(Protocol):
    """消息发送通道协议。"""

    def send(self, target: str, content_ref: str) -> bool | None:
        """发送消息。返回 True=成功, False=失败, None=未知。"""
        ...


class FakeGateway:
    """Fake 发送通道 —— 记录发送请求，可配置成功/失败。"""

    def __init__(self, *, fail: bool = False, unknown: bool = False) -> None:
        self._fail = fail
        self._unknown = unknown
        self.sent: list[tuple[str, str]] = []  # (target, content_ref)

    def send(self, target: str, content_ref: str) -> bool | None:
        if self._unknown:
            return None
        if self._fail:
            return False
        self.sent.append((target, content_ref))
        return True

    def reset(self) -> None:
        self.sent.clear()
        self._fail = False
        self._unknown = False


# ── 重试策略 ──

MAX_DELIVERY_TENTATIVE = 3
DELIVERY_BACKOFF_BASE = 10
DELIVERY_BACKOFF_MULTIPLIER = 3
DELIVERY_MAX_BACKOFF = 3600


class DeliveryLease(NamedTuple):
    delivery_id: str
    target_snapshot: str
    content_ref: str | None
    idempotency_key: str
    created_at: str
    lease_version: int
    attempt_count: int


def compute_delivery_backoff(attempt_count: int) -> float:
    delay = DELIVERY_BACKOFF_BASE * (DELIVERY_BACKOFF_MULTIPLIER ** (attempt_count - 1))
    return min(delay, DELIVERY_MAX_BACKOFF)


class DeliveryWorker:
    """投递 Worker。"""

    def __init__(self, conn: sqlite3.Connection, gateway: Gateway) -> None:
        self._conn = conn
        self._gateway = gateway

    def lease_next(self, worker_id: str, clock: datetime | None = None) -> DeliveryLease | None:
        """领取下一个待投递的 Delivery。

        可领取：
        - status = 'pending'（新投递）
        - status = 'retry_scheduled' AND next_attempt_at <= now（重试到期）
        """
        now = clock or datetime.now(UTC)
        now_iso = now.isoformat()

        with UnitOfWork(self._conn) as uow:
            row = self._conn.execute("""
                SELECT * FROM deliveries
                WHERE (
                    status = 'pending'
                    OR (
                        status = 'retry_scheduled'
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    )
                )
                ORDER BY created_at ASC
                LIMIT 1
            """, (now_iso,)).fetchone()

            if row is None:
                return None

            old_version = row["lease_version"]
            new_attempt_count = row["attempt_count"] + 1

            updated = self._conn.execute(
                "UPDATE deliveries SET status='sending', lease_owner=?, "
                "lease_version=lease_version+1, attempt_count=?, lease_expires_at=? "
                "WHERE delivery_id=? AND status=? AND lease_version=?",
                (worker_id, new_attempt_count, now_iso,
                 row["delivery_id"], row["status"], old_version),
            )
            if updated.rowcount == 0:
                return None

            import uuid
            attempt_id = uuid.uuid4().hex
            attempt_no = self._next_attempt_no(row["delivery_id"])

            self._conn.execute(
                "INSERT INTO delivery_attempts (attempt_id, delivery_id, attempt_no, status, "
                "started_at, lease_owner, lease_version) "
                "VALUES (?, ?, ?, 'sending', ?, ?, ?)",
                (attempt_id, row["delivery_id"], attempt_no, now, worker_id, old_version + 1),
            )

            uow.commit()

            return DeliveryLease(
                delivery_id=row["delivery_id"],
                target_snapshot=row["target_snapshot"],
                content_ref=row["content_ref"],
                idempotency_key=row["idempotency_key"],
                created_at=row["created_at"],
                lease_version=old_version + 1,
                attempt_count=new_attempt_count,
            )

    def deliver(self, lease: DeliveryLease, worker_id: str, clock: datetime | None = None) -> str:
        """发送 Delivery。

        验证 lease_owner + lease_version 后才提交结果。

        返回值：
        - 'sent' — 发送成功
        - 'failed' — 外部明确失败（可重试）
        - 'unknown' — 外部结果未知（仅可 reconcile，不可自动重试）
        - 'stale' — Lease 不匹配，拒绝提交
        """
        target = lease.target_snapshot
        content_ref = lease.content_ref or ""

        # Gateway 调用在事务外
        result = self._gateway.send(target, content_ref)

        now = clock or datetime.now(UTC)
        now_iso = now.isoformat()

        with UnitOfWork(self._conn) as uow:
            # 验证 Lease 有效性
            current = self._conn.execute(
                "SELECT status, lease_owner, lease_version FROM deliveries WHERE delivery_id=?",
                (lease.delivery_id,),
            ).fetchone()

            if current is None or current["lease_owner"] != worker_id or current["lease_version"] != lease.lease_version:
                return "stale"

            attempt_row = self._conn.execute(
                "SELECT attempt_id FROM delivery_attempts "
                "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                (lease.delivery_id, worker_id, lease.lease_version),
            ).fetchone()
            attempt_id = attempt_row["attempt_id"] if attempt_row else ""

            if result is True:
                self._conn.execute(
                    "UPDATE deliveries SET status='sent', platform_message_id=? "
                    "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                    (f"fake_{lease.delivery_id[:8]}", lease.delivery_id, worker_id, lease.lease_version),
                )
                self._conn.execute(
                    "UPDATE delivery_attempts SET status='succeeded', finished_at=? "
                    "WHERE attempt_id=?",
                    (now_iso, attempt_id),
                )
                uow.commit()
                return "sent"

            if result is False:
                if lease.attempt_count >= MAX_DELIVERY_TENTATIVE:
                    self._conn.execute(
                        "UPDATE deliveries SET status='failed' "
                        "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                        (lease.delivery_id, worker_id, lease.lease_version),
                    )
                else:
                    delay = compute_delivery_backoff(lease.attempt_count)
                    next_at = datetime.fromtimestamp(now.timestamp() + delay, tz=UTC)
                    self._conn.execute(
                        "UPDATE deliveries SET status='retry_scheduled', next_attempt_at=? "
                        "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                        (next_at.isoformat(), lease.delivery_id, worker_id, lease.lease_version),
                    )
                self._conn.execute(
                    "UPDATE delivery_attempts SET status='failed', finished_at=? "
                    "WHERE attempt_id=?",
                    (now_iso, attempt_id),
                )
                uow.commit()
                return "failed"

            # result is None → unknown
            self._conn.execute(
                "UPDATE deliveries SET status='unknown' "
                "WHERE delivery_id=? AND lease_owner=? AND lease_version=?",
                (lease.delivery_id, worker_id, lease.lease_version),
            )
            self._conn.execute(
                "UPDATE delivery_attempts SET status='failed', finished_at=? "
                "WHERE attempt_id=?",
                (now_iso, attempt_id),
            )
            uow.commit()
            return "unknown"

    def reconcile(self, delivery_id: str, platform_message_id: str, worker_id: str = "") -> bool:
        """Reconcile 路径：外部成功但本地结果 unknown → 标记为 sent。

        必须从 status='unknown' 转换。若指定 worker_id 则验证 Lease 归属。
        """
        with UnitOfWork(self._conn) as uow:
            if worker_id:
                updated = self._conn.execute(
                    "UPDATE deliveries SET status='sent', platform_message_id=? "
                    "WHERE delivery_id=? AND status='unknown' AND lease_owner=?",
                    (platform_message_id, delivery_id, worker_id),
                )
            else:
                updated = self._conn.execute(
                    "UPDATE deliveries SET status='sent', platform_message_id=? "
                    "WHERE delivery_id=? AND status='unknown'",
                    (platform_message_id, delivery_id),
                )
            uow.commit()
            return updated.rowcount > 0

    def _next_attempt_no(self, delivery_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM delivery_attempts WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        return row[0]
