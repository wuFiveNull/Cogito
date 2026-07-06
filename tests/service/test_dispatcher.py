"""Tests for Dispatcher and Turn completion (P3: Dispatcher + Lane + Stub Agent)."""

import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.domain.turn import RunAttemptStatus, Turn, TurnStatus
from cogito.service.completion import TurnCompletionService
from cogito.service.dispatcher import Dispatcher
from cogito.store.migration import migrate

# ── Fixtures ──


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _create_queued_turn(conn: sqlite3.Connection, session_id: str = "s1", priority: int = 80) -> Turn:
    """Helper: insert a queued turn directly into the database."""
    from cogito.domain.turn import TurnStatus

    turn = Turn(
        session_id=session_id,
        status=TurnStatus.queued,
        priority=priority,
        version=2,  # accepted(1) → queued(2)
    )
    conn.execute(
        "INSERT INTO turns (turn_id, session_id, input_message_id, status, priority, version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (turn.turn_id, turn.session_id, turn.input_message_id,
         turn.status.value, turn.priority, turn.version,
         turn.created_at.isoformat()),
    )
    conn.commit()
    return turn


def _create_session(conn: sqlite3.Connection, session_id: str = "s1",
                    conversation_id: str = "c1",
                    context_partition_key: str = "c1") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
        "VALUES (?, 'private', ?)",
        (conversation_id, conversation_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, conversation_id, context_partition_key, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, conversation_id, context_partition_key,
         datetime.now(UTC).isoformat()),
    )
    conn.commit()


# =============================================================================
# Dispatcher claim_next
# =============================================================================


class TestClaimNext:
    def test_claim_queued_turn(self, db: sqlite3.Connection):
        _create_session(db)
        turn = _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        result = dispatcher.claim_next("worker1")

        assert result is not None
        assert result.turn.turn_id == turn.turn_id
        assert result.attempt.turn_id == turn.turn_id
        assert result.attempt.attempt_no == 1
        assert result.attempt.status == RunAttemptStatus.running
        assert result.attempt.started_at is not None

        # Verify Turn is running
        row = db.execute(
            "SELECT status, active_attempt_id, version FROM turns WHERE turn_id=?",
            (turn.turn_id,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["active_attempt_id"] == result.attempt.attempt_id
        assert row["version"] == 3  # 2 → 3 (queued → running)

    def test_returns_none_when_empty(self, db: sqlite3.Connection):
        dispatcher = Dispatcher(db)
        result = dispatcher.claim_next("worker1")
        assert result is None

    def test_skips_sessions_with_running_turn(self, db: sqlite3.Connection):
        """Lane: same session can't have two running turns."""
        _create_session(db, "s1", "c1")
        t1 = _create_queued_turn(db, "s1")
        _create_queued_turn(db, "s1")  # second turn, should be skipped
        db.commit()

        dispatcher = Dispatcher(db)
        r1 = dispatcher.claim_next("worker1")
        assert r1 is not None
        assert r1.turn.turn_id == t1.turn_id

        # Second claim should return None since s1 already has a running turn
        r2 = dispatcher.claim_next("worker1")
        assert r2 is None

    def test_priority_ordering(self, db: sqlite3.Connection):
        """Higher priority (larger number) turns are claimed first."""
        _create_session(db, "s1", "c1")
        _create_session(db, "s2", "c2")
        high = _create_queued_turn(db, "s1", priority=100)  # high priority
        _create_queued_turn(db, "s2", priority=20)  # low priority
        db.commit()

        dispatcher = Dispatcher(db)
        r1 = dispatcher.claim_next("worker1")
        assert r1 is not None
        assert r1.turn.turn_id == high.turn_id  # higher priority first

    def test_concurrent_claim_only_one_succeeds(self, db: sqlite3.Connection):
        """Test version check prevents double-claim."""
        _create_session(db)
        _create_queued_turn(db)

        # Simulate two dispatchers trying to claim the same turn
        dispatcher1 = Dispatcher(db)
        dispatcher2 = Dispatcher(db)

        r1 = dispatcher1.claim_next("worker1")
        r2 = dispatcher2.claim_next("worker2")

        assert r1 is not None
        assert r2 is None  # version changed by first claim

    def test_claim_creates_run_attempt(self, db: sqlite3.Connection):
        _create_session(db)
        turn = _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        result = dispatcher.claim_next("worker1")
        assert result is not None

        row = db.execute(
            "SELECT status, attempt_no, turn_id FROM run_attempts WHERE attempt_id=?",
            (result.attempt.attempt_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "running"
        assert row["attempt_no"] == 1
        assert row["turn_id"] == turn.turn_id

    def test_attempt_no_increments(self, db: sqlite3.Connection):
        """When an attempt already exists, next attempt gets incrementing no."""
        _create_session(db)
        turn = _create_queued_turn(db)

        # Insert a previous failed attempt
        db.execute(
            "INSERT INTO run_attempts (attempt_id, turn_id, attempt_no, status) "
            "VALUES ('prev1', ?, 1, 'failed')",
            (turn.turn_id,),
        )
        db.commit()

        dispatcher = Dispatcher(db)
        result = dispatcher.claim_next("worker1")
        assert result is not None
        assert result.attempt.attempt_no == 2


# =============================================================================
# Dispatcher cancel
# =============================================================================


class TestCancelTurn:
    def test_cancel_queued_turn(self, db: sqlite3.Connection):
        _create_session(db)
        turn = _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        result = dispatcher.cancel(turn.turn_id, turn.version)
        assert result is True

        row = db.execute(
            "SELECT status FROM turns WHERE turn_id=?", (turn.turn_id,),
        ).fetchone()
        assert row["status"] == "cancelled"

    def test_cancel_already_running_fails(self, db: sqlite3.Connection):
        _create_session(db)
        turn = _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        dispatcher.claim_next("worker1")

        # Try to cancel with old version
        result = dispatcher.cancel(turn.turn_id, turn.version)
        assert result is False  # version mismatch, can't cancel running

    def test_cancel_wrong_version(self, db: sqlite3.Connection):
        _create_session(db)
        turn = _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        result = dispatcher.cancel(turn.turn_id, 999)  # wrong version
        assert result is False


# =============================================================================
# Dispatcher complete
# =============================================================================


class TestComplete:
    def test_complete_success(self, db: sqlite3.Connection):
        _create_session(db)
        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.complete(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            claimed.turn.version,  # now version is the post-claim version (3)
            final_message_id="final_msg_1",
        )
        assert ok is True

        # Turn completed
        row = db.execute(
            "SELECT status, final_message_id, version FROM turns WHERE turn_id=?",
            (claimed.turn.turn_id,),
        ).fetchone()
        assert row["status"] == "completed"
        assert row["final_message_id"] == "final_msg_1"

        # RunAttempt succeeded
        row = db.execute(
            "SELECT status FROM run_attempts WHERE attempt_id=?",
            (claimed.attempt.attempt_id,),
        ).fetchone()
        assert row["status"] == "succeeded"

    def test_stale_version_cannot_complete(self, db: sqlite3.Connection):
        """Submitting with an old version should be a no-op."""
        _create_session(db)
        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        # Use the stale version (before the claim incremented it)
        stale_version = claimed.turn.version - 1
        dispatcher.complete(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            stale_version,
            final_message_id="final_msg_1",
        )
        # The UPDATE won't match, but the run_attempt update still goes through
        # This is ok - the turn stays running and can be retried
        row = db.execute(
            "SELECT status FROM turns WHERE turn_id=?",
            (claimed.turn.turn_id,),
        ).fetchone()
        assert row["status"] == "running"  # still running


# =============================================================================
# Dispatcher fail
# =============================================================================


class TestFail:
    def test_fail_marks_both(self, db: sqlite3.Connection):
        _create_session(db)
        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.fail(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            claimed.turn.version,
        )
        assert ok is True

        turn_row = db.execute(
            "SELECT status FROM turns WHERE turn_id=?",
            (claimed.turn.turn_id,),
        ).fetchone()
        assert turn_row["status"] == "failed"

        att_row = db.execute(
            "SELECT status FROM run_attempts WHERE attempt_id=?",
            (claimed.attempt.attempt_id,),
        ).fetchone()
        assert att_row["status"] == "failed"


# =============================================================================
# TurnCompletionService (Stub Agent end-to-end)
# =============================================================================


class TestTurnCompletionService:
    def test_complete_with_stub_creates_message(self, db: sqlite3.Connection):
        _create_session(db, "s1", "c1")
        turn = _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        service = TurnCompletionService(db)
        msg_id = service.complete_with_stub(
            claimed.turn,
            claimed.attempt,
            conversation_id="c1",
            session_id="s1",
            endpoint_id="ep1",
            principal_id="p1",
            channel_type="test",
            delivery_target="test_channel",
        )
        assert msg_id is not None

        # Message exists
        msg = db.execute(
            "SELECT role, direction, reply_to_message_id FROM messages WHERE message_id=?",
            (msg_id,),
        ).fetchone()
        assert msg is not None
        assert msg["role"] == "assistant"
        assert msg["direction"] == "outbound"
        assert msg["reply_to_message_id"] == turn.input_message_id

        # Content part exists with stub text
        part = db.execute(
            "SELECT inline_data FROM content_parts WHERE message_id=?",
            (msg_id,),
        ).fetchone()
        assert part is not None
        assert "stub mode" in part["inline_data"]

    def test_complete_creates_delivery(self, db: sqlite3.Connection):
        _create_session(db, "s1", "c1")
        _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        service = TurnCompletionService(db)
        service.complete_with_stub(
            claimed.turn, claimed.attempt,
            conversation_id="c1", session_id="s1",
            endpoint_id="ep1", principal_id="p1",
            channel_type="test",
            delivery_target="test_channel",
        )

        deliveries = db.execute("SELECT status FROM deliveries").fetchall()
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "pending"

    def test_complete_creates_outbox_event(self, db: sqlite3.Connection):
        _create_session(db, "s1", "c1")
        _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        service = TurnCompletionService(db)
        service.complete_with_stub(
            claimed.turn, claimed.attempt,
            conversation_id="c1", session_id="s1",
            endpoint_id="ep1", principal_id="p1",
            channel_type="test",
            delivery_target="test_channel",
        )

        events = db.execute(
            "SELECT event_type, aggregate_id FROM outbox_events"
        ).fetchall()
        assert len(events) >= 1
        assert events[0]["event_type"] == "TurnCompleted"

    def test_turn_completed_after_stub(self, db: sqlite3.Connection):
        _create_session(db, "s1", "c1")
        _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        service = TurnCompletionService(db)
        service.complete_with_stub(
            claimed.turn, claimed.attempt,
            conversation_id="c1", session_id="s1",
            endpoint_id="ep1", principal_id="p1",
            channel_type="test",
            delivery_target="test_channel",
        )

        row = db.execute(
            "SELECT status, final_message_id FROM turns WHERE turn_id=?",
            (claimed.turn.turn_id,),
        ).fetchone()
        assert row["status"] == "completed"
        assert row["final_message_id"] is not None

    def test_rollback_on_failure(self, db: sqlite3.Connection):
        """If completion fails mid-way, nothing should be committed."""
        from unittest.mock import patch

        import cogito.service.unit_of_work as uow_mod

        _create_session(db, "s1", "c1")
        _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        # Mock only commit — __exit__ will still auto-rollback
        service = TurnCompletionService(db)
        with patch.object(uow_mod.UnitOfWork, "commit"):
            service.complete_with_stub(
                claimed.turn, claimed.attempt,
                conversation_id="c1", session_id="s1",
                endpoint_id="ep1", principal_id="p1",
                channel_type="test",
                delivery_target="test_channel",
            )

        # Nothing should exist (real __exit__ rolled back since _committed=False)
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0


# =============================================================================
# Full end-to-end: inbound → dispatch → stub → complete
# =============================================================================


class TestFullCycle:
    def test_create_then_dispatch_then_complete(self, db: sqlite3.Connection):
        """Simulate the full P2->P3 flow: accept -> claim -> stub -> complete."""
        from cogito.contracts.envelope import ChannelEnvelope
        from cogito.service.inbound_service import InboundService

        # Ensure conversation exists for message FK
        db.execute(
            "INSERT OR IGNORE INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
            "VALUES ('pc1', 'private', 'pc1')"
        )
        svc = InboundService(db)
        accept_result = svc.accept(ChannelEnvelope(
            channel_type="test",
            channel_instance_id="ci1",
            platform_sender_id="user1",
            platform_conversation_id="pc1",
            platform_message_id="pm1",
            content_parts=[{"content_type": "text", "inline_data": "Hello Cogito!"}],
            received_at=datetime.now(UTC).isoformat(),
        ))
        assert accept_result.is_new is True

        # 2. Dispatcher claims the turn
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None
        assert claimed.turn.turn_id == accept_result.turn_id
        assert claimed.turn.status == TurnStatus.running

        # 3. Complete with stub agent
        completion = TurnCompletionService(db)
        msg_id = completion.complete_with_stub(
            claimed.turn, claimed.attempt,
            conversation_id="pc1",
            session_id=claimed.turn.session_id,
            endpoint_id="ep1",
            principal_id="p1",
            channel_type="test",
            delivery_target="test_channel",
        )
        assert msg_id is not None

        # 4. Verify full state
        assert db.execute(
            "SELECT status FROM turns WHERE turn_id=?",
            (claimed.turn.turn_id,),
        ).fetchone()["status"] == "completed"

        assert db.execute(
            "SELECT status FROM run_attempts WHERE attempt_id=?",
            (claimed.attempt.attempt_id,),
        ).fetchone()["status"] == "succeeded"

        # 2 outbox events: InboundMessageAccepted + TurnQueued + TurnCompleted
        assert db.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 3

    def test_retry_after_failure(self, db: sqlite3.Connection):
        """Failed turn can be retried: fail → new attempt."""
        from cogito.contracts.envelope import ChannelEnvelope
        from cogito.service.inbound_service import InboundService

        # Accept + claim
        svc = InboundService(db)
        svc.accept(ChannelEnvelope(
            channel_type="test", channel_instance_id="ci1",
            platform_sender_id="user1", platform_conversation_id="pc1",
            platform_message_id="pm2",
            content_parts=[{"content_type": "text", "inline_data": "Hello"}],
            received_at=datetime.now(UTC).isoformat(),
        ))

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        # Fail
        dispatcher.fail(claimed.turn.turn_id, claimed.attempt.attempt_id,
                        claimed.turn.version)

        # Retry: re-queue and claim again
        from cogito.domain.state_machines import can_transition_turn
        assert can_transition_turn(TurnStatus.failed, TurnStatus.queued)
        db.execute(
            "UPDATE turns SET status='queued', version=version+1 "
            "WHERE turn_id=? AND status='failed'",
            (claimed.turn.turn_id,),
        )
        db.commit()

        claimed2 = dispatcher.claim_next("worker1")
        assert claimed2 is not None
        assert claimed2.attempt.attempt_no == 2  # second attempt
