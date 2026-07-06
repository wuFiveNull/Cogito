"""RecoveryService — 启动恢复扫描和定期清理。

处理以下场景（EXECUTION-LIFECYCLE / 5.3 进程重启）：
1. 过期的 Outbox Lease（status='leased' 且 lease_expires_at < now）
2. 长时间处于 sending 的 Delivery（Lease 过期 → unknown）
3. 无有效执行权的 running Turn/RunAttempt
4. unknown Delivery 保持待对账，不转回普通重试

恢复操作必须使用条件更新，旧 Worker 返回结果后不得提交。
恢复依据只能是数据库状态、Checkpoint、Lease 和 Receipt。
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

        条件：status='leased' AND lease_expires_at IS NOT NULL AND lease_expires_at < now
        操作：status → 'pending', 清除 lease_owner, 推进 lease_version
        返回：回收数量
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
                    "WHERE event_id=? AND status='leased'",
                    (row["event_id"],),
                )
                if updated.rowcount > 0:
                    count += 1

            if count > 0:
                uow.commit()
        return count

    def recover_delivery_leases(self, clock: datetime | None = None) -> int:
        """回收过期的 Delivery Lease。

        条件：status='sending' AND lease_expires_at IS NOT NULL AND lease_expires_at < now
        操作：status → 'unknown'（因外部可能已成功），清除 lease_owner，推进 lease_version
        sending 不能直接回 pending —— 外部结果可能已成功，应通过 reconcile 处理。
        返回：回收数量
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
                    "WHERE delivery_id=? AND status='sending'",
                    (row["delivery_id"],),
                )
                if updated.rowcount > 0:
                    count += 1

            if count > 0:
                uow.commit()
        return count

    def recover_stale_turns(self, clock: datetime | None = None) -> int:
        """标记无有效执行权的 running Turn/RunAttempt 为 abandoned。

        只恢复 Lease 已过期或明确失去执行权的 Attempt。
        仍持有有效 Lease 的 running Turn 不被重置。
        过期 Attempt → abandoned；Turn 根据恢复策略进入 queued。
        若无法判断则进入 manual_review（当前简化：只支持 queued）。
        """
        now = clock or datetime.now(UTC)
        now_ms_val = epoch_ms(now)

        with UnitOfWork(self._conn) as uow:
            rows = self._conn.execute("""
                SELECT t.turn_id, t.version, t.active_attempt_id
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
                # 标记 RunAttempt 为 abandoned
                self._conn.execute(
                    "UPDATE run_attempts SET status=?, finished_at=?, lease_version=lease_version+1 "
                    "WHERE attempt_id=? AND status='running'",
                    (RunAttemptStatus.abandoned.value, now_ms_val, row["active_attempt_id"]),
                )

                # Turn 回到 queued（但保持 version 递增防竞态）
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
        """执行所有恢复扫描。

        Returns: 每类恢复的数量。
        """
        return {
            "outbox_leases": self.recover_outbox_leases(clock),
            "delivery_leases": self.recover_delivery_leases(clock),
            "stale_turns": self.recover_stale_turns(clock),
        }
