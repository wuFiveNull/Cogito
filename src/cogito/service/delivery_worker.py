"""Delivery Worker — 领取 pending Delivery、通过 Gateway 发送、重试和 Reconcile。

Fake Gateway 提供测试用的假发送通道。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import NamedTuple, Protocol

from cogito.service.unit_of_work import UnitOfWork


# ── Gateway Protocol ──


class Gateway(Protocol):
    """消息发送通道协议。"""

    def send(self, target: str, content_ref: str) -> bool:
        """发送消息。返回 True=成功, False=失败, None=未知。"""
        ...


class FakeGateway:
    """Fake 发送通道 —— 记录发送请求，可配置成功/失败。"""

    def __init__(self, *, fail: bool = False, unknown: bool = False) -> None:
        self._fail = fail
        self._unknown = unknown
        self.sent: list[tuple[str, str]] = []  # (target, content_ref)

    def send(self, target: str, content_ref: str) -> bool:
        if self._unknown:
            return None  # type: ignore[return-value]
        if self._fail:
            return False
        self.sent.append((target, content_ref))
        return True

    def reset(self) -> None:
        self.sent.clear()
        self._fail = False
        self._unknown = False


class DeliveryLease(NamedTuple):
    delivery_id: str
    target_snapshot: str
    content_ref: str | None
    idempotency_key: str
    created_at: str


MAX_DELIVERY_TENTATIVE = 3


class DeliveryWorker:
    """投递 Worker。"""

    def __init__(self, conn: sqlite3.Connection, gateway: Gateway) -> None:
        self._conn = conn
        self._gateway = gateway

    def lease_next(self, worker_id: str) -> DeliveryLease | None:
        """领取下一个待投递的 Delivery。"""
        with UnitOfWork(self._conn) as uow:
            row = self._conn.execute("""
                SELECT * FROM deliveries
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
            """).fetchone()

            if row is None:
                return None

            # 标记为 sending，记录 Attempt
            updated = self._conn.execute(
                "UPDATE deliveries SET status='sending' "
                "WHERE delivery_id=? AND status='pending'",
                (row["delivery_id"],),
            )
            if updated.rowcount == 0:
                return None

            # 创建 delivery_attempt
            import uuid
            attempt_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            attempt_no = self._next_attempt_no(row["delivery_id"])

            self._conn.execute(
                "INSERT INTO delivery_attempts (attempt_id, delivery_id, attempt_no, status, started_at) "
                "VALUES (?, ?, ?, 'sending', ?)",
                (attempt_id, row["delivery_id"], attempt_no, now),
            )

            uow.commit()

            return DeliveryLease(
                delivery_id=row["delivery_id"],
                target_snapshot=row["target_snapshot"],
                content_ref=row["content_ref"],
                idempotency_key=row["idempotency_key"],
                created_at=row["created_at"],
            )

    def deliver(self, lease: DeliveryLease) -> str:
        """发送 Delivery，返回结果状态。

        返回值：
        - 'sent' — 发送成功
        - 'failed' — 发送失败
        - 'unknown' — 外部成功但本地结果未知（reconcile 路径）
        """
        with UnitOfWork(self._conn) as uow:
            # 解析 target 和 content
            target = lease.target_snapshot
            content_ref = lease.content_ref or ""

            # 通过 Gateway 发送
            result = self._gateway.send(target, content_ref)

            now = datetime.now(timezone.utc).isoformat()
            attempt_row = self._conn.execute(
                "SELECT attempt_id FROM delivery_attempts "
                "WHERE delivery_id=? ORDER BY attempt_no DESC LIMIT 1",
                (lease.delivery_id,),
            ).fetchone()
            attempt_id = attempt_row["attempt_id"] if attempt_row else ""

            if result is True:
                # 成功
                self._conn.execute(
                    "UPDATE deliveries SET status='sent', platform_message_id=? "
                    "WHERE delivery_id=?",
                    (f"fake_{lease.delivery_id[:8]}", lease.delivery_id),
                )
                self._conn.execute(
                    "UPDATE delivery_attempts SET status='succeeded', finished_at=? "
                    "WHERE attempt_id=?",
                    (now, attempt_id),
                )
                uow.commit()
                return "sent"

            elif result is False:
                # 失败 — 检查重试次数
                tentative = self._conn.execute(
                    "SELECT COUNT(*) FROM delivery_attempts WHERE delivery_id=?",
                    (lease.delivery_id,),
                ).fetchone()[0]

                if tentative >= MAX_DELIVERY_TENTATIVE:
                    self._conn.execute(
                        "UPDATE deliveries SET status='failed' WHERE delivery_id=?",
                        (lease.delivery_id,),
                    )
                else:
                    self._conn.execute(
                        "UPDATE deliveries SET status='retry_scheduled' WHERE delivery_id=?",
                        (lease.delivery_id,),
                    )
                self._conn.execute(
                    "UPDATE delivery_attempts SET status='failed', finished_at=? "
                    "WHERE attempt_id=?",
                    (now, attempt_id),
                )
                uow.commit()
                return "failed"

            else:  # None — unknown
                self._conn.execute(
                    "UPDATE deliveries SET status='unknown' WHERE delivery_id=?",
                    (lease.delivery_id,),
                )
                self._conn.execute(
                    "UPDATE delivery_attempts SET status='failed', finished_at=? "
                    "WHERE attempt_id=?",
                    (now, attempt_id),
                )
                uow.commit()
                return "unknown"

    def reconcile(self, delivery_id: str, platform_message_id: str) -> bool:
        """Reconcile 路径：外部成功但本地结果未知 → 标记为 sent。"""
        with UnitOfWork(self._conn) as uow:
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
