"""State machine transition validation tests."""

import pytest

from cogito.domain.delivery import DeliveryStatus
from cogito.domain.errors import InvalidStateTransitionError
from cogito.domain.memory import MemoryStatus
from cogito.domain.state_machines import (
    can_transition_attempt,
    can_transition_delivery,
    can_transition_memory,
    can_transition_task,
    can_transition_task_attempt,
    can_transition_turn,
    is_active_turn,
    is_terminal_delivery,
    is_terminal_task,
    is_terminal_turn,
    validate_transition_attempt,
    validate_transition_delivery,
    validate_transition_turn,
)
from cogito.domain.task import TaskAttemptStatus, TaskStatus
from cogito.domain.turn import RunAttemptStatus, TurnStatus


class TestTurnTransitions:
    def test_accepted_to_queued(self):
        assert can_transition_turn(TurnStatus.accepted, TurnStatus.queued)

    def test_queued_to_running(self):
        assert can_transition_turn(TurnStatus.queued, TurnStatus.running)

    def test_running_to_completed(self):
        assert can_transition_turn(TurnStatus.running, TurnStatus.completed)

    def test_running_to_waiting_user(self):
        assert can_transition_turn(TurnStatus.running, TurnStatus.waiting_user)

    def test_waiting_user_to_queued(self):
        assert can_transition_turn(TurnStatus.waiting_user, TurnStatus.queued)

    def test_running_to_failed(self):
        assert can_transition_turn(TurnStatus.running, TurnStatus.failed)

    def test_failed_to_queued(self):
        """Failed → Queued is allowed via RetryTurn command."""
        assert can_transition_turn(TurnStatus.failed, TurnStatus.queued)

    def test_illegal_transition(self):
        """Accepted → Completed directly should be illegal."""
        assert not can_transition_turn(TurnStatus.accepted, TurnStatus.completed)

    def test_terminal_states(self):
        assert is_terminal_turn(TurnStatus.completed)
        assert is_terminal_turn(TurnStatus.cancelled)
        assert not is_terminal_turn(TurnStatus.running)

    def test_active_states(self):
        assert is_active_turn(TurnStatus.queued)
        assert is_active_turn(TurnStatus.running)
        assert is_active_turn(TurnStatus.waiting_user)
        assert not is_active_turn(TurnStatus.completed)
        assert not is_active_turn(TurnStatus.accepted)

    def test_validate_transition_raises(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition_turn("t1", TurnStatus.accepted, TurnStatus.completed)


class TestRunAttemptTransitions:
    def test_created_to_running(self):
        assert can_transition_attempt(RunAttemptStatus.created, RunAttemptStatus.running)

    def test_running_to_succeeded(self):
        assert can_transition_attempt(RunAttemptStatus.running, RunAttemptStatus.succeeded)

    def test_illegal_skip_running(self):
        assert not can_transition_attempt(RunAttemptStatus.created, RunAttemptStatus.succeeded)

    def test_validate_raises(self):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition_attempt("ra1", RunAttemptStatus.created, RunAttemptStatus.succeeded)


class TestTaskTransitions:
    def test_created_to_queued(self):
        assert can_transition_task(TaskStatus.created, TaskStatus.queued)

    def test_created_to_scheduled(self):
        assert can_transition_task(TaskStatus.created, TaskStatus.scheduled)

    def test_running_to_completed(self):
        assert can_transition_task(TaskStatus.running, TaskStatus.completed)

    def test_terminal(self):
        assert is_terminal_task(TaskStatus.completed)
        assert is_terminal_task(TaskStatus.cancelled)

    def test_retry_path(self):
        assert can_transition_task(TaskStatus.failed, TaskStatus.queued)


class TestTaskAttemptTransitions:
    def test_created_to_running(self):
        assert can_transition_task_attempt(TaskAttemptStatus.created, TaskAttemptStatus.running)

    def test_running_to_failed(self):
        assert can_transition_task_attempt(TaskAttemptStatus.running, TaskAttemptStatus.failed)


class TestDeliveryTransitions:
    def test_pending_to_scheduled(self):
        assert can_transition_delivery(DeliveryStatus.pending, DeliveryStatus.scheduled)

    def test_sending_to_sent(self):
        assert can_transition_delivery(DeliveryStatus.sending, DeliveryStatus.sent)

    def test_terminal(self):
        assert is_terminal_delivery(DeliveryStatus.sent)
        assert is_terminal_delivery(DeliveryStatus.failed)
        assert is_terminal_delivery(DeliveryStatus.cancelled)
        assert not is_terminal_delivery(DeliveryStatus.sending)


class TestMemoryTransitions:
    def test_candidate_to_confirmed(self):
        assert can_transition_memory(MemoryStatus.candidate, MemoryStatus.confirmed)

    def test_confirmed_to_expired(self):
        assert can_transition_memory(MemoryStatus.confirmed, MemoryStatus.expired)

    def test_rejected_is_terminal(self):
        assert not can_transition_memory(MemoryStatus.rejected, MemoryStatus.confirmed)
