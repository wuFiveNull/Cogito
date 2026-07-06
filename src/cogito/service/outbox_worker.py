"""Outbox Worker — 领取 pending Event、按聚合顺序发布、重试和 dead letter。

同聚合顺序保证：只领取当前最小版本号的 pending Event。
若更小版本仍处于 pending/leased/retry_scheduled，则跳过该聚合。

EVENT-OUTBOX / 3. Outbox 状态
pending → leased → published
                 ├→ retry_scheduled
                 └→ dead_letter

可靠性语义：
- lease_next 仅领取 pending 或已到期的 retry_scheduled
- 条件更新验证 lease_owner + lease_version
- 指数退避计算 next_attempt_at
- lease_expires_at = now + TTL（不等于 now）
- 达到 MAX_TENTATIVE 后精确进入 dead_letter
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import NamedTuple

from cogito.runtime.clock import Clock, ProductionClock
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.time_utils import epoch_ms

# ── 重试策略 ──

MAX_TENTATIVE = 3
BACKOFF_BASE_SECONDS = 10
BACKOFF_MULTIPLIER = 3
MAX_BACKOFF_SECONDS = 3600

# ── Lease TTL（可被 Config 覆盖）──

OUTBOX_LEASE_TTL_S = 120


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
    lease_version: int
    attempt_count: int


def compute_backoff(attempt_count: int) -> float:
    """指数退避计算重试延迟（秒）。"""
    delay = BACKOFF_BASE_SECONDS * (BACKOFF_MULTIPLIER ** (attempt_count - 1))
    return min(delay, MAX_BACKOFF_SECONDS)


class OutboxWorker:
    """Outbox 发布 Worker。"""

    def __init__(self, conn: sqlite3.Connection, lease_ttl_s: int = OUTBOX_LEASE_TTL_S,
                 clock: Clock | None = None) -> None:
        self._conn = conn
        self._lease_ttl_s = lease_ttl_s
        self._clock = clock or ProductionClock()

    def _now(self, override: datetime | None = None) -> datetime:
        return override if override is not None else self._clock.now()

    def lease_next(self, worker_id: str, clock: datetime | None = None) -> OutboxLease | None:
        """领取下一个待发布的 Outbox Event（同聚合有序）。"""
        now = self._now(clock)
        now_int = epoch_ms(now)
        lease_expires = now_int + self._lease_ttl_s * 1000

        with UnitOfWork(self._conn) as uow:
            row = self._conn.execute("""
                SELECT * FROM outbox_events o1
                WHERE (
                    o1.status = 'pending'
                    OR (
                        o1.status = 'retry_scheduled'
                        AND (o1.next_attempt_at IS NULL OR o1.next_attempt_at <= ?)
                    )
                )
                AND NOT EXISTS (
                    SELECT 1 FROM outbox_events o2
                    WHERE o2.aggregate_type = o1.aggregate_type
                      AND o2.aggregate_id = o1.aggregate_id
                      AND o2.aggregate_version < o1.aggregate_version
                      AND o2.status IN ('pending', 'leased', 'retry_scheduled')
                )
                ORDER BY o1.aggregate_type, o1.aggregate_id, o1.aggregate_version
                LIMIT 1
            """, (now_int,)).fetchone()

            if row is None:
                return None

            old_version = row["lease_version"]
            new_attempt_count = row["attempt_count"] + 1

            updated = self._conn.execute(
                "UPDATE outbox_events SET status='leased', lease_owner=?, "
                "lease_version=lease_version+1, attempt_count=?, lease_expires_at=? "
                "WHERE event_id=? AND status=? AND lease_version=?",
                (worker_id, new_attempt_count, lease_expires,
                 row["event_id"], row["status"], old_version),
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
                lease_version=old_version + 1,
                attempt_count=new_attempt_count,
            )

    def publish(self, lease: OutboxLease, worker_id: str, clock: datetime | None = None) -> bool:
        """标记 OutboxEvent 为已发布。

        验证 lease_owner + lease_version + lease_expires_at > now。
        """
        now_int = epoch_ms(self._now(clock))

        with UnitOfWork(self._conn) as uow:
            updated = self._conn.execute(
                "UPDATE outbox_events SET status='published' "
                "WHERE event_id=? AND lease_owner=? AND lease_version=? AND status='leased' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (lease.event_id, worker_id, lease.lease_version, now_int),
            )
            uow.commit()
            return updated.rowcount > 0

    def retry(self, lease: OutboxLease, worker_id: str, clock: datetime | None = None) -> bool:
        """标记为 retry_scheduled 或 dead_letter。

        验证 lease_owner + lease_version + lease_expires_at > now。
        """
        now = self._now(clock)
        now_int = epoch_ms(now)

        with UnitOfWork(self._conn) as uow:
            if lease.attempt_count >= MAX_TENTATIVE:
                updated = self._conn.execute(
                    "UPDATE outbox_events SET status='dead_letter' "
                    "WHERE event_id=? AND lease_owner=? AND lease_version=? AND status='leased' "
                    "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                    (lease.event_id, worker_id, lease.lease_version, now_int),
                )
                uow.commit()
                return updated.rowcount > 0

            delay_seconds = compute_backoff(lease.attempt_count)
            next_at = datetime.fromtimestamp(now.timestamp() + delay_seconds, tz=UTC)
            next_at_int = epoch_ms(next_at)

            updated = self._conn.execute(
                "UPDATE outbox_events SET status='retry_scheduled', next_attempt_at=? "
                "WHERE event_id=? AND lease_owner=? AND lease_version=? AND status='leased' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (next_at_int, lease.event_id, worker_id, lease.lease_version, now_int),
            )
            uow.commit()
            return updated.rowcount > 0

    def dead_letter(self, event_id: str, worker_id: str = "", lease_version: int = 0) -> bool:
        """直接移入 dead letter。"""
        with UnitOfWork(self._conn) as uow:
            if worker_id:
                updated = self._conn.execute(
                    "UPDATE outbox_events SET status='dead_letter' "
                    "WHERE event_id=? AND lease_owner=? AND lease_version=? "
                    "AND status IN ('leased', 'retry_scheduled')",
                    (event_id, worker_id, lease_version),
                )
            else:
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
