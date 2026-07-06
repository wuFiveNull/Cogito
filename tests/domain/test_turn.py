"""Tests for Turn and RunAttempt domain entities."""

from cogito.domain.turn import Turn, TurnStatus, RunAttempt, RunAttemptStatus


class TestTurn:
    def test_create_default(self):
        t = Turn()
        assert t.turn_id is not None
        assert t.status == TurnStatus.accepted
        assert t.priority == 80
        assert t.version == 1
        assert t.input_message_id == ""

    def test_create_with_values(self):
        t = Turn(turn_id="t1", session_id="s1", input_message_id="msg1",
                 priority=100, status=TurnStatus.running, version=3)
        assert t.turn_id == "t1"
        assert t.priority == 100
        assert t.status == TurnStatus.running
        assert t.input_message_id == "msg1"
        assert t.version == 3

    def test_to_dict_roundtrip(self):
        t1 = Turn(turn_id="t1", session_id="s1", input_message_id="msg1", priority=50, version=2)
        d = t1.to_dict()
        t2 = Turn.from_dict(d)
        assert t1 == t2
        assert t2.priority == 50
        assert t2.input_message_id == "msg1"
        assert t2.version == 2

    def test_equality(self):
        a = Turn(turn_id="same")
        b = Turn(turn_id="same")
        assert a == b

    def test_queued_status(self):
        t = Turn(status=TurnStatus.queued)
        assert t.status == TurnStatus.queued

    def test_expired_status(self):
        t = Turn(status=TurnStatus.expired)
        assert t.status == TurnStatus.expired


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
