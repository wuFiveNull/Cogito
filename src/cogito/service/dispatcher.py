"""Dispatcher — 领取 queued Turn、创建 RunAttempt、推进到 running。

Lane 概念：同一 context_partition_key 同一时间最多一个 running Turn，
通过 SQLite 事务原子性保证，不引入显式锁。

RUNTIME-FLOWS / 3.2 优先级：
优先级数值越大越重要（100=取消/审批 > 80=用户消息 > ... > 10=维护）

EXECUTION-LIFECYCLE / 3.2 开始 Attempt：
同一事务内完成验证 Turn=queued → 创建 RunAttempt → 设置 Lease。

GLOBAL-INVARIANTS / 2.5：
旧 Lease/旧版本 Attempt 的结果不得提交业务状态。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import NamedTuple

from cogito.domain.state_machines import validate_transition_turn
from cogito.domain.turn import RunAttempt, RunAttemptStatus, Turn, TurnStatus
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.time_utils import epoch_ms, now_ms

# Default lease TTL in seconds (overridable via Config)
DEFAULT_LEASE_TTL_S = 120


class ClaimedRun(NamedTuple):
    """Dispatcher 领取结果。"""
    turn: Turn
    attempt: RunAttempt


class Dispatcher:
    """Turn 调度器 —— 领取、执行、完成。

    职责：
    - claim_next：领取 queued Turn 并创建带有效 Lease 的 RunAttempt
    - complete：提交结果（验证 lease_owner, lease_version, 未过期）
    - fail：标记失败（同样验证 Lease）
    - heartbeat：延长当前 Lease
    """

    def __init__(self, conn: sqlite3.Connection, lease_ttl_s: int = DEFAULT_LEASE_TTL_S) -> None:
        self._conn = conn
        self._lease_ttl_s = lease_ttl_s

    def claim_next(self, worker_id: str, clock: datetime | None = None) -> ClaimedRun | None:
        """领取下一个可执行的 queued Turn。

        同一事务中：
        1. 验证 Turn=queued
        2. 验证 Lane 可用（同 Session 无 running Turn）
        3. 创建带有效 Lease 的 RunAttempt
        4. 设置 Turn.active_attempt_id
        5. 推进 Turn 和 Attempt 到 running
        """
        now = clock or datetime.now(UTC)
        lease_expires = epoch_ms(now) + self._lease_ttl_s * 1000

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
                cancel_requested_at=(
                    datetime.fromisoformat(turn_row["cancel_requested_at"])
                    if turn_row["cancel_requested_at"] else None
                ),
                active_attempt_id=turn_row["active_attempt_id"],
                final_message_id=turn_row["final_message_id"],
                created_at=datetime.fromisoformat(turn_row["created_at"]),
            )

            # 创建 RunAttempt
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
                lease_expires_at=now.timestamp() + self._lease_ttl_s,
            )

            # 原子推进 Turn: queued → running
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

            # 写入 RunAttempt（含 Lease 字段）
            self._conn.execute(
                "INSERT INTO run_attempts (attempt_id, turn_id, attempt_no, status, "
                "started_at, worker_id, lease_version, lease_expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt.attempt_id, attempt.turn_id, attempt.attempt_no,
                 RunAttemptStatus.running.value, epoch_ms(attempt.started_at),
                 worker_id, attempt.lease_version, int(lease_expires)),
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
        _uow: UnitOfWork | None = None,
    ) -> bool:
        """完成 RunAttempt 并推进 Turn 到 completed。

        验证（全部匹配才提交）：
        - turn version
        - active_attempt_id
        - attempt status='running'
        - lease_owner
        - lease_version
        """
        now = now_ms()
        now_iso = datetime.fromtimestamp(now / 1000, tz=UTC).isoformat()

        if _uow is not None:
            return self._complete_internal(
                _uow, turn_id, attempt_id, expected_turn_version,
                worker_id, lease_version, now_iso,
                final_message_id=final_message_id,
            )

        with UnitOfWork(self._conn) as uow:
            result = self._complete_internal(
                uow, turn_id, attempt_id, expected_turn_version,
                worker_id, lease_version, now_iso,
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
        now_iso: str,
        *,
        final_message_id: str | None = None,
    ) -> bool:
        # 验证 attempt 仍在运行且 Lease 匹配
        attempt_updated = self._conn.execute(
            "UPDATE run_attempts SET status=?, finished_at=?, worker_id=?, lease_version=? "
            "WHERE attempt_id=? AND status='running' AND turn_id=? "
            "AND worker_id=? AND lease_version=?",
            (RunAttemptStatus.succeeded.value, now_iso, worker_id, lease_version,
             attempt_id, turn_id, worker_id, lease_version),
        )

        # Turn: running → completed
        if final_message_id:
            turn_updated = self._conn.execute(
                "UPDATE turns SET status=?, final_message_id=?, version=version+1, completed_at=? "
                "WHERE turn_id=? AND version=? AND status='running'",
                (TurnStatus.completed.value, final_message_id, now_iso,
                 turn_id, expected_turn_version),
            )
        else:
            turn_updated = self._conn.execute(
                "UPDATE turns SET status=?, version=version+1, completed_at=? "
                "WHERE turn_id=? AND version=? AND status='running'",
                (TurnStatus.completed.value, now_iso, turn_id, expected_turn_version),
            )

        return attempt_updated.rowcount > 0 and turn_updated.rowcount > 0

    def fail(
        self,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
        worker_id: str = "",
        lease_version: int = 0,
    ) -> bool:
        """标记 RunAttempt 失败，推进 Turn 到 failed。

        验证 turn version + active_attempt_id + lease_owner + lease_version。
        """
        with UnitOfWork(self._conn) as uow:
            now = now_ms()
            now_iso = datetime.fromtimestamp(now / 1000, tz=UTC).isoformat()

            attempt_updated = self._conn.execute(
                "UPDATE run_attempts SET status=?, finished_at=?, worker_id=?, lease_version=? "
                "WHERE attempt_id=? AND status='running' AND turn_id=? "
                "AND worker_id=? AND lease_version=?",
                (RunAttemptStatus.failed.value, now_iso, worker_id, lease_version,
                 attempt_id, turn_id, worker_id, lease_version),
            )

            turn_updated = self._conn.execute(
                "UPDATE turns SET status=?, version=version+1 "
                "WHERE turn_id=? AND version=? AND status='running'",
                (TurnStatus.failed.value, turn_id, expected_turn_version),
            )

            result = attempt_updated.rowcount > 0 and turn_updated.rowcount > 0
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
        """延长当前 RunAttempt 的 Lease。

        调用前验证 lease_owner + lease_version 匹配。
        返回 True 表示心跳成功。
        """
        now = clock or datetime.now(UTC)
        new_expires = int(epoch_ms(now)) + self._lease_ttl_s * 1000

        with UnitOfWork(self._conn) as uow:
            updated = self._conn.execute(
                "UPDATE run_attempts SET lease_expires_at=?, heartbeat_at=? "
                "WHERE attempt_id=? AND turn_id=? AND worker_id=? AND lease_version=? "
                "AND status='running'",
                (new_expires, epoch_ms(now), attempt_id, turn_id, worker_id, lease_version),
            )
            uow.commit()
            return updated.rowcount > 0
