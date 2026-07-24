"""Task Repository — tasks / task_attempts Event-only CRUD.

All state is reconstructed from Event streams.  Legacy table rows are never
read or written.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from cogito.contracts.clock import epoch_ms, from_epoch_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.domain.task import Task, TaskAttempt, TaskAttemptStatus, TaskStatus
from cogito.store.event_replay import replay_task, replay_task_attempt
from cogito.store.event_store import EventStore


class TaskRepository:
    """Task Event-only read/write boundary."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── Reads ──

    def get(self, task_id: str) -> Task | None:
        events = EventStore(self._conn).read_stream("task", task_id)
        projection = replay_task(events, task_id)
        return self._task_from_projection(projection) if projection is not None else None

    def find_queued(
        self,
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[Task]:
        """Find claimable Tasks: queued or scheduled and past their fire time."""
        now_ms = epoch_ms(now or datetime.now(UTC))
        tasks = [
            task
            for task in self._event_tasks()
            if task.status in {TaskStatus.queued, TaskStatus.scheduled}
            and (task.scheduled_at is None or task.scheduled_at <= now_ms)
        ]
        tasks.sort(key=lambda t: (t.priority or 40), reverse=True)
        return tasks[:limit]

    def find_by_type(
        self,
        task_type: str,
        status: str = "queued",
        limit: int = 10,
    ) -> list[Task]:
        return [
            task
            for task in self._event_tasks()
            if task.task_type == task_type and task.status.value == status
        ][:limit]

    def exists_by_idempotency(self, idempotency_key: str) -> bool:
        events = EventStore(self._conn).read_stream_type("task")
        for event in events:
            if event.idempotency_key and event.idempotency_key == idempotency_key:
                return True
        return False

    def list_filtered(
        self,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Task]:
        tasks = [
            task
            for task in self._event_tasks()
            if status is None or task.status.value == status
        ]
        tasks.sort(key=lambda t: epoch_ms(t.created_at) if t.created_at else 0, reverse=True)
        return tasks[offset : offset + limit]

    def count(self, status: str | None = None) -> int:
        tasks = self._event_tasks()
        if status is None:
            return len(tasks)
        return sum(1 for task in tasks if task.status.value == status)

    # ── Writes ──

    def insert(self, task: Task) -> Task:
        attrs = {
            "task_type": task.task_type,
            "priority": task.priority,
            "origin": task.origin,
            "idempotency_key": task.idempotency_key,
        }
        if task.retry_policy:
            attrs["retry_policy"] = dict(task.retry_policy)
        if task.scheduled_at is not None:
            attrs["scheduled_at"] = task.scheduled_at
        EventStore(self._conn).append(
            Event(
                event_type="task.created",
                stream_type="task",
                stream_id=task.task_id,
                producer="task-repository",
                event_class=EventClass.DOMAIN,
                summary=f"Task created: {task.task_type}",
                attributes=attrs,
                payload_ref=task.payload_ref,
                outcome=task.status.value,
                occurred_at=epoch_ms(task.created_at),
                idempotency_key=f"task:{task.task_id}:created",
            ),
            expected_version=0,
        )
        return task

    def update(self, task: Task) -> bool:
        # All state transitions use specific event types (claim, complete, fail, etc.)
        # Updates to payload_ref or result_ref are applied via complete/fail.
        return True

    def claim(
        self, task_id: str, worker_id: str, lease_ttl_s: int, now_ms: int | None = None
    ) -> bool:
        now_ms = now_ms or epoch_ms(datetime.now(UTC))
        events = EventStore(self._conn).read_stream("task", task_id)
        state = replay_task(events, task_id)
        if state is None or state.status not in {"queued", "scheduled"}:
            return False
        try:
            EventStore(self._conn).append(
                Event(
                    event_type="task.leased",
                    stream_type="task",
                    stream_id=task_id,
                    producer="task-repository",
                    event_class=EventClass.OPERATION,
                    summary=f"Task leased by {worker_id}",
                    attributes={
                        "worker_id": worker_id,
                        "lease_version": state.lease_version + 1,
                        "lease_expires_at": now_ms + lease_ttl_s,
                    },
                    outcome="running",
                    occurred_at=now_ms,
                    idempotency_key=f"task:{task_id}:leased:{state.stream_version + 1}",
                ),
                expected_version=state.stream_version,
            )
        except Exception:
            return False
        return True

    def complete(
        self,
        task_id: str,
        worker_id: str,
        lease_version: int,
        *,
        now_ms: int | None = None,
        result_ref: str | None = None,
    ) -> bool:
        return self._finish_event_task(
            task_id, worker_id, lease_version, "completed",
            now_ms=now_ms, payload_ref=result_ref,
        )

    def fail(
        self,
        task_id: str,
        worker_id: str,
        lease_version: int,
        *,
        now_ms: int | None = None,
    ) -> bool:
        return self._finish_event_task(
            task_id, worker_id, lease_version, "failed",
            now_ms=now_ms,
        )

    def schedule_retry(
        self, task_id: str, worker_id: str, lease_version: int,
        scheduled_at: int, now_ms: int,
    ) -> bool:
        """Mark a task for retry at the given scheduled time."""
        events = EventStore(self._conn).read_stream("task", task_id)
        state = replay_task(events, task_id)
        if (state is None or state.status != "running"
            or state.lease_owner != worker_id
            or state.lease_version != lease_version):
            return False
        try:
            EventStore(self._conn).append(
                Event(
                    event_type="task.retry_scheduled",
                    stream_type="task",
                    stream_id=task_id,
                    producer="task-repository",
                    event_class=EventClass.DOMAIN,
                    summary=f"Task retry scheduled at {scheduled_at}",
                    attributes={
                        "scheduled_at": scheduled_at,
                        "reason": "retry",
                        "worker_id": worker_id,
                    },
                    outcome="scheduled",
                    occurred_at=now_ms,
                    idempotency_key=f"task:{task_id}:retry:{state.stream_version + 1}",
                ),
                expected_version=state.stream_version,
            )
        except Exception:
            return False
        return True

    def reset_to_queued(self, task_id: str) -> bool:
        events = EventStore(self._conn).read_stream("task", task_id)
        state = replay_task(events, task_id)
        if state is None or state.status != "running":
            return False
        try:
            EventStore(self._conn).append(
                Event(
                    event_type="task.scheduled",
                    stream_type="task",
                    stream_id=task_id,
                    producer="task-repository",
                    event_class=EventClass.DOMAIN,
                    summary="Task reset to queued",
                    outcome="queued",
                    idempotency_key=f"task:{task_id}:reset:{state.stream_version + 1}",
                ),
                expected_version=state.stream_version,
            )
        except Exception:
            return False
        return True

    def recover_expired_lease(
        self, task_id: str, lease_owner: str, lease_version: int, now_ms: int
    ) -> bool:
        """Recover an expired task lease — requeue the task."""
        events = EventStore(self._conn).read_stream("task", task_id)
        state = replay_task(events, task_id)
        if (
            state is None or state.status != "running"
            or state.lease_owner != lease_owner
            or state.lease_version != lease_version
        ):
            return False
        try:
            EventStore(self._conn).append(
                Event(
                    event_type="task.scheduled",
                    stream_type="task",
                    stream_id=task_id,
                    producer="task-repository",
                    event_class=EventClass.DOMAIN,
                    summary="Task recovered from expired lease",
                    outcome="queued",
                    idempotency_key=f"task:{task_id}:recover:{state.stream_version + 1}",
                ),
                expected_version=state.stream_version,
            )
        except Exception:
            return False
        return True

    def wait(
        self, task_id: str, worker_id: str, lease_version: int,
        waiting_status: str, waiting_id: str, now_ms: int,
    ) -> bool:
        """Transition a task to a waiting state."""
        events = EventStore(self._conn).read_stream("task", task_id)
        state = replay_task(events, task_id)
        if (state is None or state.status != "running"
            or state.lease_owner != worker_id
            or state.lease_version != lease_version):
            return False
        try:
            EventStore(self._conn).append(
                Event(
                    event_type=f"task.{waiting_status}",
                    stream_type="task",
                    stream_id=task_id,
                    producer="task-repository",
                    event_class=EventClass.OPERATION,
                    summary=f"Task waiting: {waiting_id}",
                    attributes={
                        "waiting_id": waiting_id,
                        "reason": waiting_status,
                    },
                    outcome=waiting_status,
                    occurred_at=now_ms,
                    idempotency_key=f"task:{task_id}:wait:{state.stream_version + 1}",
                ),
                expected_version=state.stream_version,
            )
        except Exception:
            return False
        return True

    def heartbeat(
        self, task_id: str, worker_id: str, lease_version: int,
        lease_ttl_ms: int, *, now_ms: int | None = None,
    ) -> bool:
        events = EventStore(self._conn).read_stream("task", task_id)
        state = replay_task(events, task_id)
        if (state is None or state.status != "running"
            or state.lease_owner != worker_id
            or state.lease_version != lease_version):
            return False
        now_ms = now_ms or epoch_ms(datetime.now(UTC))
        new_expires = now_ms + lease_ttl_ms
        try:
            EventStore(self._conn).append(
                Event(
                    event_type="task.lease_renewed",
                    stream_type="task",
                    stream_id=task_id,
                    producer="task-repository",
                    event_class=EventClass.OPERATION,
                    summary="Task lease renewed",
                    attributes={
                        "worker_id": worker_id,
                        "lease_version": lease_version + 1,
                        "lease_expires_at": new_expires,
                    },
                    outcome="running",
                    idempotency_key=f"task:{task_id}:lease:{lease_version + 1}:{now_ms}",
                ),
                expected_version=state.stream_version,
            )
        except Exception:
            return False
        return True

    # ── Helpers ──

    def _finish_event_task(
        self,
        task_id: str,
        worker_id: str,
        lease_version: int,
        terminal: str,
        *,
        now_ms: int | None = None,
        payload_ref: str | None = None,
    ) -> bool:
        now_ms = now_ms or epoch_ms(datetime.now(UTC))
        events = EventStore(self._conn).read_stream("task", task_id)
        state = replay_task(events, task_id)
        if (
            state is None or state.status != "running"
            or state.lease_owner != worker_id
            or state.lease_version != lease_version
        ):
            return False
        try:
            EventStore(self._conn).append(
                Event(
                    event_type=f"task.{terminal}",
                    stream_type="task",
                    stream_id=task_id,
                    producer="task-repository",
                    event_class=EventClass.DOMAIN,
                    summary=f"Task {terminal}",
                    payload_ref=payload_ref,
                    outcome=terminal,
                    occurred_at=now_ms,
                    idempotency_key=f"task:{task_id}:{terminal}:{state.stream_version + 1}",
                ),
                expected_version=state.stream_version,
            )
        except Exception:
            return False
        return True

    def _event_tasks(self) -> list[Task]:
        grouped: dict[str, list[Event]] = {}
        for event in EventStore(self._conn).read_stream_type("task"):
            grouped.setdefault(event.stream_id, []).append(event)
        return [
            self._task_from_projection(projection)
            for task_id, stream in grouped.items()
            if (projection := replay_task(stream, task_id)) is not None
        ]

    @staticmethod
    def _task_from_projection(projection) -> Task:
        return Task(
            task_id=projection.task_id,
            task_type=projection.task_type,
            payload_ref=projection.payload_ref,
            status=TaskStatus(projection.status),
            priority=projection.priority or 40,
            origin=projection.origin,
            scheduled_at=projection.scheduled_at,
            retry_policy=dict(projection.retry_policy or {}),
            checkpoint_ref=projection.checkpoint_ref,
            idempotency_key=projection.idempotency_key,
            lease_owner=projection.lease_owner,
            lease_expires_at=from_epoch_ms(projection.lease_expires_at),
            lease_version=projection.lease_version,
            result_ref=projection.result_ref,
            created_at=from_epoch_ms(projection.created_at),
        )


class TaskAttemptRepository:
    """TaskAttempt Event-only read/write boundary."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, attempt: TaskAttempt) -> TaskAttempt:
        EventStore(self._conn).append(
            Event(
                event_type="task.attempt.started",
                stream_type="task_attempt",
                stream_id=attempt.task_attempt_id,
                producer="task-attempt-repository",
                event_class=EventClass.OPERATION,
                summary=f"Task attempt started: #{attempt.attempt_no}",
                attributes={
                    "task_id": attempt.task_id,
                    "attempt_no": attempt.attempt_no,
                    "worker_id": attempt.lease_owner,
                    "lease_version": attempt.lease_version,
                    "lease_expires_at": epoch_ms(attempt.lease_expires_at) if attempt.lease_expires_at else None,
                    "checkpoint_ref": attempt.checkpoint_ref or "",
                },
                payload_ref=attempt.checkpoint_ref or None,
                outcome="running",
                occurred_at=epoch_ms(attempt.started_at) if attempt.started_at else 0,
                idempotency_key=f"task-attempt:{attempt.task_attempt_id}:started",
            ),
            expected_version=0,
        )
        return attempt

    def succeed(self, attempt_id: str, finished_at: int | None = None) -> bool:
        return self._finish_event_attempt(attempt_id, finished_at or 0, "task.attempt.completed")

    def fail(self, attempt_id: str, finished_at: int | None = None) -> bool:
        return self._finish_event_attempt(attempt_id, finished_at or 0, "task.attempt.failed")

    def abandon(self, attempt_id: str, finished_at: int | None = None) -> bool:
        return self._finish_event_attempt(attempt_id, finished_at or 0, "task.attempt.abandoned")

    def list_for_task(self, task_id: str) -> list[TaskAttempt]:
        return [
            attempt
            for attempt in self._event_attempts()
            if attempt.task_id == task_id
        ]

    def get_attempt(self, attempt_id: str) -> TaskAttempt | None:
        events = EventStore(self._conn).read_stream("task_attempt", attempt_id)
        projection = replay_task_attempt(events, attempt_id)
        return self._attempt_from_projection(projection) if projection is not None else None

    def _finish_event_attempt(self, attempt_id: str, finished_at: int, event_type: str) -> bool:
        events = EventStore(self._conn).read_stream("task_attempt", attempt_id)
        state = replay_task_attempt(events, attempt_id)
        if state is None:
            return False
        suffix = event_type.rsplit(".", 1)[-1]
        try:
            EventStore(self._conn).append(
                Event(
                    event_type=event_type,
                    stream_type="task_attempt",
                    stream_id=attempt_id,
                    producer="task-attempt-repository",
                    event_class=EventClass.OPERATION,
                    summary=f"Task attempt {suffix}",
                    outcome=suffix,
                    occurred_at=finished_at,
                    idempotency_key=f"task-attempt:{attempt_id}:{suffix}:{state.stream_version + 1}",
                ),
                expected_version=state.stream_version,
            )
        except Exception:
            return False
        return True

    def _event_attempts(self) -> list[TaskAttempt]:
        grouped: dict[str, list[Event]] = {}
        for event in EventStore(self._conn).read_stream_type("task_attempt"):
            grouped.setdefault(event.stream_id, []).append(event)
        return [
            self._attempt_from_projection(projection)
            for attempt_id, stream in grouped.items()
            if (projection := replay_task_attempt(stream, attempt_id)) is not None
        ]

    @staticmethod
    def _attempt_from_projection(projection) -> TaskAttempt:
        return TaskAttempt(
            task_attempt_id=projection.task_attempt_id,
            task_id=projection.task_id,
            attempt_no=projection.attempt_no,
            status=TaskAttemptStatus(projection.status),
            lease_owner=projection.lease_owner,
            lease_version=projection.lease_version,
            lease_expires_at=from_epoch_ms(projection.lease_expires_at),
            checkpoint_ref=projection.checkpoint_ref,
            started_at=from_epoch_ms(projection.started_at),
            finished_at=from_epoch_ms(projection.finished_at),
        )
