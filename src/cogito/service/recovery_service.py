"""RecoveryService — 启动恢复扫描和定期清理。

处理以下场景（EXECUTION-LIFECYCLE / 5.3 进程重启）：
1. 过期的 Outbox Lease（status='leased' 且 lease_expires_at < now）
2. 长时间处于 sending 的 Delivery（Lease 过期 → unknown）
3. 无有效执行权的 running Turn/RunAttempt
4. unknown Delivery 保持待对账，不转回普通重试

恢复操作必须使用条件更新，重新验证 lease_version + lease_expires_at，
防止与 heartbeat 产生竞态（Worker 在 SELECT→UPDATE 间续期 Lease）。

recover_stale_turns 原子保证：
1. SELECT 保存 attempt 的 lease_version 和原 lease_expires_at
2. Attempt UPDATE 条件验证 lease_version + lease_expires_at 未变化
3. 只有 Attempt 更新成功后才更新 Turn
4. Turn UPDATE 同时验证 active_attempt_id
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from cogito.domain.turn import RunAttemptStatus, TurnStatus
from cogito.contracts.clock import Clock, ProductionClock
from cogito.service.unit_of_work import UnitOfWork
from cogito.contracts.clock import epoch_ms

_LOG = logging.getLogger("cogito.recovery")


class RecoveryService:
    """启动恢复扫描和定期 Lease 清理。"""

    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None) -> None:
        self._conn = conn
        self._clock = clock or ProductionClock()

    def _now(self, override: datetime | None = None) -> datetime:
        return override if override is not None else self._clock.now()

    def recover_outbox_leases(self, clock: datetime | None = None) -> int:
        now_ms_val = epoch_ms(self._now(clock))

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
        now_ms_val = epoch_ms(self._now(clock))

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

        原子操作：
        1. SELECT 出 lease_version 和 lease_expires_at 用于条件更新
        2. Attempt UPDATE 验证 lease_version + lease_expires_at 未变化 + 仍过期
        3. 只有 Attempt rowcount > 0 才更新 Turn
        4. Turn UPDATE 验证 version + active_attempt_id

        Worker 在 SELECT 和 UPDATE 之间 heartbeat 成功时，
        Attempt UPDATE 的 lease_version/lease_expires_at 条件不匹配，行数=0，
        Turn 不会被修改。
        """
        now_ms_val = epoch_ms(self._now(clock))

        with UnitOfWork(self._conn) as uow:
            rows = self._conn.execute("""
                SELECT t.turn_id, t.version, t.active_attempt_id,
                       a.attempt_id, a.lease_version, a.lease_expires_at
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
                # Step 1: 条件标记 Attempt — 验证 lease_version + lease_expires_at 仍匹配且过期
                attempt_updated = self._conn.execute(
                    "UPDATE run_attempts SET status=?, finished_at=?, lease_version=lease_version+1 "
                    "WHERE attempt_id=? AND status='running' "
                    "AND lease_version=? "
                    "AND (lease_expires_at IS NULL OR lease_expires_at = ?)",
                    (RunAttemptStatus.abandoned.value, now_ms_val,
                     row["active_attempt_id"],
                     row["lease_version"], row["lease_expires_at"]),
                )

                # Step 2: 只有 Attempt 更新成功才能修改 Turn
                if attempt_updated.rowcount == 0:
                    continue  # heartbeat 续期 — 跳过，不修改 Turn

                # Step 3: 更新 Turn — 验证 version + active_attempt_id
                turn_updated = self._conn.execute(
                    "UPDATE turns SET status=?, active_attempt_id=NULL, version=version+1 "
                    "WHERE turn_id=? AND version=? AND status='running' AND active_attempt_id=?",
                    (TurnStatus.queued.value,
                     row["turn_id"], row["version"], row["active_attempt_id"]),
                )
                if turn_updated.rowcount > 0:
                    count += 1

            if count > 0:
                uow.commit()
        return count

    def recover_stale_tasks(self, clock: datetime | None = None) -> int:
        """标记无有效执行权的 running Task/TaskAttempt 为 abandoned+queued。

        与 recover_stale_turns 同款条件更新模式：
        1. SELECT 出 attempt 的 lease_version + lease_expires_at
        2. Attempt UPDATE 验证 lease_version + lease_expires_at 仍过期
        3. 仅 Attempt rowcount > 0 才将 Task 回 queued
        """
        now_ms_val = epoch_ms(self._now(clock))

        with UnitOfWork(self._conn) as uow:
            # 查 running Task 及其当前 running 的 Attempt
            rows = self._conn.execute("""
                SELECT t.task_id,
                       a.task_attempt_id AS attempt_id,
                       a.lease_version, a.lease_expires_at
                FROM tasks t
                JOIN task_attempts a ON a.task_id = t.task_id
                WHERE t.status = 'running'
                  AND a.status = 'running'
                  AND (
                      a.lease_expires_at IS NULL
                      OR a.lease_expires_at < ?
                  )
            """, (now_ms_val,)).fetchall()

            count = 0
            for row in rows:
                # Step 1: 条件标记 Attempt 为 abandoned（验证 lease_version + 仍过期）
                attempt_updated = self._conn.execute(
                    "UPDATE task_attempts SET status='abandoned', "
                    "  finished_at=?, lease_version=lease_version+1 "
                    "WHERE task_attempt_id=? AND status='running' "
                    "AND lease_version=? "
                    "AND (lease_expires_at IS NULL OR lease_expires_at = ?)",
                    (now_ms_val, row["attempt_id"],
                     row["lease_version"], row["lease_expires_at"]),
                )
                if attempt_updated.rowcount == 0:
                    continue  # heartbeat 续期成功，跳过

                # Step 2: Task 回 queued
                self._conn.execute(
                    "UPDATE tasks SET status='queued', lease_owner=NULL, "
                    "  lease_expires_at=NULL "
                    "WHERE task_id=? AND status='running'",
                    (row["task_id"],),
                )
                count += 1

            if count > 0:
                uow.commit()
        return count

    def recover_streaming_deliveries(self, clock: datetime | None = None) -> int:
        """撤回崩溃遗留的流式 Delivery（status='streaming' 且 Turn 已不再 running）。

        流式 Delivery 在 AgentRunner.run_once 内由 Turn 的 RunAttempt lease 拥有。
        若进程在流式过程中崩溃，delivery 永久卡在 'streaming'（平台已创建占位气泡），
        而其 Turn/RunAttempt 将由 recover_stale_turns 标记为 abandoned / queued。

        孤儿判定（与 recover_stale_turns 顺序配合，recover_all 先跑 stale_turns）：
        - Turn 不存在 → 孤儿
        - Turn 已不在 running（queued/completed/abandoned 等）→ 孤儿
        - Turn 名义 running 但其 active attempt 已不 running（崩溃但 lease 未过期的
          中间态）→ 孤儿，并把 Turn 重置为 queued 以便重新尝试

        注意：run_once 创建流式 delivery 前会把 Turn 置为 running，故一个合法的
        流式 delivery 必然对应 running 的 Turn。重放时旧 delivery 已被本函数撤回，
        不会出现 "Turn queued 但仍有合法 streaming delivery" 的误杀。

        不变量：本函数只写 status='streaming' 的行（条件 UPDATE），与运行中的
        AgentRunner 不冲突——后者要么已把 delivery 推进到 sent/interrupted，要么
        其 Turn 仍 running，不会被选中。
        """
        with UnitOfWork(self._conn) as uow:
            rows = self._conn.execute("""
                SELECT d.delivery_id, d.turn_id, d.platform_message_id,
                       t.status AS turn_status, t.version AS turn_version,
                       t.active_attempt_id
                FROM deliveries d
                LEFT JOIN turns t ON t.turn_id = d.turn_id
                WHERE d.status = 'streaming'
            """).fetchall()

            count = 0
            for row in rows:
                turn_status = row["turn_status"]
                reset_turn = False

                if turn_status is None:
                    orphan = True
                elif turn_status != "running":
                    orphan = True
                else:
                    # Turn 名义 running —— 需确认其 active attempt 是否仍存活
                    attempt_row = self._conn.execute(
                        "SELECT status FROM run_attempts WHERE attempt_id=?",
                        (row["active_attempt_id"],),
                    ).fetchone()
                    attempt_status = attempt_row["status"] if attempt_row else None
                    if attempt_status == "running":
                        continue  # 仍由存活的 attempt 持有，跳过
                    orphan = True
                    reset_turn = True

                if not orphan:
                    continue

                # 条件撤回：仅当仍 streaming，避免与运行中的 run_once 竞态
                updated = self._conn.execute(
                    "UPDATE deliveries SET status='interrupted', stream_status=NULL, "
                    "lease_version=lease_version+1 "
                    "WHERE delivery_id=? AND status='streaming'",
                    (row["delivery_id"],),
                )
                if updated.rowcount == 0:
                    continue
                count += 1
                _LOG.info(
                    "recover_streaming_deliveries: withdrawn orphan delivery=%s (turn=%s)",
                    row["delivery_id"], row["turn_id"],
                )

                # Turn 名义 running 但 attempt 已死 → 重置为 queued 以便重放
                if reset_turn and turn_status == "running":
                    self._conn.execute(
                        "UPDATE turns SET status='queued', active_attempt_id=NULL, "
                        "version=version+1 "
                        "WHERE turn_id=? AND status='running' AND version=?",
                        (row["turn_id"], row["turn_version"]),
                    )

            if count > 0:
                uow.commit()
        return count

    def recover_stale_ingestion_batches(self, clock: datetime | None = None) -> int:
        """收尾崩溃遗留的 MCP ingestion batch。

        合法的 started batch 必须仍有 running Task。启动恢复发生在 Worker 开始
        领取新任务之前，因此没有 running Task 的 started 行可安全标记为 failed。
        """
        now_ms_val = epoch_ms(self._now(clock))
        with UnitOfWork(self._conn) as uow:
            updated = self._conn.execute(
                "UPDATE ingestion_batches SET status='failed', "
                "error_ref=CASE WHEN error_ref='' THEN 'startup_recovery' ELSE error_ref END, "
                "completed_at=? WHERE status='started' AND (task_id IS NULL OR task_id='' "
                "OR NOT EXISTS (SELECT 1 FROM tasks t WHERE t.task_id=ingestion_batches.task_id "
                "AND t.status='running'))",
                (now_ms_val,),
            )
            if updated.rowcount:
                uow.commit()
            return int(updated.rowcount)

    def recover_all(self, clock: datetime | None = None) -> dict[str, int]:
        return {
            "outbox_leases": self.recover_outbox_leases(clock),
            "delivery_leases": self.recover_delivery_leases(clock),
            "stale_turns": self.recover_stale_turns(clock),
            "stale_tasks": self.recover_stale_tasks(clock),
            "streaming_deliveries": self.recover_streaming_deliveries(clock),
            "stale_ingestion_batches": self.recover_stale_ingestion_batches(clock),
        }
