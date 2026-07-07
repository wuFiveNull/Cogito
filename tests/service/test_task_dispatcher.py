"""Tests for TaskDispatcher — claim_next, complete, fail, heartbeat.

覆盖场景：
- claim_next 从 queued 领取并创建 Attempt
- 无可用 Task 时返回 None
- complete 成功提交
- fail 标记失败
- heartbeat 续期
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.domain.task import Task, TaskAttempt, TaskStatus
from cogito.service.task_dispatcher import TaskDispatcher
from cogito.store.migration import migrate
from cogito.store.time_utils import epoch_ms


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def dispatcher(db) -> TaskDispatcher:
    return TaskDispatcher(db)


def _insert_task(db, task_id="t1", task_type="memory.extract", priority=40):
    now = epoch_ms(datetime.now(UTC))
    db.execute(
        "INSERT INTO tasks (task_id, task_type, idempotency_key, status, priority, created_at) "
        "VALUES (?, ?, ?, 'queued', ?, ?)",
        (task_id, task_type, f"{task_id}:{now}", priority, now),
    )
    db.commit()


class TestTaskDispatcher:
    def test_claim_next_returns_task_and_attempt(self, db, dispatcher):
        _insert_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None
        assert claimed.task.task_id == "t1"
        assert claimed.task.status == TaskStatus.running
        assert claimed.attempt.task_id == "t1"
        assert claimed.attempt.lease_owner == "worker1"

    def test_claim_next_returns_highest_priority_first(self, db, dispatcher):
        _insert_task(db, task_id="t_low", priority=20)
        _insert_task(db, task_id="t_high", priority=80)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None
        assert claimed.task.task_id == "t_high"

    def test_claim_next_idle_when_no_tasks(self, db, dispatcher):
        claimed = dispatcher.claim_next("worker1")
        assert claimed is None

    def test_claim_next_only_queued(self, db, dispatcher):
        _insert_task(db, task_id="t1")
        # 第一次领取成功
        assert dispatcher.claim_next("worker1") is not None
        # 第二次无可用
        assert dispatcher.claim_next("worker2") is None

    def test_complete_success(self, db, dispatcher):
        _insert_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.complete(claimed.task, claimed.attempt, "worker1")
        assert ok is True

        row = db.execute(
            "SELECT status FROM tasks WHERE task_id='t1'"
        ).fetchone()
        assert row["status"] == "completed"

    def test_complete_fails_wrong_worker(self, db, dispatcher):
        _insert_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.complete(claimed.task, claimed.attempt, "wrong_worker")
        assert ok is False

    def test_fail_success(self, db, dispatcher):
        _insert_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.fail(claimed.task, claimed.attempt, "worker1")
        assert ok is True

        row = db.execute(
            "SELECT status FROM tasks WHERE task_id='t1'"
        ).fetchone()
        assert row["status"] == "failed"

    def test_heartbeat(self, db, dispatcher):
        _insert_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.heartbeat(
            claimed.task.task_id,
            claimed.attempt.task_attempt_id,
            "worker1",
            claimed.attempt.lease_version,
        )
        assert ok is True
