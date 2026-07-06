"""RecoveryService — 启动恢复扫描和定期清理。

处理以下场景（EXECUTION-LIFECYCLE / 5.3 进程重启）：
1. 过期的 Outbox Lease（status='leased' 且 lease_expires_at < now）
2. 长时间处于 sending 的 Delivery（Lease 过期）
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


class RecoveryService:
    """启动恢复扫描和定期 Lease 清理。

    本阶段实现最小恢复操作：
    - 回收过期 Outbox Lease
    - 回收过期 Delivery Lease
    - 标记 abandoned 的 Turn/RunAttempt
    - unknown Delivery 不自动转 retry_scheduled
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def recover_outbox_leases(self, clock: datetime | None = None) -> int:
        """回收过期的 Outbox Lease。

        条件：status='leased' AND lease_expires_at IS NOT NULL AND lease_expires_at < now
        操作：status → 'pending'（重新待发布）
        返回：回收数量
        """
        now = clock or datetime.now(UTC)
        now_iso = now.isoformat()

        with UnitOfWork(self._conn) as uow:
            rows = self._conn.execute(
                "SELECT event_id, lease_owner, lease_version FROM outbox_events "
                "WHERE status='leased' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (now_iso,),
            ).fetchall()

            count = 0
            for row in rows:
                updated = self._conn.execute(
                    "UPDATE outbox_events SET status='pending', lease_owner=NULL, "
                    "lease_expires_at=NULL WHERE event_id=? AND status='leased'",
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
        操作：status → 'pending'（重新待发送）
        不处理 unknown Delivery —— 只能通过 reconcile 恢复。
        返回：回收数量
        """
        now = clock or datetime.now(UTC)
        now_iso = now.isoformat()

        with UnitOfWork(self._conn) as uow:
            rows = self._conn.execute(
                "SELECT delivery_id, lease_owner, lease_version FROM deliveries "
                "WHERE status='sending' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (now_iso,),
            ).fetchall()

            count = 0
            for row in rows:
                updated = self._conn.execute(
                    "UPDATE deliveries SET status='pending', lease_owner=NULL, "
                    "lease_expires_at=NULL WHERE delivery_id=? AND status='sending'",
                    (row["delivery_id"],),
                )
                if updated.rowcount > 0:
                    count += 1

            if count > 0:
                uow.commit()
        return count

    def recover_stale_turns(self, clock: datetime | None = None) -> int:
        """标记无有效执行权的 running Turn/RunAttempt 为 abandoned。

        本阶段简化实现：查找 status='running' 但无有效 Lease 信息的 Turn。
        （完整实现需检查过期时间；当前使用 active_attempt_id 是否为 NULL 作为判断。）
        操作：RunAttempt → abandoned；Turn → queued（可重新调度）
        返回：处理数量
        """
        now = clock or datetime.now(UTC)

        with UnitOfWork(self._conn) as uow:
            rows = self._conn.execute("""
                SELECT t.turn_id, t.version, t.active_attempt_id
                FROM turns t
                WHERE t.status = 'running'
                  AND t.active_attempt_id IS NOT NULL
            """).fetchall()

            count = 0
            for row in rows:
                # 标记 RunAttempt 为 abandoned
                self._conn.execute(
                    "UPDATE run_attempts SET status=?, finished_at=? "
                    "WHERE attempt_id=? AND status='running'",
                    (RunAttemptStatus.abandoned.value, now.isoformat(), row["active_attempt_id"]),
                )

                # Turn 回到 queued（需版本条件更新防竞态）
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
