"""Regression coverage for remaining mutable workers and execution leases.

Delivery's former row-based worker is intentionally absent: delivery effects
are covered by the canonical Event worker tests instead.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.service.dispatcher import Dispatcher
from cogito.service.recovery_service import RecoveryService
from cogito.store.event_store import EventStore
from cogito.store.migration import migrate
from tests.service.test_dispatcher import _create_queued_turn, _create_session


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


class TestTurnLeaseValidation:
    def test_expired_attempt_cannot_complete(self, db: sqlite3.Connection) -> None:
        _create_session(db, "session-1", "conversation-1")
        _create_queued_turn(db, "session-1")
        dispatcher = Dispatcher(db)
        start = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        claimed = dispatcher.claim_next("worker-1", clock=start)
        assert claimed is not None

        assert not dispatcher.complete(
            claimed.turn.turn_id,
            claimed.attempt.attempt_id,
            claimed.turn.version,
            worker_id="worker-1",
            lease_version=claimed.attempt.lease_version,
            clock=datetime(2026, 1, 15, 12, 5, tzinfo=UTC),
        )

    def test_stale_attempt_is_requeued_by_recovery(self, db: sqlite3.Connection) -> None:
        _create_session(db, "session-1", "conversation-1")
        _create_queued_turn(db, "session-1")
        dispatcher = Dispatcher(db)
        claimed = dispatcher.claim_next("worker-1", clock=datetime(2026, 1, 15, 12, 0, tzinfo=UTC))
        assert claimed is not None

        assert RecoveryService(db).recover_stale_turns(
            clock=datetime(2026, 1, 15, 12, 5, tzinfo=UTC)
        ) == 1

        # Verify via events: turn was re-queued
        from cogito.store.event_replay import replay_turn

        state = replay_turn(
            EventStore(db).read_stream("turn", claimed.turn.turn_id), claimed.turn.turn_id
        )
        assert state is not None
        assert state.status == "queued"
