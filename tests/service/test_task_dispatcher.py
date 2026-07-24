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

from cogito.domain.task import Task, TaskStatus
from cogito.service.task_dispatcher import TaskDispatcher
from cogito.store.event_replay import replay_task, replay_task_attempt
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate
from cogito.store.task_repo import TaskRepository
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


def _create_task(db, task_id="t1", task_type="memory.extract", priority=40):
    """Create a queued task via Event-only TaskRepository."""
    repo = TaskRepository(db)
    repo.insert(
        Task(
            task_id=task_id,
            task_type=task_type,
            status=TaskStatus.queued,
            priority=priority,
            idempotency_key=f"{task_id}:{epoch_ms(datetime.now(UTC))}",
            created_at=datetime.now(UTC),
        )
    )


class TestTaskDispatcher:
    def test_claim_next_returns_task_and_attempt(self, db, dispatcher):
        _create_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None
        assert claimed.task.task_id == "t1"
        assert claimed.task.status == TaskStatus.running
        assert claimed.attempt.task_id == "t1"
        assert claimed.attempt.lease_owner == "worker1"

    def test_claim_next_returns_highest_priority_first(self, db, dispatcher):
        _create_task(db, task_id="t_low", priority=20)
        _create_task(db, task_id="t_high", priority=80)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None
        assert claimed.task.task_id == "t_high"

    def test_claim_next_idle_when_no_tasks(self, db, dispatcher):
        claimed = dispatcher.claim_next("worker1")
        assert claimed is None

    def test_claim_next_only_queued(self, db, dispatcher):
        _create_task(db, task_id="t1")
        assert dispatcher.claim_next("worker1") is not None
        assert dispatcher.claim_next("worker2") is None

    def test_complete_success(self, db, dispatcher):
        _create_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.complete(claimed.task, claimed.attempt, "worker1")
        assert ok is True

        state = replay_task(EventStore(db).read_stream("task", "t1"), "t1")
        assert state is not None
        assert state.status == "completed"

    def test_complete_fails_wrong_worker(self, db, dispatcher):
        _create_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.complete(claimed.task, claimed.attempt, "wrong_worker")
        assert ok is False

    def test_fail_success(self, db, dispatcher):
        _create_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.fail(claimed.task, claimed.attempt, "worker1")
        assert ok is True

        state = replay_task(EventStore(db).read_stream("task", "t1"), "t1")
        assert state is not None
        assert state.status == "failed"

    def test_heartbeat(self, db, dispatcher):
        _create_task(db, task_id="t1")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.heartbeat(
            claimed.task.task_id,
            claimed.attempt.task_attempt_id,
            "worker1",
            claimed.attempt.lease_version,
        )
        assert ok is True
        assert db.in_transaction is False

    def test_retry_appends_scheduled_event(self, db, dispatcher):
        _create_task(db, task_id="t-retry")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        assert dispatcher.retry(claimed.task, claimed.attempt, "worker1", delay_seconds=30)

        state = replay_task(EventStore(db).read_stream("task", "t-retry"), "t-retry")
        assert state is not None
        assert state.status == "scheduled"
        event = EventStore(db).read_stream("task", "t-retry")[-1]
        assert event.event_type == "task.retry_scheduled"
        assert event.attributes["reason"] == "retry"

    def test_wait_appends_waiting_event(self, db, dispatcher):
        _create_task(db, task_id="t-wait")
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        assert dispatcher.wait(
            claimed.task,
            claimed.attempt,
            "worker1",
            TaskStatus.waiting_user,
            "approval-1",
        )

        state = replay_task(EventStore(db).read_stream("task", "t-wait"), "t-wait")
        assert state is not None
        assert state.status == "waiting_user"
        event = EventStore(db).read_stream("task", "t-wait")[-1]
        assert event.event_type == "task.waiting_user"
        assert event.attributes["waiting_id"] == "approval-1"
