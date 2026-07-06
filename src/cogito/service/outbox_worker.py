"""Outbox Worker — 领取 pending Event、按聚合顺序发布、重试和 dead letter。

同聚合顺序保证：只领取当前最小版本号的 pending Event。
若更小版本仍处于 pending/leased/retry_scheduled，则跳过该聚合。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import NamedTuple

from cogito.service.unit_of_work import UnitOfWork


MAX_TENTATIVE = 3


class OutboxLease(NamedTuple):
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    payload_ref: str | None
    content_hash: str
    schema_version: str
    correlation_id: str
    causation_id: str
    origin: str
    trust_label: str
    created_at: str


class OutboxWorker:
    """Outbox 发布 Worker。

    每个 OutboxEvent 按 (aggregate_type, aggregate_id, aggregate_version) 有序发布。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def lease_next(self, worker_id: str) -> OutboxLease | None:
        """领取下一个待发布的 Outbox Event（同聚合有序）。"""
        with UnitOfWork(self._conn) as uow:
            # 找可领取的事件：按聚合顺序，跳过有更小版本 pending 的聚合
            row = self._conn.execute("""
                SELECT * FROM outbox_events o1
                WHERE o1.status = 'pending'
                  AND NOT EXISTS (
                    SELECT 1 FROM outbox_events o2
                    WHERE o2.aggregate_type = o1.aggregate_type
                      AND o2.aggregate_id = o1.aggregate_id
                      AND o2.aggregate_version < o1.aggregate_version
                      AND o2.status IN ('pending', 'leased', 'retry_scheduled')
                  )
                ORDER BY o1.aggregate_type, o1.aggregate_id, o1.aggregate_version
                LIMIT 1
            """).fetchone()

            if row is None:
                return None

            # 尝试获取 Lease
            updated = self._conn.execute(
                "UPDATE outbox_events SET status='leased', lease_owner=? "
                "WHERE event_id=? AND status='pending'",
                (worker_id, row["event_id"]),
            )
            if updated.rowcount == 0:
                return None

            uow.commit()

            return OutboxLease(
                event_id=row["event_id"],
                event_type=row["event_type"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                aggregate_version=row["aggregate_version"],
                payload_ref=row["payload_ref"],
                content_hash=row["content_hash"],
                schema_version=row["schema_version"],
                correlation_id=row["correlation_id"],
                causation_id=row["causation_id"],
                origin=row["origin"],
                trust_label=row["trust_label"],
                created_at=row["created_at"],
            )

    def publish(self, lease: OutboxLease) -> bool:
        """标记 OutboxEvent 为已发布。"""
        with UnitOfWork(self._conn) as uow:
            updated = self._conn.execute(
                "UPDATE outbox_events SET status='published' "
                "WHERE event_id=? AND status='leased'",
                (lease.event_id,),
            )
            uow.commit()
            return updated.rowcount > 0

    def retry(self, lease: OutboxLease) -> bool:
        """标记为 retry_scheduled（等待下次重试）。"""
        with UnitOfWork(self._conn) as uow:
            # 检查已有尝试次数
            row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM outbox_events o "
                "JOIN outbox_events o2 ON o2.aggregate_type=o.aggregate_type "
                "AND o2.aggregate_id=o.aggregate_id "
                "WHERE o.event_id=? AND o2.status='published'",
                (lease.event_id,),
            ).fetchone()
            tentative = row["cnt"] if row else 0

            if tentative >= MAX_TENTATIVE:
                self._conn.execute(
                    "UPDATE outbox_events SET status='dead_letter' "
                    "WHERE event_id=? AND status='leased'",
                    (lease.event_id,),
                )
            else:
                self._conn.execute(
                    "UPDATE outbox_events SET status='retry_scheduled' "
                    "WHERE event_id=? AND status='leased'",
                    (lease.event_id,),
                )
            uow.commit()
            return True

    def dead_letter(self, event_id: str) -> bool:
        """直接移入 dead letter。"""
        with UnitOfWork(self._conn) as uow:
            updated = self._conn.execute(
                "UPDATE outbox_events SET status='dead_letter' "
                "WHERE event_id=? AND status IN ('leased', 'retry_scheduled')",
                (event_id,),
            )
            uow.commit()
            return updated.rowcount > 0

    def count_pending(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE status='pending'"
        ).fetchone()[0]

    def count_by_status(self, status: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE status=?", (status,)
        ).fetchone()[0]
