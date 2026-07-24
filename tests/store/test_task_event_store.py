from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from cogito.domain.task import Task, TaskStatus
from cogito.service.recovery_service import RecoveryService
from cogito.service.task_dispatcher import TaskDispatcher
from cogito.store.event_projection_store import EventProjectionStore
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate
from cogito.store.task_repo import TaskRepository


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def test_task_and_attempt_lifecycle_replays_without_state_rows() -> None:
    conn = _connection()
    try:
        now = datetime(2026, 7, 23, tzinfo=UTC)
        repository = TaskRepository(conn)
        repository.insert(
            Task(
                task_id="task-event-1",
                task_type="memory.extract",
                payload_ref="payload-1",
                status=TaskStatus.queued,
                priority=7,
                retry_policy={"max_attempts": 2},
                idempotency_key="task-event-idempotency-1",
                created_at=now,
            )
        )

        dispatcher = TaskDispatcher(conn)
        claimed = dispatcher.claim_next("worker-1", clock=now)
        assert claimed is not None
        assert claimed.task.lease_version == 1
        assert claimed.attempt.attempt_no == 1
        # Heartbeat succeeds
        hb_ok = dispatcher.heartbeat(
            claimed.task.task_id,
            claimed.attempt.task_attempt_id,
            "worker-1",
            claimed.attempt.lease_version,
            clock=now + timedelta(seconds=1),
        )
        assert hb_ok

        # Get updated lease_version after heartbeat
        updated_task = repository.get(claimed.task.task_id)
        assert updated_task is not None
        updated_task.result_ref = "result-payload-1"

        assert dispatcher.complete(
            updated_task,
            claimed.attempt,
            "worker-1",
            clock=now + timedelta(seconds=2),
        )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_attempts").fetchone()[0] == 0
        projection = EventProjectionStore(EventStore(conn))
        task = projection.tasks()[0]
        attempt = projection.task_attempts(task_id="task-event-1")[0]
        assert task["status"] == "completed"
        assert task["result_ref"] == "result-payload-1"
        assert attempt["status"] == "completed"
        assert attempt["lease_version"] == 1
        assert [event.event_type for event in EventStore(conn).read_stream("task", "task-event-1")] == [
            "task.created",
            "task.leased",
            "task.lease_renewed",
            "task.completed",
        ]
    finally:
        conn.close()


def test_expired_event_task_lease_is_abandoned_and_requeued() -> None:
    conn = _connection()
    try:
        now = datetime(2026, 7, 23, tzinfo=UTC)
        TaskRepository(conn).insert(
            Task(
                task_id="task-event-expired",
                task_type="connector.poll",
                status=TaskStatus.queued,
                idempotency_key="task-event-expired-key",
                created_at=now,
            )
        )
        claimed = TaskDispatcher(conn, lease_ttl_s=1).claim_next(
            "worker-1", clock=now
        )
        assert claimed is not None

        assert RecoveryService(conn).recover_stale_tasks(
            clock=now + timedelta(seconds=2)
        ) == 1

        projection = EventProjectionStore(EventStore(conn))
        assert projection.tasks()[0]["status"] == "queued"
        assert projection.task_attempts(task_id="task-event-expired")[0]["status"] == "abandoned"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_attempts").fetchone()[0] == 0
    finally:
        conn.close()
