"""Tests for Task and TaskAttempt domain entities."""

from cogito.domain.task import Task, TaskAttempt, TaskAttemptStatus, TaskStatus


class TestTask:
    def test_create_default(self):
        t = Task()
        assert t.task_id is not None
        assert t.status == TaskStatus.created
        assert t.priority == 40

    def test_create_with_values(self):
        t = Task(
            task_id="tk1",
            task_type="connector.poll",
            status=TaskStatus.running,
            priority=20,
            idempotency_key="ik1",
            origin="scheduler",
        )
        assert t.task_type == "connector.poll"
        assert t.idempotency_key == "ik1"
        assert t.origin == "scheduler"

    def test_to_dict_roundtrip(self):
        t1 = Task(task_id="tk1", task_type="memory.consolidate")
        d = t1.to_dict()
        t2 = Task.from_dict(d)
        assert t1 == t2
        assert t2.task_type == "memory.consolidate"


class TestTaskAttempt:
    def test_create_default(self):
        ta = TaskAttempt()
        assert ta.attempt_no == 1
        assert ta.lease_version == 1
        assert ta.status == TaskAttemptStatus.created

    def test_create_with_values(self):
        ta = TaskAttempt(
            task_attempt_id="ta1",
            task_id="tk1",
            attempt_no=2,
            lease_version=1,
            lease_owner="worker1",
            status=TaskAttemptStatus.running,
        )
        assert ta.attempt_no == 2
        assert ta.lease_owner == "worker1"

    def test_to_dict_roundtrip(self):
        ta1 = TaskAttempt(task_attempt_id="ta1", task_id="tk1", attempt_no=1)
        d = ta1.to_dict()
        ta2 = TaskAttempt.from_dict(d)
        assert ta1 == ta2
