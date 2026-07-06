"""Tests for Turn and RunAttempt domain entities."""

from cogito.domain.turn import Turn, TurnStatus, RunAttempt, RunAttemptStatus


class TestTurn:
    def test_create_default(self):
        t = Turn()
        assert t.turn_id is not None
        assert t.status == TurnStatus.created
        assert t.priority == 80

    def test_create_with_values(self):
        t = Turn(turn_id="t1", session_id="s1", priority=100, status=TurnStatus.running)
        assert t.turn_id == "t1"
        assert t.priority == 100
        assert t.status == TurnStatus.running

    def test_to_dict_roundtrip(self):
        t1 = Turn(turn_id="t1", session_id="s1", priority=50)
        d = t1.to_dict()
        t2 = Turn.from_dict(d)
        assert t1 == t2
        assert t2.priority == 50

    def test_equality(self):
        a = Turn(turn_id="same")
        b = Turn(turn_id="same")
        assert a == b


class TestRunAttempt:
    def test_create_default(self):
        ra = RunAttempt()
        assert ra.attempt_id is not None
        assert ra.attempt_no == 1
        assert ra.status == RunAttemptStatus.created

    def test_create_with_values(self):
        ra = RunAttempt(
            attempt_id="ra1", turn_id="t1", attempt_no=2,
            status=RunAttemptStatus.running,
        )
        assert ra.attempt_no == 2
        assert ra.status == RunAttemptStatus.running

    def test_to_dict_roundtrip(self):
        ra1 = RunAttempt(attempt_id="ra1", turn_id="t1", attempt_no=3)
        d = ra1.to_dict()
        ra2 = RunAttempt.from_dict(d)
        assert ra1 == ra2
        assert ra2.attempt_no == 3

    def test_turn_and_attempt_relationship(self):
        t = Turn(turn_id="t1", session_id="s1")
        ra = RunAttempt(turn_id=t.turn_id, attempt_no=1)
        t.active_attempt_id = ra.attempt_id
        assert t.active_attempt_id is not None
        assert ra.turn_id == t.turn_id
