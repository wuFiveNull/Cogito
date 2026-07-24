"""Tests for Dispatcher and Turn completion — Event-only."""
import sqlite3
import uuid
from datetime import UTC, datetime

import pytest

from cogito.domain.turn import RunAttemptStatus, Turn, TurnStatus
from cogito.service.completion import TurnCompletionService
from cogito.service.dispatcher import Dispatcher
from cogito.store.event_replay import replay_delivery, replay_run_attempt, replay_turn
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate
from cogito.store.time_utils import epoch_ms

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


def _create_session(
    conn: sqlite3.Connection,
    session_id: str = "s1",
    conversation_id: str = "c1",
    context_partition_key: str = "c1",
) -> None:
    """Legacy helper for tests that still reference session tables."""
    conn.execute(
        "INSERT OR IGNORE INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
        "VALUES (?, 'private', ?)",
        (conversation_id, conversation_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, conversation_id, context_partition_key, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, conversation_id, context_partition_key, epoch_ms(datetime.now(UTC))),
    )
    conn.commit()


def _create_queued_turn(
    conn: sqlite3.Connection, session_id: str = "s1", priority: int = 80
) -> Turn:
    """Helper: append events to create a queued turn."""
    from cogito.domain.event import Event, EventClass, EventContext

    store = EventStore(conn)
    turn_id = "turn-" + uuid.uuid4().hex[:12]
    store.append(
        Event(
            event_type="interaction.message.accepted",
            stream_type="turn",
            stream_id=turn_id,
            producer="test",
            event_class=EventClass.DOMAIN,
            context=EventContext(session_id=session_id),
            summary="Message accepted",
        )
    )
    store.append(
        Event(
            event_type="runtime.turn.queued",
            stream_type="turn",
            stream_id=turn_id,
            producer="test",
            event_class=EventClass.DOMAIN,
            context=EventContext(session_id=session_id),
            summary="Turn queued",
            attributes={"priority": priority},
        )
    )
    return Turn(
        turn_id=turn_id,
        session_id=session_id,
        status=TurnStatus.queued,
        priority=priority,
        version=2,
    )


def _assert_turn_status(db: sqlite3.Connection, turn_id: str, expected: str) -> None:
    state = replay_turn(EventStore(db).read_stream("turn", turn_id), turn_id)
    assert state is not None and state.status == expected, f"expected {expected}, got {state.status if state else None}"


def _assert_attempt_status(db: sqlite3.Connection, attempt_id: str, expected: str) -> None:
    state = replay_run_attempt(EventStore(db).read_stream("run_attempt", attempt_id), attempt_id)
    assert state is not None and state.status == expected, f"expected {expected}, got {state.status if state else None}"


# =============================================================================
# Dispatcher claim_next
# =============================================================================


class TestClaimNext:
    def test_claim_queued_turn(self, db: sqlite3.Connection):
        turn = _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        result = dispatcher.claim_next("worker1")

        assert result is not None
        assert result.turn.turn_id == turn.turn_id
        assert result.attempt.turn_id == turn.turn_id
        assert result.attempt.attempt_no == 1
        assert result.attempt.status == RunAttemptStatus.running
        assert result.attempt.started_at is not None

        turn_events = EventStore(db).read_stream("turn", turn.turn_id)
        assert len(turn_events) == 3  # accepted, queued, started
        assert turn_events[-1].event_type == "runtime.turn.started"
        assert turn_events[-1].context.attempt_id == result.attempt.attempt_id
        assert turn_events[-1].attributes["worker_id"] == "worker1"

        attempt_events = EventStore(db).read_stream("run_attempt", result.attempt.attempt_id)
        assert len(attempt_events) == 1
        assert attempt_events[0].event_type == "runtime.attempt.started"

    def test_returns_none_when_empty(self, db: sqlite3.Connection):
        dispatcher = Dispatcher(db)
        result = dispatcher.claim_next("worker1")
        assert result is None

    def test_skips_sessions_with_running_turn(self, db: sqlite3.Connection):
        """Lane: same session can't have two running turns."""
        _create_queued_turn(db, "s1")
        _create_queued_turn(db, "s1")  # second turn, should be skipped

        dispatcher = Dispatcher(db)
        r1 = dispatcher.claim_next("worker1")
        assert r1 is not None
        assert r1.turn.session_id == "s1"

        # Second claim should return None since s1 already has a running turn
        r2 = dispatcher.claim_next("worker1")
        assert r2 is None

    def test_priority_ordering(self, db: sqlite3.Connection):
        """Higher priority (larger number) turns are claimed first."""
        low = _create_queued_turn(db, "s1", priority=20)
        high = _create_queued_turn(db, "s2", priority=100)

        dispatcher = Dispatcher(db)
        r1 = dispatcher.claim_next("worker1")
        assert r1 is not None
        assert r1.turn.turn_id == high.turn_id

    def test_concurrent_claim_only_one_succeeds(self, db: sqlite3.Connection):
        """Test version check prevents double-claim."""
        _create_queued_turn(db)

        dispatcher1 = Dispatcher(db)
        dispatcher2 = Dispatcher(db)

        r1 = dispatcher1.claim_next("worker1")
        r2 = dispatcher2.claim_next("worker2")

        assert r1 is not None
        assert r2 is None  # version changed by first claim

    def test_claim_creates_run_attempt(self, db: sqlite3.Connection):
        turn = _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        result = dispatcher.claim_next("worker1")
        assert result is not None

        # Verify attempt state from events
        _assert_attempt_status(db, result.attempt.attempt_id, "running")
        assert result.attempt.attempt_no == 1
        assert result.attempt.turn_id == turn.turn_id

    def test_attempt_no_increments(self, db: sqlite3.Connection):
        """When an attempt already exists, next attempt gets incrementing no."""
        from cogito.domain.event import Event, EventClass, EventContext

        turn = _create_queued_turn(db)

        # Append a previous failed attempt event
        EventStore(db).append(
            Event(
                event_type="runtime.attempt.started",
                stream_type="run_attempt",
                stream_id=f"prev-attempt-{turn.turn_id}",
                producer="test",
                event_class=EventClass.OPERATION,
                context=EventContext(turn_id=turn.turn_id),
                attributes={"attempt_no": 1, "worker_id": "prev-worker"},
            )
        )

        dispatcher = Dispatcher(db)
        result = dispatcher.claim_next("worker1")
        assert result is not None
        assert result.attempt.attempt_no == 2


# =============================================================================
# Dispatcher cancel
# =============================================================================


class TestCancelTurn:
    def test_cancel_queued_turn(self, db: sqlite3.Connection):
        turn = _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        result = dispatcher.cancel(turn.turn_id, turn.version)
        assert result is True

        _assert_turn_status(db, turn.turn_id, "cancelled")
        last_event = EventStore(db).read_stream("turn", turn.turn_id)[-1]
        assert last_event.event_type == "runtime.turn.cancelled"

    def test_cancel_already_running_fails(self, db: sqlite3.Connection):
        turn = _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        dispatcher.claim_next("worker1")

        result = dispatcher.cancel(turn.turn_id, turn.version)
        assert result is False

    def test_cancel_wrong_version(self, db: sqlite3.Connection):
        turn = _create_queued_turn(db)

        dispatcher = Dispatcher(db)
        result = dispatcher.cancel(turn.turn_id, 999)
        assert result is False


# =============================================================================
# Dispatcher complete
# =============================================================================


class TestComplete:
    def test_complete_success(self, db: sqlite3.Connection):
        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.complete(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            claimed.turn.version,
            worker_id=claimed.attempt.worker_id,
            lease_version=claimed.attempt.lease_version,
            final_message_id="final_msg_1",
        )
        assert ok is True

        # Verify from events
        _assert_turn_status(db, claimed.turn.turn_id, "completed")
        _assert_attempt_status(db, claimed.attempt.attempt_id, "succeeded")
        last_event = EventStore(db).read_stream("turn", claimed.turn.turn_id)[-1]
        assert last_event.event_type == "runtime.turn.completed"
        assert last_event.context.attempt_id == claimed.attempt.attempt_id

    def test_stale_version_cannot_complete(self, db: sqlite3.Connection):
        """Submitting with an old version should be a no-op."""
        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        stale_version = claimed.turn.version - 1
        result = dispatcher.complete(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            stale_version,
            worker_id=claimed.attempt.worker_id,
            lease_version=claimed.attempt.lease_version,
        )
        assert result is False
        # Turn stays running
        _assert_turn_status(db, claimed.turn.turn_id, "running")


# =============================================================================
# Dispatcher fail
# =============================================================================


class TestFail:
    def test_fail_marks_both(self, db: sqlite3.Connection):
        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        ok = dispatcher.fail(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            claimed.turn.version,
            worker_id=claimed.attempt.worker_id,
            lease_version=claimed.attempt.lease_version,
        )
        assert ok is True

        _assert_turn_status(db, claimed.turn.turn_id, "failed")
        _assert_attempt_status(db, claimed.attempt.attempt_id, "failed")
        last_event = EventStore(db).read_stream("turn", claimed.turn.turn_id)[-1]
        assert last_event.event_type == "runtime.turn.failed"
        assert last_event.context.attempt_id == claimed.attempt.attempt_id

    def test_resume_from_failed(self, db: sqlite3.Connection):
        """A failed turn can be resumed."""
        turn = _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        dispatcher.fail(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            claimed.turn.version,
            worker_id=claimed.attempt.worker_id,
            lease_version=claimed.attempt.lease_version,
        )

        resumed = Dispatcher(db).resume(turn.turn_id, "worker-recovery", checkpoint_ref="checkpoint-1")
        assert resumed is not None
        last_event = EventStore(db).read_stream("turn", turn.turn_id)[-1]
        assert last_event.event_type == "runtime.turn.started"
        assert last_event.attributes.get("resumed") is True
        assert last_event.context.attempt_id == resumed.attempt.attempt_id


class TestWaitTransitions:
    def test_pause_for_approval_records_waiting_user_event(self, db: sqlite3.Connection):
        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        assert dispatcher.pause_for_approval(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            claimed.turn.version,
            claimed.attempt.worker_id,
            claimed.attempt.lease_version,
            "approval-1",
        )

        _assert_turn_status(db, claimed.turn.turn_id, "waiting_user")
        event = EventStore(db).read_stream("turn", claimed.turn.turn_id)[-1]
        assert event.event_type == "runtime.turn.waiting_user"
        assert event.attributes["approval_id"] == "approval-1"

    def test_pause_for_external_records_waiting_external_event(self, db: sqlite3.Connection):
        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        assert dispatcher.pause_for_external(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            claimed.turn.version,
            claimed.attempt.worker_id,
            claimed.attempt.lease_version,
            "waiting-1",
        )

        _assert_turn_status(db, claimed.turn.turn_id, "waiting_external")
        event = EventStore(db).read_stream("turn", claimed.turn.turn_id)[-1]
        assert event.event_type == "runtime.turn.waiting_external"
        assert event.attributes["waiting_id"] == "waiting-1"


# =============================================================================
# TurnCompletionService (Stub Agent end-to-end)
# =============================================================================


class TestTurnCompletionService:
    def test_complete_with_stub_creates_message(self, db: sqlite3.Connection, tmp_path):
        from cogito.infrastructure.payload_store import PayloadStore
        from cogito.contracts.envelope import ChannelEnvelope

        payload_store = PayloadStore(tmp_path / "payloads", db)

        turn = _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        service = TurnCompletionService(db, effect_payload_store=payload_store)
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

        # Message events exist
        msg_stream = EventStore(db).read_stream("message", msg_id)
        assert len(msg_stream) == 1
        assert msg_stream[0].event_type == "interaction.message.recorded"

    def test_complete_creates_delivery(self, db: sqlite3.Connection, tmp_path):
        from cogito.infrastructure.payload_store import PayloadStore

        payload_store = PayloadStore(tmp_path / "payloads", db)

        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        service = TurnCompletionService(db, effect_payload_store=payload_store)
        service.complete_with_stub(
            claimed.turn,
            claimed.attempt,
            conversation_id="c1",
            session_id="s1",
            endpoint_id="ep1",
            principal_id="p1",
            channel_type="test",
            delivery_target="test_channel",
        )

        streams = EventStore(db).read_stream_type("delivery")
        assert len(streams) == 1
        assert replay_delivery(streams, streams[0].stream_id).status == "pending"

    def test_complete_creates_canonical_event(self, db: sqlite3.Connection, tmp_path):
        from cogito.infrastructure.payload_store import PayloadStore

        payload_store = PayloadStore(tmp_path / "payloads", db)

        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        service = TurnCompletionService(db, effect_payload_store=payload_store)
        service.complete_with_stub(
            claimed.turn,
            claimed.attempt,
            conversation_id="c1",
            session_id="s1",
            endpoint_id="ep1",
            principal_id="p1",
            channel_type="test",
            delivery_target="test_channel",
        )

        events = EventStore(db).read_stream("turn", claimed.turn.turn_id)
        assert [e.event_type for e in events].count("runtime.turn.completed") == 1

    def test_turn_completed_after_stub(self, db: sqlite3.Connection, tmp_path):
        from cogito.infrastructure.payload_store import PayloadStore

        payload_store = PayloadStore(tmp_path / "payloads", db)

        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        service = TurnCompletionService(db, effect_payload_store=payload_store)
        service.complete_with_stub(
            claimed.turn,
            claimed.attempt,
            conversation_id="c1",
            session_id="s1",
            endpoint_id="ep1",
            principal_id="p1",
            channel_type="test",
            delivery_target="test_channel",
        )

        _assert_turn_status(db, claimed.turn.turn_id, "completed")
        last_event = EventStore(db).read_stream("turn", claimed.turn.turn_id)[-1]
        assert last_event.event_type == "runtime.turn.completed"

    def test_rollback_on_failure(self, db: sqlite3.Connection, tmp_path):
        """If completion fails mid-way, nothing should be committed."""
        from unittest.mock import patch

        import cogito.service.unit_of_work as uow_mod
        from cogito.infrastructure.payload_store import PayloadStore

        payload_store = PayloadStore(tmp_path / "payloads", db)

        _create_queued_turn(db)
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")

        service = TurnCompletionService(db, effect_payload_store=payload_store)
        with patch.object(uow_mod.UnitOfWork, "commit"):
            service.complete_with_stub(
                claimed.turn,
                claimed.attempt,
                conversation_id="c1",
                session_id="s1",
                endpoint_id="ep1",
                principal_id="p1",
                channel_type="test",
                delivery_target="test_channel",
            )

        # No events should exist after rollback
        assert len(EventStore(db).read_stream("turn", claimed.turn.turn_id)) == 3
        assert len(EventStore(db).read_stream_type("delivery")) == 0


# =============================================================================
# Full end-to-end: inbound → dispatch → stub → complete
# =============================================================================


class TestFullCycle:
    def test_create_then_dispatch_then_complete(self, db: sqlite3.Connection, tmp_path):
        """Simulate the full P2->P3 flow: accept -> claim -> stub -> complete."""
        from cogito.contracts.envelope import ChannelEnvelope
        from cogito.infrastructure.payload_store import PayloadStore
        from cogito.service.inbound_service import InboundService

        payload_store = PayloadStore(tmp_path / "payloads", db)

        svc = InboundService(db, payload_store=payload_store)
        accept_result = svc.accept(
            ChannelEnvelope(
                channel_type="test",
                channel_instance_id="ci1",
                platform_sender_id="user1",
                platform_conversation_id="pc1",
                platform_message_id="pm1",
                content_parts=[{"content_type": "text", "inline_data": "Hello Cogito!"}],
                received_at=datetime.now(UTC).isoformat(),
            )
        )
        assert accept_result.is_new is True

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None
        assert claimed.turn.turn_id == accept_result.turn_id
        assert claimed.turn.status == TurnStatus.running

        completion = TurnCompletionService(db, effect_payload_store=payload_store)
        msg_id = completion.complete_with_stub(
            claimed.turn,
            claimed.attempt,
            conversation_id="pc1",
            session_id=claimed.turn.session_id,
            endpoint_id="ep1",
            principal_id="p1",
            channel_type="test",
            delivery_target="test_channel",
        )
        assert msg_id is not None

        # Verify purely from events
        state = replay_turn(
            EventStore(db).read_stream("turn", claimed.turn.turn_id), claimed.turn.turn_id
        )
        assert state is not None
        assert state.status == "completed"

        attempt_state = replay_run_attempt(
            EventStore(db).read_stream("run_attempt", claimed.attempt.attempt_id),
            claimed.attempt.attempt_id,
        )
        assert attempt_state is not None
        assert attempt_state.status == "succeeded"

        events = EventStore(db).read_stream("turn", claimed.turn.turn_id)
        assert [e.event_type for e in events].count("runtime.turn.completed") == 1

    def test_retry_after_failure(self, db: sqlite3.Connection, tmp_path):
        """Failed turn can be retried: fail → new attempt."""
        from cogito.contracts.envelope import ChannelEnvelope
        from cogito.infrastructure.payload_store import PayloadStore
        from cogito.service.inbound_service import InboundService

        payload_store = PayloadStore(tmp_path / "payloads", db)

        svc = InboundService(db, payload_store=payload_store)
        svc.accept(
            ChannelEnvelope(
                channel_type="test",
                channel_instance_id="ci1",
                platform_sender_id="user1",
                platform_conversation_id="pc1",
                platform_message_id="pm2",
                content_parts=[{"content_type": "text", "inline_data": "Hello"}],
                received_at=datetime.now(UTC).isoformat(),
            )
        )

        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker1")
        assert claimed is not None

        dispatcher.fail(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            claimed.turn.version,
            worker_id=claimed.attempt.worker_id,
            lease_version=claimed.attempt.lease_version,
        )

        claimed2 = dispatcher.resume(claimed.turn.turn_id, "worker1")
        assert claimed2 is not None
        assert claimed2.attempt.attempt_no == 2
