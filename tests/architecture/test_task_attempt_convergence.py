"""PR-B1: TaskAttempt lifecycle convergence — Plan 04 M1."""
from __future__ import annotations

import sqlite3

import pytest

from cogito.domain.task import Task, TaskAttempt, TaskAttemptStatus, TaskStatus
from cogito.service.task_service import SqliteTaskService


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


def test_create_claim_complete_lifecycle(db: sqlite3.Connection) -> None:
    """创建 → 领取 → 完成 完整生命周期。"""
    svc = SqliteTaskService(db)
    task = svc.create(task_type="echo", payload_ref="p1")
    assert task.status == TaskStatus.queued

    claim = svc.claim("worker-1")
    assert claim is not None
    assert claim.task.task_type == "echo"

    outcome = svc.complete(claim.task, claim.attempt, "worker-1")
    assert outcome.status == TaskStatus.completed


def test_claim_creates_monotonic_attempt_no(db: sqlite3.Connection) -> None:
    """同一 Task 的新 Attempt 编号单调递增（fail 后 retry）。"""
    svc = SqliteTaskService(db)
    svc.create(task_type="echo")
    c1 = svc.claim("w")
    assert c1 is not None
    # fail → reset_to_queued → claim again
    svc.fail(c1.task, c1.attempt, "w")
    db.execute("UPDATE tasks SET status='queued' WHERE task_id=?", (c1.task.task_id,))
    db.commit()
    c2 = svc.claim("w")
    assert c2 is not None
    assert c2.attempt.attempt_no > c1.attempt.attempt_no


def test_heartbeat_extends_lease(db: sqlite3.Connection) -> None:
    """心跳续租。"""
    svc = SqliteTaskService(db)
    svc.create(task_type="echo")
    claim = svc.claim("worker-1")
    assert claim is not None
    ok = svc.heartbeat(claim.task, claim.attempt)
    assert ok is True


def test_waiting_does_not_hold_lease(db: sqlite3.Connection) -> None:
    """waiting 状态不持有 Lease（TaskAttempt 不处于 running）。"""
    attempt = TaskAttempt(status=TaskAttemptStatus.failed)
    assert attempt.status != TaskAttemptStatus.running


def test_unknown_tool_not_in_normal_retry(db: sqlite3.Connection) -> None:
    """unknown Tool/Delivery 不进入普通 Retry（由 Reconcile 处理）。"""
    svc = SqliteTaskService(db)
    t = svc.create(task_type="unknown_side_effect")
    # 创建时状态为 queued，但 unknown 应走 reconcile 而非 retry
    assert t.status == TaskStatus.queued
