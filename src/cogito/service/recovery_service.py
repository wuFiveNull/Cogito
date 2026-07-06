"""RecoveryService — 启动恢复扫描和定期清理。

处理以下场景（EXECUTION-LIFECYCLE / 5.3 进程重启）：
1. 过期的 Outbox Lease（status='leased' 且 lease_expires_at < now）
2. 长时间处于 sending 的 Delivery（Lease 过期 → unknown）
3. 无有效执行权的 running Turn/RunAttempt
4. unknown Delivery 保持待对账，不转回普通重试

恢复操作必须使用条件更新，重新验证 lease_version + lease_expires_at，
防止与 heartbeat 产生竞态（Worker 在 SELECT→UPDATE 间续期 Lease）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from cogito.domain.turn import RunAttemptStatus, TurnStatus
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.time_utils import epoch_ms


class RecoveryService:
    """启动恢复扫描和定期 Lease 清理。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def recover_outbox_leases(self, clock: datetime | None = None) -> int:
        """回收过期的 Outbox Lease。

        条件 UPDATE 重新验证 lease_version + lease_expires_at < now。
        若 Worker 在 SELECT 后续期，UPDATE 因 version/expires 不匹配而失败。
        """
        now = clock or datetime.now(UTC)
        now_ms_val = epoch_ms(now)

        with UnitOfWork(self._conn) as uow:
            rows = self._conn.execute(
                "SELECT event_id, lease_version FROM outbox_events "
                "WHERE status='leased' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < ?",
                (now_ms_val,),
            ).fetchall()

            count = 0
            for row in rows:
                updated = self._conn.execute(
                    "UPDATE outbox_events SET status='pending', lease_owner=NULL, "
                    "lease_expires_at=NULL, lease_version=lease_version+1 "
                    "WHERE event_id=? AND status='leased' AND lease_version=? "
                    "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                    (row["event_id"], row["lease_version"], now_ms_val),
                )
                if updated.rowcount > 0:
                    count += 1

            if count > 0:
                uow.commit()
        return count

    def recover_delivery_leases(self, clock: datetime | None = None) -> int:
        """回收过期的 Delivery Lease。

        sending + 过期 → unknown（不可回 pending）。
        条件 UPDATE 重新验证 lease_version + lease_expires_at。
        """
        now = clock or datetime.now(UTC)
        now_ms_val = epoch_ms(now)

        with UnitOfWork(self._conn) as uow:
            rows = self._conn.execute(
                "SELECT delivery_id, lease_version FROM deliveries "
                "WHERE status='sending' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < ?",
                (now_ms_val,),
            ).fetchall()

            count = 0
            for row in rows:
                updated = self._conn.execute(
                    "UPDATE deliveries SET status='unknown', lease_owner=NULL, "
                    "lease_expires_at=NULL, lease_version=lease_version+1 "
                    "WHERE delivery_id=? AND status='sending' AND lease_version=? "
                    "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                    (row["delivery_id"], row["lease_version"], now_ms_val),
                )
                if updated.rowcount > 0:
                    count += 1

            if count > 0:
                uow.commit()
        return count

    def recover_stale_turns(self, clock: datetime | None = None) -> int:
        """标记无有效执行权的 running Turn/RunAttempt 为 abandoned。

        只恢复 Lease 已过期的 Attempt（不干扰有效 Lease）。
        条件 UPDATE 重新验证 attempt 状态、lease_version 和过期时间。
        """
        now = clock or datetime.now(UTC)
        now_ms_val = epoch_ms(now)

        with UnitOfWork(self._conn) as uow:
            rows = self._conn.execute("""
                SELECT t.turn_id, t.version, t.active_attempt_id, a.attempt_id
                FROM turns t
                JOIN run_attempts a ON a.attempt_id = t.active_attempt_id
                WHERE t.status = 'running'
                  AND a.status = 'running'
                  AND (
                      a.lease_expires_at IS NULL
                      OR a.lease_expires_at < ?
                  )
            """, (now_ms_val,)).fetchall()

            count = 0
            for row in rows:
                # 条件标记 RunAttempt：验证 status + lease_expires_at
                self._conn.execute(
                    "UPDATE run_attempts SET status=?, finished_at=?, lease_version=lease_version+1 "
                    "WHERE attempt_id=? AND status='running' "
                    "AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
                    (RunAttemptStatus.abandoned.value, now_ms_val,
                     row["active_attempt_id"], now_ms_val),
                )

                # Turn 回到 queued（条件验证 version 防止竞态）
                updated = self._conn.execute(
                    "UPDATE turns SET status=?, active_attempt_id=NULL, version=version+1 "
                    "WHERE turn_id=? AND version=? AND status='running'",
                    (TurnStatus.queued.value, row["turn_id"], row["version"]),
                )
                if updated.rowcount > 0:
                    count += 1

            if count > 0:
                uow.commit()
        return count

    def recover_all(self, clock: datetime | None = None) -> dict[str, int]:
        return {
            "outbox_leases": self.recover_outbox_leases(clock),
            "delivery_leases": self.recover_delivery_leases(clock),
            "stale_turns": self.recover_stale_turns(clock),
        }
