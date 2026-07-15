"""Task Repository — tasks / task_attempts 表 CRUD。

使用 cogito.domain.task 中的 Task/TaskAttempt 领域对象。
tasks 表和 task_attempts 表已在 0001_initial.sql 中定义。
"""

from __future__ import annotations

import ast
import json
import sqlite3
from datetime import UTC, datetime

from cogito.contracts.clock import epoch_ms, from_epoch_ms
from cogito.domain.task import Task, TaskAttempt, TaskStatus


class TaskRepository:
    """Task 数据访问层。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 读取 ──

    def get(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        return self._row_to_task(row) if row else None

    def find_queued(
        self, limit: int = 10,
        now: datetime | None = None,
    ) -> list[Task]:
        """查找可领取的 Task：queued 或 scheduled 且已到调度时间。"""
        now_ms = epoch_ms(now or datetime.now(UTC))
        rows = self._conn.execute(
            "SELECT * FROM tasks "
            "WHERE status IN ('queued', 'scheduled') "
            "AND (scheduled_at IS NULL OR scheduled_at <= ?) "
            "ORDER BY priority DESC, created_at ASC LIMIT ?",
            (now_ms, limit),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def find_by_type(
        self,
        task_type: str,
        status: str = "queued",
        limit: int = 10,
    ) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE task_type=? AND status=? "
            "ORDER BY priority DESC, created_at ASC LIMIT ?",
            (task_type, status, limit),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def exists_by_idempotency(self, idempotency_key: str) -> bool:
        """检查已存在相同幂等键的 Task（用于创建前去重）。"""
        row = self._conn.execute(
            "SELECT 1 FROM tasks WHERE idempotency_key=? LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        return row is not None

    def list_filtered(
        self,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Task]:
        """按状态过滤列出 Task（None 表示全部）。"""
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM tasks "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status=? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def count(self, status: str | None = None) -> int:
        """统计 Task 数量（按状态或全部）。"""
        if status is None:
            row = self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status=?", (status,),
            ).fetchone()
        return int(row[0]) if row else 0

    # ── 写入 ──

    def insert(self, task: Task) -> Task:
        now = epoch_ms(task.created_at or datetime.now(UTC))
        # 自动生成 idempotency_key（如果为空），避免 UNIQUE 约束冲突
        idempotency_key = task.idempotency_key or f"{task.task_id}:{now}"
        task.idempotency_key = idempotency_key
        self._conn.execute(
            "INSERT INTO tasks ("
            "  task_id, task_type, payload_ref, status, priority, "
            "  scheduled_at, retry_policy, lease_owner, lease_expires_at, "
            "  checkpoint_ref, idempotency_key, origin, created_at"
            ") VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?)",
            (
                task.task_id,
                task.task_type,
                task.payload_ref,
                task.status.value,
                task.priority,
                epoch_ms(task.scheduled_at) if task.scheduled_at else None,
                str(task.retry_policy or {}),
                task.lease_owner,
                epoch_ms(task.lease_expires_at) if task.lease_expires_at else None,
                task.checkpoint_ref,
                task.idempotency_key,
                task.origin,
                now,
            ),
        )
        return task

    def update(self, task: Task) -> bool:
        """乐观锁更新（按 idempotency_key）。返回 False 表示未匹配。"""
        now = epoch_ms(datetime.now(UTC))
        cursor = self._conn.execute(
            "UPDATE tasks SET "
            "  task_type=?, payload_ref=?, status=?, priority=?, "
            "  scheduled_at=?, retry_policy=?, "
            "  lease_owner=?, lease_expires_at=?, checkpoint_ref=?, "
            "  origin=?, lease_version=lease_version+1 "
            "WHERE task_id=? AND idempotency_key=?",
            (
                task.task_type,
                task.payload_ref,
                task.status.value,
                task.priority,
                epoch_ms(task.scheduled_at) if task.scheduled_at else None,
                str(task.retry_policy or {}),
                task.lease_owner,
                epoch_ms(task.lease_expires_at) if task.lease_expires_at else None,
                task.checkpoint_ref,
                task.origin,
                task.task_id,
                task.idempotency_key,
            ),
        )
        return cursor.rowcount > 0

    # ── 状态变更（原子操作）──

    def claim(
        self, task_id: str, worker_id: str,
        lease_ttl_ms: int, now_ms: int | None = None,
    ) -> bool:
        if now_ms is None:
            now_ms = epoch_ms(datetime.now(UTC))
        expires = now_ms + lease_ttl_ms
        cursor = self._conn.execute(
            "UPDATE tasks SET status='running', "
            "  lease_owner=?, lease_expires_at=?, "
            "  lease_version=lease_version+1, attempt_count=attempt_count+1 "
            "WHERE task_id=? AND status IN ('queued','scheduled') "
            "AND (scheduled_at IS NULL OR scheduled_at <= ?)",
            (worker_id, expires, task_id, now_ms),
        )
        return cursor.rowcount > 0

    def complete(
        self, task_id: str, worker_id: str, lease_version: int,
        now_ms: int | None = None, result_ref: str | None = None,
    ) -> bool:
        if now_ms is None:
            now_ms = epoch_ms(datetime.now(UTC))
        cursor = self._conn.execute(
            "UPDATE tasks SET status='completed', lease_owner=NULL, lease_expires_at=NULL, "
            "result_ref=COALESCE(?,result_ref) "
            "WHERE task_id=? AND lease_owner=? AND lease_version=? "
            "AND status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (result_ref, task_id, worker_id, lease_version, now_ms),
        )
        return cursor.rowcount > 0

    def fail(
        self, task_id: str, worker_id: str, lease_version: int,
        now_ms: int | None = None,
    ) -> bool:
        if now_ms is None:
            now_ms = epoch_ms(datetime.now(UTC))
        cursor = self._conn.execute(
            "UPDATE tasks SET status='failed', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE task_id=? AND lease_owner=? AND lease_version=? "
            "AND status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (task_id, worker_id, lease_version, now_ms),
        )
        return cursor.rowcount > 0

    def reset_to_queued(self, task_id: str) -> bool:
        """把 failed/cancelled 的 Task 重新排队（retry）。"""
        cursor = self._conn.execute(
            "UPDATE tasks SET status='queued', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE task_id=? AND status IN ('failed', 'cancelled')",
            (task_id,),
        )
        return cursor.rowcount > 0

    def heartbeat(
        self, task_id: str, worker_id: str, lease_version: int,
        lease_ttl_ms: int, now_ms: int | None = None,
    ) -> bool:
        if now_ms is None:
            now_ms = epoch_ms(datetime.now(UTC))
        expires = now_ms + lease_ttl_ms
        cursor = self._conn.execute(
            "UPDATE tasks SET lease_expires_at=? "
            "WHERE task_id=? AND lease_owner=? AND lease_version=? "
            "AND status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (expires, task_id, worker_id, lease_version, now_ms),
        )
        return cursor.rowcount > 0

    # ── 辅助 ──

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        raw_retry = row["retry_policy"] or "{}"
        try:
            retry_policy = json.loads(raw_retry)
        except (json.JSONDecodeError, TypeError):
            try:
                parsed = ast.literal_eval(raw_retry)
                retry_policy = parsed if isinstance(parsed, dict) else {}
            except (ValueError, SyntaxError):
                retry_policy = {}
        return Task(
            task_id=row["task_id"],
            task_type=row["task_type"],
            payload_ref=row["payload_ref"],
            status=TaskStatus(row["status"]),
            priority=row["priority"],
            scheduled_at=from_epoch_ms(row["scheduled_at"]),
            retry_policy=retry_policy,
            lease_owner=row["lease_owner"],
            lease_expires_at=from_epoch_ms(row["lease_expires_at"]),
            checkpoint_ref=row["checkpoint_ref"],
            idempotency_key=row["idempotency_key"],
            origin=row["origin"],
            created_at=from_epoch_ms(row["created_at"]),
        )


class TaskAttemptRepository:
    """TaskAttempt 数据访问层。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, attempt: TaskAttempt) -> TaskAttempt:
        self._conn.execute(
            "INSERT INTO task_attempts ("
            "  task_attempt_id, task_id, attempt_no, status, "
            "  lease_owner, lease_version, lease_expires_at, "
            "  checkpoint_ref, started_at"
            ") VALUES (?,?,?,?, ?,?,?, ?,?)",
            (
                attempt.task_attempt_id,
                attempt.task_id,
                attempt.attempt_no,
                attempt.status.value,
                attempt.lease_owner,
                attempt.lease_version,
                epoch_ms(attempt.lease_expires_at) if attempt.lease_expires_at else None,
                attempt.checkpoint_ref,
                epoch_ms(attempt.started_at) if attempt.started_at else None,
            ),
        )
        return attempt

    def succeed(self, attempt_id: str, finished_at: int | None = None) -> bool:
        if finished_at is None:
            finished_at = epoch_ms(datetime.now(UTC))
        cursor = self._conn.execute(
            "UPDATE task_attempts SET status='succeeded', finished_at=? "
            "WHERE task_attempt_id=? AND status IN ('created','running')",
            (finished_at, attempt_id),
        )
        return cursor.rowcount > 0

    def fail(self, attempt_id: str, finished_at: int | None = None) -> bool:
        if finished_at is None:
            finished_at = epoch_ms(datetime.now(UTC))
        cursor = self._conn.execute(
            "UPDATE task_attempts SET status='failed', finished_at=? "
            "WHERE task_attempt_id=? AND status IN ('created','running')",
            (finished_at, attempt_id),
        )
        return cursor.rowcount > 0

    def list_for_task(self, task_id: str) -> list[TaskAttempt]:
        """列出某个 Task 的全部 Attempt。"""
        rows = self._conn.execute(
            "SELECT * FROM task_attempts WHERE task_id=? "
            "ORDER BY attempt_no ASC",
            (task_id,),
        ).fetchall()
        return [self._row_to_attempt(r) for r in rows]

    def get_attempt(self, attempt_id: str) -> TaskAttempt | None:
        row = self._conn.execute(
            "SELECT * FROM task_attempts WHERE task_attempt_id=?", (attempt_id,),
        ).fetchone()
        return self._row_to_attempt(row) if row else None

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> TaskAttempt:
        return TaskAttempt(
            task_attempt_id=row["task_attempt_id"],
            task_id=row["task_id"],
            attempt_no=row["attempt_no"],
            status=row["status"],
            lease_owner=row["lease_owner"],
            lease_version=row["lease_version"],
            lease_expires_at=from_epoch_ms(row["lease_expires_at"]),
            checkpoint_ref=row["checkpoint_ref"],
            started_at=from_epoch_ms(row["started_at"]),
        )
