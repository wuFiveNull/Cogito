"""Dispatcher — 领取 queued Turn、创建 RunAttempt、推进到 running。

Lane 概念：同一 context_partition_key 同一时间最多一个 running Turn，
通过 SQLite 事务原子性保证，不引入显式锁。

RUNTIME-FLOWS / 3.2 优先级：
优先级数值越大越重要（100=取消/审批 > 80=用户消息 > ... > 10=维护）
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import NamedTuple

from cogito.domain.state_machines import validate_transition_turn
from cogito.domain.turn import RunAttempt, RunAttemptStatus, Turn, TurnStatus
from cogito.service.unit_of_work import UnitOfWork


class ClaimedRun(NamedTuple):
    """Dispatcher 领取结果。"""
    turn: Turn
    attempt: RunAttempt


class Dispatcher:
    """Turn 调度器 —— 领取、执行、完成。

    职责：
    - claim_next：领取下一个 queued Turn 并创建 RunAttempt
    - complete：完成 RunAttempt 并推进 Turn
    - fail：标记 RunAttempt 失败，推进 Turn 到 failed
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def claim_next(self, worker_id: str) -> ClaimedRun | None:
        """领取下一个可执行的 queued Turn。

        约束：
        - 同一 Session 中最多一个 running Turn（Lane 机制）
        - 按 priority DESC（高优先级优先）、created_at ASC 排序
        - 原子创建 RunAttempt
        """
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

            # 创建 RunAttempt（取当前最大 attempt_no + 1）
            max_no_row = self._conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM run_attempts WHERE turn_id=?",
                (turn.turn_id,),
            ).fetchone()
            attempt_no = max_no_row[0]

            attempt = RunAttempt(
                turn_id=turn.turn_id,
                attempt_no=attempt_no,
                status=RunAttemptStatus.created,
                started_at=datetime.now(UTC),
            )

            # 原子推进 Turn: queued → running（需要当前版本匹配）
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

            # 写入 RunAttempt
            self._conn.execute(
                "INSERT INTO run_attempts (attempt_id, turn_id, attempt_no, status, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (attempt.attempt_id, attempt.turn_id, attempt.attempt_no,
                 RunAttemptStatus.running.value, attempt.started_at.isoformat()),
            )

            attempt.status = RunAttemptStatus.running

            uow.commit()

        return ClaimedRun(turn=turn, attempt=attempt)

    def complete(
        self,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
        *,
        final_message_id: str | None = None,
        _uow: UnitOfWork | None = None,
    ) -> bool:
        """完成 RunAttempt 并推进 Turn 到 completed。

        原子操作：
        1. RunAttempt: running → succeeded（验证 attempt_id 和 status）
        2. Turn: running → completed（版本条件更新）

        只有两步都成功时返回 True。
        如果提供 _uow，则在现有 UoW 内执行（不自行 commit）。
        """
        if _uow is not None:
            return self._complete_internal(
                _uow, turn_id, attempt_id, expected_turn_version,
                final_message_id=final_message_id,
            )

        with UnitOfWork(self._conn) as uow:
            result = self._complete_internal(
                uow, turn_id, attempt_id, expected_turn_version,
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
        *,
        final_message_id: str | None = None,
    ) -> bool:
        """UoW 内部的完成逻辑（不自行 commit/rollback）。"""
        now = datetime.now(UTC)

        # 验证 attempt 仍在 running
        attempt_updated = self._conn.execute(
            "UPDATE run_attempts SET status=?, finished_at=? "
            "WHERE attempt_id=? AND status='running' AND turn_id=?",
            (RunAttemptStatus.succeeded.value, now.isoformat(), attempt_id, turn_id),
        )

        # Turn: running → completed（版本条件更新）
        if final_message_id:
            turn_updated = self._conn.execute(
                "UPDATE turns SET status=?, final_message_id=?, version=version+1 "
                "WHERE turn_id=? AND version=? AND status='running'",
                (TurnStatus.completed.value, final_message_id,
                 turn_id, expected_turn_version),
            )
        else:
            turn_updated = self._conn.execute(
                "UPDATE turns SET status=?, version=version+1 "
                "WHERE turn_id=? AND version=? AND status='running'",
                (TurnStatus.completed.value, turn_id, expected_turn_version),
            )

        return attempt_updated.rowcount > 0 and turn_updated.rowcount > 0

    def fail(
        self,
        turn_id: str,
        attempt_id: str,
        expected_turn_version: int,
    ) -> bool:
        """标记 RunAttempt 失败，推进 Turn 到 failed。

        原子操作：
        1. RunAttempt: running → failed（验证 attempt_id + turn_id）
        2. Turn: running → failed（版本条件更新）

        只有两步都成功时返回 True。
        """
        with UnitOfWork(self._conn) as uow:
            now = datetime.now(UTC)

            attempt_updated = self._conn.execute(
                "UPDATE run_attempts SET status=?, finished_at=? "
                "WHERE attempt_id=? AND status='running' AND turn_id=?",
                (RunAttemptStatus.failed.value, now.isoformat(), attempt_id, turn_id),
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
        """取消 queued 状态的 Turn，返回条件更新是否匹配。"""
        with UnitOfWork(self._conn) as uow:
            updated = self._conn.execute(
                "UPDATE turns SET status=?, version=version+1 "
                "WHERE turn_id=? AND version=? AND status='queued'",
                (TurnStatus.cancelled.value, turn_id, expected_version),
            )
            uow.commit()
            return updated.rowcount > 0
