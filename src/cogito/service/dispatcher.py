"""Dispatcher — 领取 queued Turn、创建 RunAttempt、推进到 running。

RUNTIME-FLOWS / 3.2 优先级：数值越大越重要。
EXECUTION-LIFECYCLE / 3.2：同一事务内完成 Turn→running + RunAttempt 创建。
GLOBAL-INVARIANTS / 2.5：旧 Lease/旧版本 Attempt 的结果不得提交业务状态。

complete/fail/heartbeat 必须验证：
- Turn.status = running
- Turn.version 匹配
- Turn.active_attempt_id = attempt_id
- RunAttempt.status = running
- worker_id 匹配
- lease_version 匹配
- lease_expires_at > now
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import NamedTuple

from cogito.domain.state_machines import validate_transition_turn
from cogito.domain.turn import RunAttempt, RunAttemptStatus, Turn, TurnStatus
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.time_utils import epoch_ms, from_epoch_ms

DEFAULT_LEASE_TTL_S = 120


class ClaimedRun(NamedTuple):
    turn: Turn
    attempt: RunAttempt


class Dispatcher:
    """Turn 调度器 —— 领取、运行、完成、心跳。"""

    def __init__(self, conn: sqlite3.Connection, lease_ttl_s: int = DEFAULT_LEASE_TTL_S) -> None:
        self._conn = conn
        self._lease_ttl_s = lease_ttl_s

    def claim_next(self, worker_id: str, clock: datetime | None = None) -> ClaimedRun | None:
        """领取 queued Turn，创建带有效 Lease 的 RunAttempt。

        同一事务：
        1. 验证 Turn=queued
        2. 验证 Lane 可用
        3. 创建 RunAttempt（含 Lease = now + TTL）
        4. 推进 Turn/Attempt 到 running
        """
        now = clock or datetime.now(UTC)
        lease_expires_ms = epoch_ms(now) + self._lease_ttl_s * 1000

        with UnitOfWork(self._conn) as uow:
            turn_row = self._conn.execute("""
                SELECT t.* FROM turns t
                JOIN sessions s ON s.session_id = t.session_id
                WHERE t.status = 'queued'
                  AND NOT EXISTS (
                    SELECT 1 FROM turns t2
                    WHERE t2.session_id = t.session_id
                      AND t2.status = 'running'
                  )
                ORDER BY t.priority DESC, t.created_at ASC
                LIMIT 1
            """).fetchone()

            if turn_row is None:
                return None

            turn = Turn(
                turn_id=turn_row["turn_id"],
                session_id=turn_row["session_id"],
                input_message_id=turn_row["input_message_id"],
                status=TurnStatus(turn_row["status"]),
                priority=turn_row["priority"],
                version=turn_row["version"],
                cancel_requested_at=from_epoch_ms(turn_row["cancel_requested_at"]),
                active_attempt_id=turn_row["active_attempt_id"],
                final_message_id=turn_row["final_message_id"],
                created_at=from_epoch_ms(turn_row["created_at"]),
            )

            max_no_row = self._conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM run_attempts WHERE turn_id=?",
                (turn.turn_id,),
            ).fetchone()
            attempt_no = max_no_row[0]

            attempt = RunAttempt(
                turn_id=turn.turn_id,
                attempt_no=attempt_no,
                status=RunAttemptStatus.created,
                started_at=now,
                worker_id=worker_id,
                lease_version=1,
                lease_expires_at=from_epoch_ms(lease_expires_ms),
            )

            validate_transition_turn(turn.turn_id, turn.status, TurnStatus.running)

            updated = self._conn.execute(
                "UPDATE turns SET status=?, active_attempt_id=?, version=version+1 "
                "WHERE turn_id=? AND version=? AND status='queued'",
                (TurnStatus.running.value, attempt.attempt_id,
                 turn.turn_id, turn.version),
            )
            if updated.rowcount == 0:
                return None

            turn.version += 1
            turn.status = TurnStatus.running
            turn.active_attempt_id = attempt.attempt_id

            self._conn.execute(
                "INSERT INTO run_attempts (attempt_id, turn_id, attempt_no, status, "
                "started_at, worker_id, lease_version, lease_expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt.attempt_id, attempt.turn_id, attempt.attempt_no,
                 RunAttemptStatus.running.value, epoch_ms(attempt.started_at),
                 worker_id, attempt.lease_version, lease_expires_ms),
            )

            attempt.status = RunAttemptStatus.running
            uow.commit()

        return ClaimedRun(turn=turn, attempt=attempt)

    def complete(
        self,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
        worker_id: str = "",
        lease_version: int = 0,
        *,
        final_message_id: str | None = None,
        clock: datetime | None = None,
        _uow: UnitOfWork | None = None,
    ) -> bool:
        """完成 RunAttempt。通过条件 UPDATE 全量校验 Lease 有效性。

        ALL 条件必须匹配：
        - Turn.status = running
        - Turn.version = expected_turn_version
        - Turn.active_attempt_id = attempt_id
        - RunAttempt.status = running
        - RunAttempt.worker_id = worker_id
        - RunAttempt.lease_version = lease_version
        - RunAttempt.lease_expires_at > now
        """
        now = clock or datetime.now(UTC)
        now_ms_val = epoch_ms(now)

        if _uow is not None:
            return self._complete_internal(
                _uow, turn_id, attempt_id, expected_turn_version,
                worker_id, lease_version, now_ms_val,
                final_message_id=final_message_id,
            )

        with UnitOfWork(self._conn) as uow:
            result = self._complete_internal(
                uow, turn_id, attempt_id, expected_turn_version,
                worker_id, lease_version, now_ms_val,
                final_message_id=final_message_id,
            )
            if result:
                uow.commit()
        return result

    def _complete_internal(
        self,
        uow: UnitOfWork,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
        worker_id: str,
        lease_version: int,
        now_ms_val: int,
        *,
        final_message_id: str | None = None,
    ) -> bool:
        # 全量条件验证：run_attempt 必须匹配指定 attempt_id、worker_id、lease_version 且未过期
        attempt_updated = self._conn.execute(
            "UPDATE run_attempts SET status=?, finished_at=? "
            "WHERE attempt_id=? AND status='running' AND turn_id=? "
            "AND worker_id=? AND lease_version=? "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (RunAttemptStatus.succeeded.value, now_ms_val,
             attempt_id, turn_id, worker_id, lease_version, now_ms_val),
        )

        if attempt_updated.rowcount == 0:
            return False

        if final_message_id:
            turn_updated = self._conn.execute(
                "UPDATE turns SET status=?, final_message_id=?, version=version+1, completed_at=? "
                "WHERE turn_id=? AND version=? AND status='running' AND active_attempt_id=?",
                (TurnStatus.completed.value, final_message_id, now_ms_val,
                 turn_id, expected_turn_version, attempt_id),
            )
        else:
            turn_updated = self._conn.execute(
                "UPDATE turns SET status=?, version=version+1, completed_at=? "
                "WHERE turn_id=? AND version=? AND status='running' AND active_attempt_id=?",
                (TurnStatus.completed.value, now_ms_val,
                 turn_id, expected_turn_version, attempt_id),
            )

        return turn_updated.rowcount > 0

    def fail(
        self,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
        worker_id: str = "",
        lease_version: int = 0,
        clock: datetime | None = None,
    ) -> bool:
        """标记 RunAttempt 失败。全量条件验证同 complete。"""
        now = clock or datetime.now(UTC)
        now_ms_val = epoch_ms(now)

        with UnitOfWork(self._conn) as uow:
            attempt_updated = self._conn.execute(
                "UPDATE run_attempts SET status=?, finished_at=? "
                "WHERE attempt_id=? AND status='running' AND turn_id=? "
                "AND worker_id=? AND lease_version=? "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (RunAttemptStatus.failed.value, now_ms_val,
                 attempt_id, turn_id, worker_id, lease_version, now_ms_val),
            )

            if attempt_updated.rowcount == 0:
                return False

            turn_updated = self._conn.execute(
                "UPDATE turns SET status=?, version=version+1 "
                "WHERE turn_id=? AND version=? AND status='running' AND active_attempt_id=?",
                (TurnStatus.failed.value, turn_id, expected_turn_version, attempt_id),
            )

            result = turn_updated.rowcount > 0
            if result:
                uow.commit()
            return result

    def cancel(self, turn_id: str, expected_version: int) -> bool:
        """取消 queued 状态的 Turn。"""
        with UnitOfWork(self._conn) as uow:
            updated = self._conn.execute(
                "UPDATE turns SET status=?, version=version+1 "
                "WHERE turn_id=? AND version=? AND status='queued'",
                (TurnStatus.cancelled.value, turn_id, expected_version),
            )
            uow.commit()
            return updated.rowcount > 0

    def heartbeat(
        self,
        turn_id: str,
        attempt_id: str,
        worker_id: str,
        lease_version: int,
        clock: datetime | None = None,
    ) -> bool:
        """延长当前有效 Lease。

        仅当 Lease 尚未过期时续期。已过期 Lease 返回 False。
        验证：worker_id + lease_version + lease_expires_at > now。
        """
        now = clock or datetime.now(UTC)
        new_expires_ms = epoch_ms(now) + self._lease_ttl_s * 1000

        with UnitOfWork(self._conn) as uow:
            updated = self._conn.execute(
                "UPDATE run_attempts SET lease_expires_at=?, heartbeat_at=? "
                "WHERE attempt_id=? AND turn_id=? AND worker_id=? AND lease_version=? "
                "AND status='running' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (new_expires_ms, epoch_ms(now),
                 attempt_id, turn_id, worker_id, lease_version,
                 epoch_ms(now)),
            )
            uow.commit()
            return updated.rowcount > 0
