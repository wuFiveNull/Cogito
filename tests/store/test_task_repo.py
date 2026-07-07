"""Tests for TaskRepository — CRUD + 状态变更验证。

覆盖场景：
- 插入和查询
- 状态转换（queued → running → completed/failed）
- Lease 领取和心跳
- 乐观锁
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.task import Task, TaskAttempt, TaskStatus, TaskAttemptStatus
from cogito.store.task_repo import TaskRepository, TaskAttemptRepository
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
def repo(db) -> TaskRepository:
    return TaskRepository(db)


def _create_task(**kwargs) -> Task:
    defaults = dict(
        task_id="t1",
        task_type="memory.extract",
        status=TaskStatus.queued,
        priority=40,
        origin="test",
    )
    defaults.update(kwargs)
    return Task(**defaults)


class TestTaskRepository:
    def test_insert_and_get(self, repo):
        task = _create_task()
        repo.insert(task)
        got = repo.get(task.task_id)
        assert got is not None
        assert got.task_id == "t1"
        assert got.task_type == "memory.extract"

    def test_get_nonexistent(self, repo):
        assert repo.get("nonexistent") is None

    def test_find_queued(self, repo):
        t1 = _create_task(task_id="t1", priority=80)
        t2 = _create_task(task_id="t2", priority=40)
        repo.insert(t1)
        repo.insert(t2)

        queued = repo.find_queued(limit=10)
        assert len(queued) == 2
        # 高优先级在前
        assert queued[0].task_id == "t1"

    def test_find_queued_excludes_running(self, repo):
        t1 = _create_task(task_id="t1", status=TaskStatus.running)
        t2 = _create_task(task_id="t2", status=TaskStatus.queued)
        repo.insert(t1)
        repo.insert(t2)

        queued = repo.find_queued(limit=10)
        assert len(queued) == 1
        assert queued[0].task_id == "t2"

    def test_find_queued_respects_limit(self, repo):
        for i in range(5):
            repo.insert(_create_task(task_id=f"t{i}"))
        queued = repo.find_queued(limit=2)
        assert len(queued) == 2

    def test_claim_transition(self, repo):
        task = _create_task(task_id="t_claim")
        repo.insert(task)

        ok = repo.claim("t_claim", "worker1", lease_ttl_ms=120000)
        assert ok is True

        got = repo.get("t_claim")
        assert got is not None
        assert got.lease_owner == "worker1"
        assert got.lease_expires_at is not None

    def test_claim_fails_already_running(self, repo):
        task = _create_task(task_id="t1", status=TaskStatus.running)
        repo.insert(task)

        ok = repo.claim("t1", "worker2", lease_ttl_ms=120000)
        assert ok is False

    def test_complete_with_valid_lease(self, repo):
        task = _create_task(task_id="t_c")
        repo.insert(task)

        repo.claim("t_c", "worker1", lease_ttl_ms=120000)
        # 获取当前 lease_version（claim 后 version=1）
        row = repo._conn.execute(
            "SELECT lease_version FROM tasks WHERE task_id='t_c'"
        ).fetchone()
        lease_version = row["lease_version"]

        ok = repo.complete("t_c", "worker1", lease_version)
        assert ok is True

        got = repo.get("t_c")
        assert got.status == TaskStatus.completed

    def test_complete_fails_wrong_worker(self, repo):
        task = _create_task(task_id="t_fail")
        repo.insert(task)
        repo.claim("t_fail", "worker1", lease_ttl_ms=120000)

        row = repo._conn.execute(
            "SELECT lease_version FROM tasks WHERE task_id='t_fail'"
        ).fetchone()
        lease_version = row["lease_version"]

        ok = repo.complete("t_fail", "wrong_worker", lease_version)
        assert ok is False

    def test_fail_transition(self, repo):
        task = _create_task(task_id="t_f")
        repo.insert(task)
        repo.claim("t_f", "worker1", lease_ttl_ms=120000)

        row = repo._conn.execute(
            "SELECT lease_version FROM tasks WHERE task_id='t_f'"
        ).fetchone()
        lease_version = row["lease_version"]

        ok = repo.fail("t_f", "worker1", lease_version)
        assert ok is True

        got = repo.get("t_f")
        assert got.status == TaskStatus.failed

    def test_heartbeat_extends_lease(self, repo):
        task = _create_task(task_id="t_hb")
        repo.insert(task)
        repo.claim("t_hb", "worker1", lease_ttl_ms=120000)

        row = repo._conn.execute(
            "SELECT lease_version FROM tasks WHERE task_id='t_hb'"
        ).fetchone()
        lease_version = row["lease_version"]

        ok = repo.heartbeat("t_hb", "worker1", lease_version, lease_ttl_ms=240000)
        assert ok is True

        got = repo.get("t_hb")
        assert got.lease_expires_at is not None

    def test_update_uses_idempotency_key(self, repo):
        t1 = _create_task(task_id="t_opt", idempotency_key="ikey1")
        repo.insert(t1)

        t1.status = TaskStatus.running
        ok = repo.update(t1)
        assert ok is True

        got = repo.get("t_opt")
        assert got.status == TaskStatus.running


class TestTaskAttemptRepository:
    def _insert_parent_task(self, db):
        """插入一条父级 task（满足 FK 约束）。"""
        now = datetime.now(UTC)
        db.execute(
            "INSERT INTO tasks (task_id, task_type, status, created_at) "
            "VALUES ('t1', 'test', 'queued', ?)",
            (epoch_ms(now),),
        )
        db.commit()

    def test_insert(self, db):
        self._insert_parent_task(db)
        at_repo = TaskAttemptRepository(db)
        attempt = TaskAttempt(
            task_attempt_id="ta1",
            task_id="t1",
            attempt_no=1,
        )
        at_repo.insert(attempt)

        row = db.execute(
            "SELECT status FROM task_attempts WHERE task_attempt_id='ta1'"
        ).fetchone()
        assert row is not None
        assert row["status"] == "created"

    def test_succeed(self, db):
        self._insert_parent_task(db)
        at_repo = TaskAttemptRepository(db)
        attempt = TaskAttempt(
            task_attempt_id="ta1", task_id="t1", attempt_no=1,
        )
        at_repo.insert(attempt)

        ok = at_repo.succeed("ta1")
        assert ok is True

        row = db.execute(
            "SELECT status FROM task_attempts WHERE task_attempt_id='ta1'"
        ).fetchone()
        assert row["status"] == "succeeded"

    def test_fail(self, db):
        self._insert_parent_task(db)
        at_repo = TaskAttemptRepository(db)
        attempt = TaskAttempt(
            task_attempt_id="ta1", task_id="t1", attempt_no=1,
        )
        at_repo.insert(attempt)

        ok = at_repo.fail("ta1")
        assert ok is True

        row = db.execute(
            "SELECT status FROM task_attempts WHERE task_attempt_id='ta1'"
        ).fetchone()
        assert row["status"] == "failed"
